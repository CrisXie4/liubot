"""
LLM 内容审核引擎 — MiniMax API 驱动
======================================

两阶段审核:
  1. 关键词引擎初筛（快速、零成本）
  2. MiniMax M3 终审（精准、防误判）

学习机制:
  - 注入已学习的违规词库 + 历史案例
  - MiniMax 确认的违规 → 自动提取关键词加入知识库
  - 主人手动标记 → 强化学习信号
  - 误判反馈 → 校准阈值
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from . import learner, db, cache
from .config import config

logger = logging.getLogger("qqbot")

# ─── LLM 客户端（统一走 llm_call 共享客户端，读 model_settings 的 gemini-3.5-flash-free）───
# 注：旧版 llm_mod 维护独立的 _get_client（优先 minimax_api_key），
# 跟主聊天用的模型分裂，且硬编码 MiniMax-M3。
# 现已统一——所有 AI 调用走 llm_call.llm_create，读同一份 model_settings。

def _get_client() -> Optional[object]:
    """获取共享 LLM 客户端（委托给 llm_call）"""
    from . import llm_call
    return llm_call.get_client()

# ─── 审核日志缓存 ───
_moderation_log: list[dict] = []  # 最近 200 条审核记录
MAX_LOG_SIZE = 200

def _add_log(entry: dict):
    _moderation_log.insert(0, entry)
    if len(_moderation_log) > MAX_LOG_SIZE:
        _moderation_log.pop()

def get_moderation_logs(limit: int = 50) -> list[dict]:
    return _moderation_log[:limit]

def get_moderation_stats() -> dict:
    """获取 LLM 审核统计"""
    total = len(_moderation_log)
    if total == 0:
        return {"total": 0, "llm_confirmed": 0, "llm_rejected": 0,
                "keyword_only": 0, "avg_llm_confidence": 0, "avg_latency_ms": 0}
    llm_confirmed = sum(1 for l in _moderation_log if l.get("llm_result") == "violation")
    llm_rejected = sum(1 for l in _moderation_log if l.get("llm_result") == "normal")
    keyword_only = sum(1 for l in _moderation_log if l.get("source") == "keyword_only")
    avg_conf = sum(l.get("llm_confidence", 0) for l in _moderation_log if "llm_confidence" in l)
    llm_count = llm_confirmed + llm_rejected
    avg_latency = sum(l.get("latency_ms", 0) for l in _moderation_log if "latency_ms" in l)
    return {
        "total": total,
        "llm_confirmed": llm_confirmed,
        "llm_rejected": llm_rejected,
        "keyword_only": keyword_only,
        "avg_llm_confidence": round(avg_conf / max(llm_count, 1), 3),
        "avg_latency_ms": round(avg_latency / max(llm_count, 1), 0),
    }

# ═══════════════════════════════════════════════════════════
#  Prompt 构建
# ═══════════════════════════════════════════════════════════

def _build_moderation_prompt(
    raw_message: str,
    keyword_hits: list[dict],
    group_id: int = 0,
) -> str:
    """
    构建 LLM 审核 prompt，把 learner 学到的敏感词库实时注入。
    注入:
    - 当前消息
    - 命中的关键词及权重
    - 已学习的完整违规词库（按权重分级，这是核心——让 LLM 知道历史违规词）
    - 已学习的违规模式（正则）
    - 最近历史违规案例
    """
    # ── 获取已学习的完整关键词库（核心：实时喂给 LLM）──
    knowledge = learner._load_knowledge()
    learned_kw = knowledge.get("learned_keywords", {})
    # 按权重分三档：高危(≥3)、可疑(1-3)、轻度(≤1)
    high_risk = sorted([(k, w) for k, w in learned_kw.items() if w >= 3], key=lambda x: -x[1])[:40]
    medium_risk = sorted([(k, w) for k, w in learned_kw.items() if 1 <= w < 3], key=lambda x: -x[1])[:30]
    low_risk = sorted([(k, w) for k, w in learned_kw.items() if w < 1], key=lambda x: -x[1])[:20]

    def _fmt(words):
        return "、".join(k for k, _ in words) if words else "（无）"

    kw_lib_text = (
        f"  高危词（务必拦截）: {_fmt(high_risk)}\n"
        f"  可疑词（结合上下文判断）: {_fmt(medium_risk)}\n"
        f"  轻度词（单独出现不算违规）: {_fmt(low_risk)}"
    )

    # ── 学习到的违规模式（正则）──
    patterns = knowledge.get("learned_patterns", [])
    if patterns:
        top_patterns = sorted(patterns, key=lambda x: x.get("weight", 0), reverse=True)[:8]
        pat_text = "\n".join(
            f"  • {p.get('description', p.get('pattern', ''))} (权重{p.get('weight', 1):.1f})"
            for p in top_patterns
        )
    else:
        pat_text = "  （暂无）"

    # ── 命中的关键词 ──
    hit_text = ""
    if keyword_hits:
        hit_items = sorted(keyword_hits, key=lambda x: x.get("weight", 0), reverse=True)[:10]
        hit_text = "\n".join(
            f"  • {h['keyword']} (权重: {h.get('weight', 0):.1f})"
            for h in hit_items
        )

    # ── 最近违规案例 ──
    recent_cases_text = "暂无"
    try:
        recent = db.query_violations(group_id=group_id if group_id else None, limit=5)
        if recent:
            cases = []
            for r in recent[:3]:
                msg = r.get("raw_message", "")[:80]
                kws = r.get("keywords", [])
                kws_str = ", ".join(kws[:5]) if kws else "无"
                cases.append(f"  • 「{msg}」→ 违规 (关键词: {kws_str})")
            if cases:
                recent_cases_text = "\n".join(cases)
    except Exception:
        pass

    # ── 组装 prompt ──
    # ★ 缓存命中率优化：判断标准/排除规则/输出格式是每次审核都一样的稳定
    # 前缀，放最前面吃 DeepSeek/Anthropic 的前缀缓存；每次都变的词库/模式/
    # 命中/案例/待审消息全部放尾部。审核是全量抽样的高频调用，这个重排
    # 能把每次几百 token 的固定开销变成缓存命中价。
    prompt = f"""你是一个QQ群内容审核专家。请按以下标准判断最后给出的【待审核消息】是否违规。

【判断标准】
- 消息含"高危词"或命中多个"可疑词" → 很可能违规
- 只有明确的广告推广、色情低俗、赌博诈骗、辱骂攻击才算违规
- 正常的日常聊天、技术讨论、开玩笑不算违规
- 「收/出」类正常交易帖（收key、出账号、拼车、闲置出）是本群允许的正常业务，不算违规不算广告
- 如果消息同时满足多个条件（如既含敏感词又有链接/QQ号），更可能是违规
- 注意变形词和拼音替换（如"vx"="微信"、"jq"="加群"、emoji 替代敏感字）

【技术讨论排除 — 以下不算违规】
- API 使用方法、API Key 配置教程、开发者工具使用心得
- 开源项目链接（GitHub/GitLab/Gitee）、技术文档分享
- AI 模型讨论（GPT/Claude/DeepSeek 等）、编程问题求助
- 代码截图/报错求助、服务器配置讨论
- "xxx点xyz" "xxx.top" 等域名如果是技术服务官网，不算广告
- 关键词"api""key"在技术语境中极其常见，不单独构成广告信号
- 只有明确售卖/推广/付费引流 + 联系方式才算违规

【suggested_action 使用指南】
- "none": 完全正常
- "warn": 轻微可疑但可能是误判（如含1个广告词但整体是技术讨论）
- "mute": 明确违规广告，需要禁言处罚
- "kick": 严重违规或屡犯

请只输出一行JSON（不要markdown包裹）:
{{"is_violation": true或false, "reason": "15字内简述", "confidence": 0.00到1.00, "category": "ad/spam/abuse/porn/gamble/normal", "suggested_action": "none/warn/mute/kick"}}

—— 以下为本次审核的数据 ——

【已知敏感词库（从历史违规实时学习，权重越高越危险）】
{kw_lib_text}

【已知的违规模式】
{pat_text}

【本条消息命中的敏感词】
{hit_text if hit_text else "（无关键词直接命中，但仍需结合敏感词库和上下文判断）"}

【最近被标记的违规案例（参考）】
{recent_cases_text}

【待审核消息】
「{raw_message[:500]}」"""
    return prompt

# ═══════════════════════════════════════════════════════════
#  MiniMax API 调用
# ═══════════════════════════════════════════════════════════

async def _call_minimax_moderation(prompt: str) -> Optional[dict]:
    """调用 LLM 进行内容审核，返回解析后的 JSON（走统一 llm_create，带 429 重试）"""
    from . import llm_call, memory
    if llm_call.get_client() is None:
        logger.warning("LLM 客户端不可用，无法进行 LLM 审核")
        return None

    ms = memory.load_model_settings()
    try:
        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=200,
            temperature=0.05,  # 极低温度保证一致性
            messages=[{"role": "user", "content": prompt}],
        )

        result_text = ""
        for block in response.content:
            if block.type == "text":
                result_text += block.text
        result_text = result_text.strip()

        # 清理可能的 markdown 包裹
        result_text = re.sub(r"^```\w*\n?", "", result_text)
        result_text = re.sub(r"\n?```$", "", result_text)
        # 修复常见 JSON 问题
        result_text = result_text.replace("True", "true").replace("False", "false")

        parsed = json.loads(result_text)
        return {
            "is_violation": bool(parsed.get("is_violation", False)),
            "reason": str(parsed.get("reason", ""))[:50],
            "confidence": float(parsed.get("confidence", 0.5)),
            "category": str(parsed.get("category", "normal")),
            "suggested_action": str(parsed.get("suggested_action", "none")),
        }
    except json.JSONDecodeError:
        # 尝试从非 JSON 回复中提取判断
        text_lower = result_text.lower() if 'result_text' in dir() else ""
        is_v = "true" in text_lower and "violation" in text_lower
        return {
            "is_violation": is_v,
            "reason": "LLM返回格式异常",
            "confidence": 0.5,
            "category": "unknown",
            "suggested_action": "none",
        }
    except Exception as e:
        logger.warning(f"LLM 审核 API 调用失败: {e}")
        return None

# ═══════════════════════════════════════════════════════════
#  主审核流程
# ═══════════════════════════════════════════════════════════

async def llm_moderate(
    raw_message: str,
    group_id: int = 0,
    user_id: int = 0,
    force_llm: bool = False,
) -> dict:
    """
    LLM 增强审核：关键词初筛 → MiniMax 终审

    Args:
        raw_message: 原始消息文本
        group_id: 群号（用于获取相关案例）
        user_id: 发送者 QQ
        force_llm: 跳过关键词阈值判断，强制走 LLM

    Returns:
        {
            "is_violation": bool,        # 最终判定
            "confidence": float,          # 综合置信度 0-1
            "reason": str,                # 判定理由
            "source": str,                # "keyword_only" | "llm_confirmed" | "llm_rejected"
            "keywords_matched": [...],
            "llm_confidence": float,      # MiniMax 的置信度
            "llm_reason": str,            # MiniMax 的理由
            "category": str,              # 违规类型
            "suggested_action": str,      # 建议操作
            "latency_ms": float,          # LLM 调用耗时
        }
    """
    t0 = time.time()

    # ── 阶段 1: 关键词引擎评分 ──
    kw_result = learner.score_with_learning(raw_message)
    kw_confidence = kw_result["confidence"]
    kw_hits = kw_result.get("details", {}).get("keyword_hits", [])
    kw_patterns = kw_result.get("details", {}).get("pattern_hits", [])
    is_kw_violation = kw_result["is_violation"]
    threshold = kw_result["threshold"]

    # 清洗 CQ 码
    clean_msg = raw_message
    while True:
        new_text = re.sub(r"\[CQ:[^\]]*\]", "", clean_msg)
        if new_text == clean_msg:
            break
        clean_msg = new_text
    if not clean_msg.strip():
        return {
            "is_violation": False, "confidence": 0, "reason": "空消息",
            "source": "keyword_only", "keywords_matched": [],
            "llm_confidence": 0, "llm_reason": "", "category": "normal",
            "suggested_action": "none", "latency_ms": 0,
        }

    # ── 判断是否需要 LLM 审核 ──
    # 策略: 关键词置信度在"灰色区间"时送 LLM，高置信度直接判，低置信度直接放
    LLM_LOW_THRESHOLD = 0.25   # 低于此值不送 LLM（太正常了）
    LLM_HIGH_THRESHOLD = 0.85  # 高于此值直接判违规（太明显了）

    need_llm = force_llm or (
        kw_confidence >= LLM_LOW_THRESHOLD
        and kw_confidence < LLM_HIGH_THRESHOLD
        and len(clean_msg) >= 6
    )

    # 就算高于高阈值，如果没有足够多关键词命中（≥5个），也送 LLM 防误判
    if kw_confidence >= LLM_HIGH_THRESHOLD and len(kw_hits) < 5:
        need_llm = True

    # ── 默认结果 ──
    result = {
        "is_violation": is_kw_violation,
        "confidence": kw_confidence,
        "reason": f"关键词引擎判定 ({len(kw_hits)}个命中, {len(kw_patterns)}个模式)",
        "source": "keyword_only",
        "keywords_matched": [h.get("keyword", "") for h in kw_hits],
        "llm_confidence": 0,
        "llm_reason": "",
        "category": _categorize_hits([h.get("keyword", "") for h in kw_hits]),
        "suggested_action": "warn" if is_kw_violation else "none",
        "latency_ms": 0,
    }

    if not need_llm:
        latency = (time.time() - t0) * 1000
        result["latency_ms"] = round(latency, 1)
        _add_log({
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": clean_msg[:100],
            "group_id": group_id,
            "user_id": user_id,
            "kw_confidence": kw_confidence,
            "kw_hits_count": len(kw_hits),
            "kw_threshold": threshold,
            "llm_result": "skipped",
            "llm_confidence": 0,
            "final_result": "violation" if is_kw_violation else "normal",
            "source": "keyword_only",
            "latency_ms": result["latency_ms"],
        })
        return result

    # ── 阶段 2: MiniMax LLM 审核 ──
    prompt = _build_moderation_prompt(clean_msg, kw_hits, group_id)
    llm_result = await _call_minimax_moderation(prompt)

    latency = (time.time() - t0) * 1000

    if llm_result is None:
        # LLM 不可用 → 信任关键词引擎，但降低置信度
        result["source"] = "keyword_fallback"
        result["reason"] += " (LLM不可用，使用关键词结果)"
        _add_log({
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": clean_msg[:100], "group_id": group_id, "user_id": user_id,
            "kw_confidence": kw_confidence, "kw_hits_count": len(kw_hits),
            "kw_threshold": threshold,
            "llm_result": "error", "llm_confidence": 0,
            "final_result": "violation" if is_kw_violation else "normal",
            "source": "keyword_fallback", "latency_ms": round(latency, 1),
        })
        result["latency_ms"] = round(latency, 1)
        return result

    # ── 阶段 3: 融合判定 ──
    llm_is_violation = llm_result["is_violation"]
    llm_confidence = llm_result["confidence"]
    llm_reason = llm_result["reason"]
    llm_category = llm_result["category"]
    llm_action = llm_result["suggested_action"]

    # 综合置信度: 关键词70% + LLM30% 加权（LLM 权重更高）
    combined_confidence = round(kw_confidence * 0.3 + llm_confidence * 0.7, 4)

    # 最终判定: LLM 有否决权
    if llm_is_violation and is_kw_violation:
        # 双方一致 → 确认违规
        final_violation = True
        final_source = "llm_confirmed"
        final_reason = f"双重确认: {llm_reason}"
        final_action = llm_action if llm_action != "none" else ("mute" if combined_confidence >= 0.8 else "warn")
    elif llm_is_violation and not is_kw_violation:
        # LLM 认为是但关键词没命中 → LLM 发现新违规模式
        final_violation = True if llm_confidence >= 0.75 else False
        final_source = "llm_discovered" if final_violation else "llm_overruled"
        final_reason = f"LLM判定: {llm_reason}" if final_violation else f"LLM置信度不足: {llm_reason}"
        final_action = "warn" if final_violation else "none"
    elif not llm_is_violation and is_kw_violation:
        # 关键词误判 → LLM 纠正
        final_violation = False
        final_source = "llm_rejected"
        final_reason = f"LLM纠正确认正常: {llm_reason}"
        final_action = "none"
    else:
        # 双方都认为正常
        final_violation = False
        final_source = "llm_confirmed_normal"
        final_reason = f"确认正常: {llm_reason}"
        final_action = "none"

    # ── 反馈学习 ──
    if final_violation and final_source in ("llm_confirmed", "llm_discovered"):
        # LLM 确认违规 → 强化关键词学习
        try:
            import jieba
            words = [w for w in jieba.lcut(clean_msg) if len(w) >= 2 and not learner._is_junk_keyword(w)]
            if words:
                learner.learn_from_violation(clean_msg, words, source="auto")
        except Exception:
            pass

    result.update({
        "is_violation": final_violation,
        "confidence": combined_confidence,
        "reason": final_reason,
        "source": final_source,
        "llm_confidence": llm_confidence,
        "llm_reason": llm_reason,
        "category": llm_category if llm_category != "normal" else result["category"],
        "suggested_action": final_action,
        "latency_ms": round(latency, 1),
    })

    # ── 记录审核日志 ──
    _add_log({
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": clean_msg[:100],
        "group_id": group_id,
        "user_id": user_id,
        "kw_confidence": kw_confidence,
        "kw_hits_count": len(kw_hits),
        "kw_threshold": threshold,
        "llm_result": "violation" if llm_is_violation else "normal",
        "llm_confidence": llm_confidence,
        "llm_reason": llm_reason,
        "final_result": "violation" if final_violation else "normal",
        "source": final_source,
        "latency_ms": round(latency, 1),
    })

    return result

# ═══════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════

def _categorize_hits(keywords: list[str]) -> str:
    """根据命中关键词推断违规类型"""
    text = " ".join(keywords).lower()
    if any(k in text for k in ("裸聊", "约炮", "色情", "av", "番号")):
        return "porn"
    if any(k in text for k in ("赌博", "赌场", "六合彩", "彩票")):
        return "gamble"
    if any(k in text for k in ("傻逼", "sb", "脑残", "cnm", "操你妈", "nmsl", "畜生")):
        return "abuse"
    if any(k in text for k in ("广告", "推广", "引流", "加群", "加微信", "扫码", "免费",
                               "返利", "代理", "日赚", "刷单", "兼职")):
        return "ad"
    return "spam"

def is_moderation_enabled() -> bool:
    """检查 LLM 审核是否启用（统一看 api_key，不再区分 minimax_api_key）"""
    from . import memory
    ms = memory.load_model_settings()
    return ms.get("moderation_enabled", True) and bool(
        ms.get("api_key", "") or config.LLM_API_KEY
    )
