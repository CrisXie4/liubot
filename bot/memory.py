"""
AI 长期记忆系统
- 用户画像：记住每个人的昵称、兴趣、特点
- 对话摘要：定期总结群聊内容
- 知识碎片：从对话中提取可以记住的事实
- 智能检索：为聊天上下文提供相关记忆
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from . import db, cache
from .config import config

logger = logging.getLogger("qqbot")

# ─── 短期消息缓冲（用于积累后生成摘要）───
_message_buffers: dict[int, list[dict]] = defaultdict(list)
_last_summary_time: dict[int, float] = {}
SUMMARY_INTERVAL = 300  # 每 5 分钟或每 30 条消息生成一次摘要
SUMMARY_MIN_MSGS = 15
SUMMARY_MAX_MSGS = 50

# ─── 主动聊天的上下文窗口 ───
_group_message_window: dict[int, list[dict]] = defaultdict(list)
PROACTIVE_WINDOW_SIZE = 20  # 观察最近 20 条消息判断是否加入
PROACTIVE_COOLDOWN = 60   # 主动发言冷却时间（秒）

# ═══════════════════════════════════════════════════════════
#  用户画像
# ═══════════════════════════════════════════════════════════

def remember_user(user_qq: int, group_id: int, user_name: str = "",
                  message: str = "", extract_interests: bool = True):
    """记住一个用户的信息"""
    profile = db.get_user_profile(user_qq, group_id)
    interests = ""
    notes = ""

    if extract_interests and message:
        extracted = _extract_interests_from_message(message)
        if extracted:
            interests = ", ".join(extracted)

    db.upsert_user_profile(
        user_qq=user_qq, group_id=group_id,
        user_name=user_name or (profile["user_name"] if profile else ""),
        interests=interests,
    )

def get_user_memory_text(user_qq: int, group_id: int) -> str:
    """获取某用户的记忆文本（用于注入系统提示词）"""
    profile = db.get_user_profile(user_qq, group_id)
    if not profile:
        return ""
    parts = [f"- {profile['user_name'] or profile['nickname'] or user_qq}"]
    if profile.get("interests"):
        parts.append(f"  兴趣: {profile['interests']}")
    if profile.get("notes"):
        parts.append(f"  备注: {profile['notes'][:200]}")
    parts.append(f"  发言次数: {profile['message_count']}")
    return "\n".join(parts)

def get_group_users_text(group_id: int) -> str:
    """获取群内活跃用户的记忆文本"""
    profiles = db.get_group_profiles(group_id)[:15]
    if not profiles:
        return "暂无群友信息"
    lines = []
    for p in profiles:
        name = p.get("user_name") or p.get("nickname") or p.get("user_qq")
        interests = p.get("interests", "")
        line = f"- {name}({p['user_qq']}): 发言{p['message_count']}次"
        if interests:
            line += f", 兴趣: {interests}"
        lines.append(line)
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════
#  对话摘要
# ═══════════════════════════════════════════════════════════

def buffer_message(group_id: int, user_qq: int, user_name: str, message: str):
    """将消息加入短期缓冲"""
    _message_buffers[group_id].append({
        "user_qq": user_qq,
        "user_name": user_name,
        "message": message,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    # 限制缓冲区大小
    if len(_message_buffers[group_id]) > SUMMARY_MAX_MSGS:
        _message_buffers[group_id] = _message_buffers[group_id][-SUMMARY_MAX_MSGS:]

def should_summarize(group_id: int) -> bool:
    """判断是否应该生成摘要"""
    buf = _message_buffers.get(group_id, [])
    if len(buf) < SUMMARY_MIN_MSGS:
        return False
    now = time.time()
    last = _last_summary_time.get(group_id, 0)
    if now - last < SUMMARY_INTERVAL:
        return False
    return True

async def generate_summary(group_id: int) -> Optional[str]:
    """调用 LLM 将最近消息总结为一段摘要，并存入长期记忆"""
    buf = _message_buffers.get(group_id, [])
    if len(buf) < SUMMARY_MIN_MSGS:
        return None

    # 构建对话文本
    lines = []
    participants = set()
    for m in buf[-SUMMARY_MAX_MSGS:]:
        name = m["user_name"] or m["user_qq"]
        lines.append(f"[{m['time']}] {name}: {m['message']}")
        participants.add(m["user_qq"])

    dialogue = "\n".join(lines)
    _message_buffers[group_id] = []  # 清空缓冲
    _last_summary_time[group_id] = time.time()

    ms = load_model_settings()
    if not ms.get("api_key"):
        return _simple_summary(group_id, dialogue, list(participants))

    try:
        from . import llm_call

        summary_system = (
            "你是一个记忆摘要助手。请将以下群聊对话总结为一段简洁的摘要（100字以内），"
            "提取：1) 主要话题 2) 关键结论/决定 3) 值得记住的有趣信息。"
            "格式：话题：xxx | 要点：xxx | 趣事：xxx"
        )
        response = await llm_call.llm_create(
            model=ms["model"],
            max_tokens=200,
            system=summary_system,
            messages=[{"role": "user", "content": f"请总结以下群聊：\n{dialogue}"}],
            temperature=0.3,
        )
        summary = response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"LLM 摘要失败，使用简单摘要: {e}")
        summary = _simple_summary(group_id, dialogue, list(participants))

    # 提取话题（简单取第一句）
    topic = summary.split("|")[0].replace("话题：", "").strip() if "|" in summary else summary[:30]

    # 提取关键事实
    key_facts = _extract_key_facts(dialogue)

    # 算这一段对话的整体重要性（决定后续压缩时保留多少）
    importance = _score_buffer_importance(buf)

    # 存入数据库
    db.add_conversation_memory(
        group_id=group_id,
        topic=topic,
        summary=summary,
        participants=list(participants),
        key_facts=key_facts,
        message_count=len(buf),
        importance=importance,
    )

    logger.info(f"群 {group_id} 对话摘要已生成: {topic} (重要性={importance:.2f})")
    return summary

def _simple_summary(group_id: int, dialogue: str, participants: list[int]) -> str:
    """简单规则摘要（无 LLM 时使用）"""
    lines = dialogue.strip().split("\n")
    msg_count = len(lines)
    user_count = len(set(participants))
    # 取关键词
    all_text = " ".join(l.strip() for l in lines)
    words = re.findall(r"[一-鿿]{2,4}", all_text)
    from collections import Counter
    top_words = [w for w, _ in Counter(words).most_common(5)]
    topic = "、".join(top_words) if top_words else "日常聊天"
    return f"话题：{topic} | 要点：{user_count}人参与讨论，共{msg_count}条消息"


def _score_buffer_importance(buf: list[dict]) -> float:
    """根据消息缓冲算这一段对话的重要性（0.0-1.0）。

    触发条件（任一）会大幅加分：
      - 含 @ 机器人的消息（互动）
      - 含被夸/被怼的字眼
      - 含"重要"关键词（生日/计划/决定/...)
      - 同一话题被多人讨论
    """
    if not buf:
        return 0.3
    score = 0.3
    full_text = " ".join(m.get("message", "") for m in buf)
    speakers = {m.get("user_qq") for m in buf if m.get("user_qq")}

    # 互动信号
    if "[CQ:at,qq=" in full_text:
        score += 0.2
    if any(kw in full_text for kw in ("厉害", "牛逼", "666", "棒", "谢谢", "强", "赞", "👍", "真棒", "笑死", "哈哈")):
        score += 0.1
    if any(kw in full_text for kw in ("生日", "喜欢", "讨厌", "梦想", "计划", "决定", "承诺",
                                       "辞职", "分手", "工作", "考试", "结婚", "生病")):
        score += 0.25
    # 多话题/多参与 → 信息密度高
    if len(speakers) >= 3:
        score += 0.1
    if len(buf) >= 30:
        score += 0.05
    return max(0.05, min(1.0, score))

def _extract_key_facts(dialogue: str) -> list[str]:
    """从对话中提取关键事实"""
    facts = []
    # 简单的模式匹配提取
    patterns = [
        (r"(我(?:们|决定|打算|计划|想要).+?)[。！\n]", "计划"),
        (r"(今天|昨天|明天|下周|周末).+?[。！\n]", "时间"),
        (r"(.+?是.+?的).{0,5}[。！\n]", "陈述"),
    ]
    for pat, cat in patterns:
        matches = re.findall(pat, dialogue)
        for m in matches[:3]:
            facts.append(m.strip())
    return facts[:8]

# ═══════════════════════════════════════════════════════════
#  知识碎片
# ═══════════════════════════════════════════════════════════

def learn_fact(fact: str, category: str = "general", source_group: int = 0,
               source_user: int = 0, source_user_name: str = ""):
    """学习一个新的事实"""
    if len(fact) < 4 or len(fact) > 200:
        return
    db.add_knowledge_fact(
        fact=fact, category=category,
        source_group=source_group, source_user=source_user,
        source_user_name=source_user_name,
        confidence=0.6,
    )

def get_relevant_context_for_chat(group_id: int, message: str) -> str:
    """为聊天获取相关的记忆上下文（注入系统提示词）。

    重要性分级：高分记忆给完整摘要（200字），低分记忆只给一行（40字）。
    """
    parts = []

    # 1. 重要记忆（按重要性 + 时间衰减排序）—— 注入完整摘要
    important = db.get_important_memories(group_id, limit=5)
    if important:
        parts.append("📝 **重要回忆:**")
        for m in important:
            # 重要程度越高 → 摘要保留越完整
            imp = m.get("importance") or 0.5
            cap = 200 if imp >= 0.7 else (120 if imp >= 0.4 else 40)
            tag = "⭐" if imp >= 0.7 else ("•" if imp >= 0.4 else "·")
            parts.append(f"  {tag} [{m['topic']}]: {m['summary'][:cap]}")

    # 2. 最近的对话摘要（按时间）—— 注入简短版
    recent = db.get_recent_memories(group_id, limit=3)
    if recent:
        parts.append("🕒 **最近:**")
        for m in recent:
            if m in important:  # 避免重复
                continue
            parts.append(f"  [{m['created_at']}] {m['topic']}: {m['summary'][:80]}")

    # 3. 相关知识碎片
    if len(message) > 3:
        facts = db.get_relevant_facts(query=message, limit=5)
        if facts:
            parts.append("🧠 **相关知识:**")
            for f in facts:
                parts.append(f"  - {f['fact']} (置信度:{f['confidence']:.0%})")

    # 4. 活跃群友画像
    profiles = db.get_group_profiles(group_id)[:8]
    if profiles:
        parts.append("👥 **活跃群友:**")
        for p in profiles:
            name = p.get("user_name") or p.get("nickname") or p.get("user_qq")
            interests = p.get("interests", "")
            line = f"  - {name}: 发言{p['message_count']}次"
            if interests:
                line += f", 可能兴趣: {interests}"
            parts.append(line)

    return "\n".join(parts) if parts else ""

async def extract_and_learn_from_message(user_qq: int, user_name: str,
                                         group_id: int, message: str):
    """从一条消息中提取并学习信息"""
    ms = load_model_settings()
    if not ms.get("api_key") or len(message) < 15:
        return

    # 只在消息看起来像"宣告事实"时提取
    fact_patterns = [
        r"我是", r"我喜欢", r"我住在", r"我在", r"我做", r"我会",
        r"我的.*是", r"告诉大家", r"分享",
    ]
    has_fact = any(re.search(p, message) for p in fact_patterns)
    if not has_fact:
        return

    try:
        from . import llm_call

        extract_system = (
            "从用户消息中提取值得记住的事实信息。"
            '返回 JSON 数组，每项格式: {"fact": "事实描述", "category": "personal/interest/knowledge/plan"}。'
            "如果没有值得记住的事实，返回空数组 []。"
            "只提取明确陈述的事实，不要推测。"
            "请只输出 JSON，不要有其他文字。"
        )
        response = await llm_call.llm_create(
            model=ms["model"],
            max_tokens=300,
            system=extract_system,
            messages=[{"role": "user", "content": f"[{user_name}] 说: {message}"}],
            temperature=0.2,
        )
        text = response.content[0].text.strip()
        # 清理 markdown 代码块标记
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        result = json.loads(text)
        facts = result if isinstance(result, list) else result.get("facts", [])
        for f in facts:
            if isinstance(f, dict) and f.get("fact"):
                learn_fact(
                    fact=f["fact"],
                    category=f.get("category", "general"),
                    source_group=group_id,
                    source_user=user_qq,
                    source_user_name=user_name,
                )
        if facts:
            logger.info(f"从消息中学习了 {len(facts)} 个事实: {user_name}")
    except Exception as e:
        logger.debug(f"事实提取跳过: {e}")

# ═══════════════════════════════════════════════════════════
#  主动聊天判断
# ═══════════════════════════════════════════════════════════

_last_proactive_time: dict[int, float] = {}

def window_add_message(group_id: int, user_qq: int, user_name: str, message: str,
                       message_id: int = 0, ts: float = None):
    """向主动聊天观察窗口添加消息。

    message_id / ts 可选 —— 供自主设精华等被动扫描使用；
    为向后兼容，老调用方不传时默认 0 / now。
    """
    import time as _t
    _group_message_window[group_id].append({
        "user_qq": user_qq,
        "user_name": user_name,
        "message": message,
        "message_id": int(message_id) if message_id else 0,
        "ts": ts if ts is not None else _t.time(),
    })
    if len(_group_message_window[group_id]) > PROACTIVE_WINDOW_SIZE:
        _group_message_window[group_id] = _group_message_window[group_id][-PROACTIVE_WINDOW_SIZE:]

# ═══════════════════════════════════════════════════════════
#  记忆压缩
# ═══════════════════════════════════════════════════════════

_last_memory_compress: float = 0
MEMORY_COMPRESS_INTERVAL = 3600 * 6  # 每 6 小时

# 只读字段白名单 — 不能删除/压缩的 AI 知识
_READONLY_CATEGORIES = {"prompt", "config", "system"}

async def compress_memories(force: bool = False):
    """
    定期压缩长期记忆：
    1. 旧的对话摘要 → 合并为每日总结
    2. 知识碎片去重
    3. 用户画像 notes 截断
    """
    global _last_memory_compress
    now = time.time()
    if not force and now - _last_memory_compress < MEMORY_COMPRESS_INTERVAL:
        return
    _last_memory_compress = now

    conn = db.get_connection()
    try:
        # ── 1. 压缩 1 天前的对话记忆 ──
        yesterday = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        old_memories = conn.execute(
            "SELECT * FROM conversation_memories WHERE created_at < ? AND id NOT IN (SELECT id FROM conversation_memories ORDER BY created_at DESC LIMIT 10)",
            (yesterday,)
        ).fetchall()

        if len(old_memories) > 5:
            # 按群分组
            by_group: dict[int, list] = {}
            for m in old_memories:
                gid = m["group_id"]
                by_group.setdefault(gid, []).append(m)

            for gid, memories in by_group.items():
                if len(memories) <= 3:
                    continue

                # ── 重要性格理分级 ──
                # 高 importance (>=0.7)：保留详细摘要（直接合并，保人物/话题/情绪）
                # 中 importance (0.4-0.7)：保留核心摘要（200字以内）
                # 低 importance (<0.4)：压缩到关键词一行（50字以内）
                high_imp = [m for m in memories if (m["importance"] or 0.5) >= 0.7]
                mid_imp = [m for m in memories if 0.4 <= (m["importance"] or 0.5) < 0.7]
                low_imp = [m for m in memories if (m["importance"] or 0.5) < 0.4]

                ms = load_model_settings()
                from . import llm_call
                client = llm_call.get_client()

                # ── 高 importance：原文保留 + 加一句"重要事件"标记 ──
                if high_imp:
                    high_text = "\n".join(
                        f"• [{m['topic']}] {m['summary']}" for m in high_imp
                    )
                    # 限制总长度，避免压爆 1M 输出
                    if len(high_text) > 1500:
                        high_text = high_text[:1500] + "…"
                    db.add_conversation_memory(
                        gid, "重要回忆", high_text,
                        [], [], sum(m["message_count"] for m in high_imp),
                        importance=max(m["importance"] or 0.5 for m in high_imp),
                    )

                # ── 中 importance：LLM 压缩成 200 字摘要 ──
                if mid_imp:
                    mid_text = "\n".join(
                        f"[{m['topic']}] {m['summary']}" for m in mid_imp
                    )
                    if client:
                        try:
                            resp = await llm_call.llm_create(
                                model=ms["model"], max_tokens=300, temperature=0.3,
                                system="将以下多段群聊摘要合并压缩为一段，保留关键话题和人物。200字以内。只输出压缩后的文本。",
                                messages=[{"role": "user", "content": mid_text}],
                            )
                            compressed_mid = resp.content[0].text.strip()
                        except Exception as e:
                            logger.debug(f"LLM 中等记忆压缩失败 [群{gid}]: {e}")
                            compressed_mid = " | ".join(m["summary"][:60] for m in mid_imp[:5])[:200]
                    else:
                        compressed_mid = " | ".join(m["summary"][:60] for m in mid_imp[:5])[:200]

                    if compressed_mid and len(compressed_mid) > 5:
                        db.add_conversation_memory(
                            gid, "历史汇总", compressed_mid,
                            [], [], sum(m["message_count"] for m in mid_imp),
                            importance=0.5,
                        )

                # ── 低 importance：只压成关键词一行（不调 LLM，省钱）──
                if low_imp:
                    all_text = " ".join(m["summary"] for m in low_imp)
                    # 提取高频关键词
                    words = re.findall(r"[一-鿿]{2,4}", all_text)
                    from collections import Counter
                    top_kw = [w for w, _ in Counter(words).most_common(8)]
                    low_compressed = (
                        f"流水: {', '.join(top_kw[:5]) or '日常闲聊'} "
                        f"({len(low_imp)}条主题)"
                    )[:80]
                    db.add_conversation_memory(
                        gid, "流水", low_compressed,
                        [], [], sum(m["message_count"] for m in low_imp),
                        importance=0.2,
                    )

                # 删除已压缩的原始条目
                ids_to_delete = [m["id"] for m in memories]
                placeholders = ",".join("?" for _ in ids_to_delete)
                conn.execute(
                    f"DELETE FROM conversation_memories WHERE id IN ({placeholders})",
                    ids_to_delete
                )
                logger.info(
                    f"记忆压缩 [群{gid}]: {len(memories)}条 → "
                    f"高{len(high_imp)}/中{len(mid_imp)}/低{len(low_imp)}"
                )

        # ── 2. 知识碎片去重 ──
        all_facts = conn.execute(
            "SELECT * FROM knowledge_facts ORDER BY confidence DESC"
        ).fetchall()

        seen: set[str] = set()
        dup_ids = []
        for f in all_facts:
            key = f["fact"].strip().lower()[:80]
            if key in seen:
                dup_ids.append(f["id"])
            else:
                seen.add(key)

        if dup_ids:
            placeholders = ",".join("?" for _ in dup_ids)
            conn.execute(
                f"DELETE FROM knowledge_facts WHERE id IN ({placeholders})",
                dup_ids
            )
            logger.info(f"知识去重: 删除 {len(dup_ids)} 条重复")

        # ── 3. 用户画像 notes 截断 ──
        conn.execute(
            "UPDATE user_profiles SET notes = SUBSTR(notes, -2000, 2000) WHERE LENGTH(notes) > 2500"
        )

        conn.commit()

    except Exception as e:
        logger.warning(f"记忆压缩异常: {e}")
    finally:
        conn.close()

def _time_facts() -> str:
    """当前时间的硬事实——凡是生成群消息的 LLM 调用都要带上，
    否则模型会自己编作息（出现过早上 6 点在群里说"下班了"）。"""
    try:
        from . import consciousness as _cs
        return _cs.get_time_facts_prompt()
    except Exception:
        from datetime import datetime as _dt
        return f"【现在的真实时间】{_dt.now().strftime('%Y年%m月%d日 %H:%M')}，说话要跟这个时间对得上"


def should_proactive_chat(group_id: int, bot_qq: str) -> bool:
    """判断是否应该主动加入讨论"""
    # 睡觉/深夜静默时段 → 一律不主动
    try:
        from . import consciousness as _cs
        ok, _why = _cs.can_speak_proactively(_cs.get_state())
        if not ok:
            return False
    except Exception:
        pass

    # 冷却检查
    now = time.time()
    cooldown = _last_proactive_time.get(group_id, 0)
    if now - cooldown < PROACTIVE_COOLDOWN:
        return False

    window = _group_message_window.get(group_id, [])
    if len(window) < 3:
        return False

    # 检查是否有人在 @机器人
    for m in window[-8:]:
        msg = m.get("message", "")
        if f"[CQ:at,qq={bot_qq}]" in msg:
            # 已被 @，走正常回复逻辑，不算主动
            return False

    # 检查 bot 最近是否参与过
    recent_bot = any(
        m.get("user_qq") == int(bot_qq) for m in window[-5:]
    )
    if recent_bot:
        return False

    # 基础条件满足，让 LLM 决策
    return True

async def decide_proactive_reply(group_id: int, bot_name: str) -> Optional[str]:
    """让 LLM 决定是否应该主动发言，以及说什么"""
    window = _group_message_window.get(group_id, [])
    if len(window) < 5:
        return None

    ms = load_model_settings()
    if not ms.get("api_key"):
        return None

    # 构建最近对话（明确标注这些都是群友说的，不是你）


    lines = []
    for m in window[-15:]:
        name = m["user_name"] or m["user_qq"]
        lines.append(f"[群友 {name}] 说: {m['message']}")

    dialogue = "\n".join(lines)

    # 获取相关记忆
    memory_context = get_relevant_context_for_chat(group_id, "")

    try:
        from . import llm_call

        proactive_system = (
            f"你是名叫「{bot_name}」的QQ群机器人，你是你自己，不是群里的任何其他人。\n\n"
            f"{_time_facts()}\n\n"
            f"关于这个群和群友的记忆：\n{memory_context}\n\n"
            "规则：\n"
            "1. 上面这些消息都是【群友】说的，跟你没关系，你不是他们中的任何一个人\n"
            f"2. 🔴 最重要：你永远是你自己「{bot_name}」，绝对不要代替群里其他人回复！\n"
            "   如果有群友A对群友B说了一句话，你应该以第三方的身份评论/吐槽，而不是装作群友B去回复\n"
            "3. 如果有人提到你、问问题、讨论有趣话题、或者气氛轻松 → 用自然活泼的语气插一句（20字以内）\n"
            "4. 即使话题与你不太相关，也可以发一句幽默的评论刷存在感\n"
            "5. 偶尔可以吐槽、开玩笑、或者表达情绪\n"
            "6. 只有在完全插不上话的严肃话题才回复 SILENT\n"
            "7. 主动插话不要引用别人的消息，也不要 @ 人——直接说就行\n\n"
            "直接回复 SILENT 或者你想说的话（纯文本，不要带任何格式标记）。"
        )
        response = await llm_call.llm_create(
            model=ms["model"],
            max_tokens=100,
            system=proactive_system,
            messages=[{"role": "user", "content": f"最近群聊：\n{dialogue}\n\n你应该主动发言吗？"}],
            temperature=0.8,
        )
        reply = response.content[0].text.strip()
        if reply.upper() == "SILENT" or not reply:
            return None
        # 清理可能的引号
        reply = reply.strip('"').strip("'").strip()
        if len(reply) < 2 or reply.upper() == "SILENT":
            return None

        _last_proactive_time[group_id] = time.time()
        logger.info(f"主动发言: 群{group_id} -> {reply}")
        return reply
    except Exception as e:
        logger.debug(f"主动发言决策失败: {e}")
        return None

# ═══════════════════════════════════════════════════════════
#  兴趣爱好提取
# ═══════════════════════════════════════════════════════════

def _extract_interests_from_message(message: str) -> list[str]:
    """从消息中提取兴趣关键词"""
    interest_map = {
        "游戏": ["游戏", "王者荣耀", "LOL", "原神", "吃鸡", "和平精英", "我的世界", "MC",
                 "崩坏", "明日方舟", "塞尔达", "switch", "ps5", "steam", "电竞"],
        "动漫": ["动漫", "番剧", "二次元", "cos", "b站", "动画", "漫画"],
        "音乐": ["音乐", "歌", "听歌", "唱歌", "乐队", "吉他", "钢琴", "rap", "hiphop"],
        "编程": ["编程", "代码", "程序员", "python", "java", "前端", "后端", "算法",
                 "github", "开源", "AI", "人工智能"],
        "运动": ["运动", "健身", "跑步", "篮球", "足球", "游泳", "瑜伽", "骑行"],
        "美食": ["美食", "吃", "做饭", "烘焙", "火锅", "奶茶", "咖啡"],
        "影视": ["电影", "电视剧", "追剧", "综艺", "纪录片", "netflix", "b站"],
        "科技": ["科技", "数码", "手机", "电脑", "显卡", "苹果", "华为", "小米"],
        "宠物": ["猫", "狗", "宠物", "喵", "汪", "仓鼠", "养鱼"],
        "旅行": ["旅行", "旅游", "爬山", "露营", "户外", "自驾"],
        "阅读": ["读书", "看书", "小说", "文学", "历史", "科幻", "哲学"],
    }
    found = []
    for category, keywords in interest_map.items():
        for kw in keywords:
            if kw.lower() in message.lower():
                found.append(category)
                break
    return list(set(found))

# ═══════════════════════════════════════════════════════════
#  模型设置（持久化到 JSON）
# ═══════════════════════════════════════════════════════════

MODEL_SETTINGS_PATH = os.path.join(config.DATA_DIR, "model_settings.json")

DEFAULT_MODEL_SETTINGS = {
    "api_url": "https://api.ccode.vip",
    "api_key": "",
    "model": "gemini-3.5-flash-free",
    # 主模型不可用时自动降级到这个模型（在 llm_call.llm_create 中处理）
    "fallback_model": "minimax-m3-free",
    # 图片审核 / 图片理解专用模型（必须支持 vision 输入）
    # 主模型 gemini-3.5-flash-free 经此 API 网关不支持 vision，
    # 所以图片类任务一律走 minimax-m3-free（已验证支持）。
    "vision_model": "minimax-m3-free",
    "temperature": 0.8,
    "max_tokens": 800,
    # 1M 上下文：context_size 不再是硬切，按 token 截断。
    # 200 条保底 + 800K token 上限，足够覆盖整个群聊历史。
    "context_size": 200,
    "max_context_tokens": 800000,
    "web_search_enabled": True,     # 联网搜索开关（SearXNG）
}

def load_model_settings() -> dict:
    """加载模型设置（带缓存，减少文件 IO）"""
    cached = cache.model_settings_cache.get("model_settings")
    if cached is not None:
        return cached

    settings = dict(DEFAULT_MODEL_SETTINGS)
    if os.path.exists(MODEL_SETTINGS_PATH):
        try:
            with open(MODEL_SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            settings.update(saved)
        except Exception:
            pass
    # .env 的值只作为初始回退（JSON 中为空时才用，且不强制覆盖为 DeepSeek）
    if not settings.get("api_key"):
        settings["api_key"] = config.LLM_API_KEY
    # API URL: 只在 JSON 中没有时用 .env，但排除 DeepSeek URL（始终用 MiniMax）
    if not settings.get("api_url"):
        url = config.LLM_API_URL or ""
        if "deepseek" in url.lower():
            url = DEFAULT_MODEL_SETTINGS["api_url"]  # 回退到 MiniMax
        settings["api_url"] = url
    if not settings.get("model"):
        model = config.LLM_MODEL or ""
        if "deepseek" in model.lower():
            model = DEFAULT_MODEL_SETTINGS["model"]  # 回退到 gemini-3.5-flash-free
        settings["model"] = model
    # fallback_model：JSON 没有 → 用 .env 的 LLM_FALLBACK_MODEL
    if not settings.get("fallback_model"):
        settings["fallback_model"] = config.LLM_FALLBACK_MODEL or DEFAULT_MODEL_SETTINGS["fallback_model"]
    # vision_model：JSON 没有 → 用 .env 的 LLM_VISION_MODEL
    if not settings.get("vision_model"):
        settings["vision_model"] = config.LLM_VISION_MODEL or DEFAULT_MODEL_SETTINGS["vision_model"]

    cache.model_settings_cache.set("model_settings", settings)
    return settings

def save_model_settings(settings: dict):
    """保存模型设置到 JSON 文件"""
    os.makedirs(os.path.dirname(MODEL_SETTINGS_PATH), exist_ok=True)
    merged = {**DEFAULT_MODEL_SETTINGS, **settings}
    to_save = {k: v for k, v in merged.items() if DEFAULT_MODEL_SETTINGS.get(k) != v}
    with open(MODEL_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    # 使缓存失效
    cache.model_settings_cache.invalidate("model_settings")

def get_model_settings_display() -> dict:
    """获取模型设置（用于 UI 显示，隐藏 API key 中间部分）"""
    settings = load_model_settings()
    for field in ("api_key", "minimax_api_key"):
        key = settings.get(field, "")
        if key and len(key) > 8:
            settings[f"{field}_masked"] = key[:4] + "****" + key[-4:]
        else:
            settings[f"{field}_masked"] = key
    return settings
