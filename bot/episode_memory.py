"""
双层事件记忆（精髓移植自 AgentGal）
====================================
借鉴 AgentGal 的记忆架构，适配 QQ 群聊场景：

1. 双层记忆：
   - episodes     结构化事件记忆（date/participants/keywords/importance 1-5/title/content）
   - understandings 对群友的稳定认知（subject/content + linked_episodes + 演化历史）

2. 闭合检测整合：不按固定条数硬切摘要，等话题收束才整合；
   未收束的消息留在缓冲区，下次接着看（AgentGal 的 memory_draft 语义）。

3. 回想强化检索：词法相关度（jieba）+ importance + 时间新近度融合打分；
   命中后刷新 last_recalled_at（AgentGal 的 hybrid retrieval + recall 强化）。

与 AgentGal 的差异（场景适配）：
- 群聊是单一视角（bot 是旁观者/参与者），不做 per-character 记忆与 visible_to
- 无 embedding 服务，AgentGal 的向量+BM25 融合简化为 jieba 词法融合
- 闭合检测与事件生成合并为一次 LLM 调用（省成本），understanding patch 独立一次
- LLM 不可用/解析失败时退回旧的窗口摘要（memory.generate_summary），不丢消息
"""
import json
import logging
import re
import time
from datetime import datetime

from . import db
from .config import config

logger = logging.getLogger("qqbot")

# 检索注入量：宁少勿滥，_prompt 预算有限
EPISODE_INJECT_LIMIT = 3
UNDERSTANDING_INJECT_LIMIT = 5
EPISODE_MIN_SCORE = 0.28
UNDERSTANDING_MIN_SCORE = 0.22

_STOPWORDS = {
    "的", "了", "是", "在", "和", "有", "就", "不", "也", "都", "这", "那", "我",
    "你", "他", "她", "它", "们", "个", "把", "被", "从", "到", "对", "让", "给",
    "跟", "为", "以", "可以", "没有", "知道", "觉得", "一个", "这个", "那个",
    "什么", "怎么", "如何", "还是", "但是", "然后", "所以", "因为", "如果",
    "哈哈", "哈哈哈", "嗯嗯", "哦哦", "好", "好的", "行", "嗯", "哦", "啊",
}


def _tokenize(text: str) -> set[str]:
    """jieba 分词，去停用词/单字/纯数字，供词法匹配。"""
    try:
        import jieba
        words = jieba.lcut(text or "")
    except Exception:
        words = re.findall(r"[一-鿿]{2,}|[a-zA-Z]{2,}", text or "")
    return {
        w.strip().lower() for w in words
        if len(w.strip()) >= 2 and not w.strip().isdigit()
        and w.strip().lower() not in _STOPWORDS
    }


# ═══════════════════════════════════════════════════════════
#  混合检索（词法相关度 × importance × 时间新近度）
# ═══════════════════════════════════════════════════════════

def _recency_factor(ts: float, half_life_days: float = 14) -> float:
    """时间新近度：半衰期默认 14 天。"""
    days = max(0.0, (time.time() - ts) / 86400)
    return 1.0 / (1.0 + days / half_life_days)


def _parse_dt(s: str) -> float:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def score_episode(ep: dict, query_tokens: set[str]) -> float:
    """事件记忆打分：词法重叠为主，importance 与新近度分层叠加。

    借鉴 AgentGal 的 fusion→recency 分层：相关度决定"能不能想起来"，
    importance 决定"值不值得想起来"，recency 做最后微调。
    """
    text_tokens = _tokenize(
        " ".join([ep.get("title", ""), ep.get("content", ""),
                  " ".join(ep.get("keywords", [])),
                  ep.get("participants", "")])
    )
    if query_tokens and text_tokens:
        overlap = len(query_tokens & text_tokens) / len(query_tokens)
    else:
        overlap = 0.0
    importance_raw = ep.get("importance") or 3
    importance = importance_raw / 5.0
    recency = _recency_factor(_parse_dt(ep.get("created_at", "")))
    score = 0.55 * overlap + 0.25 * importance + 0.20 * recency
    # 高重要度保底：重大事件（5分）不该因为词法不匹配而被彻底遗忘
    if importance_raw >= 5:
        score = max(score, EPISODE_MIN_SCORE)
    return score


def score_understanding(u: dict, query_tokens: set[str]) -> float:
    """稳定认知打分：subject/keywords 命中权重高于 content 命中。"""
    subject_tokens = _tokenize(" ".join([u.get("subject", ""),
                                         " ".join(u.get("keywords", []))]))
    content_tokens = _tokenize(u.get("content", ""))
    if query_tokens:
        subj_hit = len(query_tokens & subject_tokens) / len(query_tokens)
        cont_hit = len(query_tokens & content_tokens) / len(query_tokens)
    else:
        subj_hit = cont_hit = 0.0
    # 被反复回忆起的认知更可靠（回想强化的正循环）
    recall_boost = min(0.1, (u.get("recall_count") or 0) * 0.01)
    recency = _recency_factor(_parse_dt(u.get("updated_at", "")))
    return 0.45 * subj_hit + 0.20 * cont_hit + 0.15 * recency + recall_boost


def retrieve_relevant_episodes(group_id: int, message: str,
                               limit: int = EPISODE_INJECT_LIMIT) -> list[dict]:
    """检索与当前消息相关的事件记忆，按融合分排序。"""
    episodes = db.get_episodes(group_id)
    if not episodes:
        return []
    query_tokens = _tokenize(message)
    scored = [(score_episode(ep, query_tokens), ep) for ep in episodes]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ep for score, ep in scored[:limit] if score >= EPISODE_MIN_SCORE]


def retrieve_relevant_understandings(group_id: int, message: str,
                                     limit: int = UNDERSTANDING_INJECT_LIMIT) -> list[dict]:
    """检索与当前消息相关的稳定认知。"""
    us = db.get_understandings(group_id)
    if not us:
        return []
    query_tokens = _tokenize(message)
    scored = [(score_understanding(u, query_tokens), u) for u in us]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [u for score, u in scored[:limit] if score >= UNDERSTANDING_MIN_SCORE]


def render_episodes_block(episodes: list[dict]) -> str:
    """渲染注入 prompt 的事件记忆块。"""
    if not episodes:
        return ""
    lines = []
    for ep in episodes:
        imp = "★" * min(5, max(1, ep.get("importance") or 3))
        when = ep.get("date") or ""
        lines.append(f"  [{ep.get('title', '')}]({when} {imp}) {ep.get('content', '')}")
    return "\n".join(lines)


def render_understandings_block(understandings: list[dict]) -> str:
    """渲染注入 prompt 的稳定认知块。"""
    if not understandings:
        return ""
    return "\n".join(
        f"  - {u.get('subject', '')}: {u.get('content', '')}"
        for u in understandings
    )


# ═══════════════════════════════════════════════════════════
#  整合流水线：闭合检测 → 事件生成 → 认知更新
# ═══════════════════════════════════════════════════════════

_CONSOLIDATE_PROMPT = (
    "你是群聊记忆整理器。阅读一段群聊记录，判断其中的话题是否已经收束"
    "（聊出了一个完整的事），把收束的部分整理成一条可长期回忆的事件。\n\n"
    "<规则>\n"
    "- 收束 = 话题已聊出结果：解决/结束/达成共识/约定成立/自然淡出转向新话题\n"
    "- 只整合已收束的部分；还在进行中的话题留给以后。closed_until = 收束部分"
    "最后一条消息的编号；整段都在聊一个未完的话题时输出 0\n"
    "- content 以旁观视角写清：谁、发生了什么、结果如何，100字内\n"
    "- keywords 写 4-8 个词：人名、话题词、结论性动作。以后群聊出现这些词时"
    "应该能想起这条记忆\n"
    "- importance 1-5：\n"
    "  1 = 日常寒暄水聊，删掉不影响理解群里的人\n"
    "  2 = 又一次日常互动，模式重复\n"
    "  3 = 新信息：新事实/新偏好/新计划，以后可能被提起\n"
    "  4 = 重要变化：明确约定、关系变化、重要决定\n"
    "  5 = 关键事件：重大冲突/重大和解/重要信息暴露/里程碑\n"
    "</规则>\n\n"
    "<输出格式>\n"
    "只输出合法 JSON，不要其他文字：\n"
    '{"closed_until": 整数, "date": "X月X日", "time": "HH:MM或时段", '
    '"participants": "人名1、人名2", "keywords": ["词1","词2"], '
    '"importance": 1到5, "title": "8-16字短标题", "content": "一段话"}\n'
    '没有收束话题时只输出：{"closed_until": 0}\n'
    "</输出格式>"
)

_UNDERSTANDING_PROMPT = (
    "你维护一个QQ群机器人对群友的稳定认知。根据刚记录的事件，"
    "更新已有认知或形成新认知。\n\n"
    "<规则>\n"
    "- 认知是稳定结论，不是事件复述。例：「张三深夜爱聊技术」「李四说话冲但心软」\n"
    "- 对每条想写的认知，先在 existing 里找同主题：\n"
    "  - 找到且新事件修正/深化了它 → update，content 重新表述为兼顾新旧信息的连贯结论\n"
    "  - 找到但只是再次印证 → update，只输出 subject 字段\n"
    "  - 没有 → add 新认知\n"
    "- subject 指向人/关系/相处模式，不锚定具体事件"
    "（不写「张三昨天吵架的表现」，写「张三在冲突时的样子」）\n"
    "- content 100字内，只写结论；keywords 3-6 个，描述以后什么情境该想起它\n"
    "- 跟群友无关的事件（纯灌水/没有可沉淀结论）输出空 add 和空 update\n"
    "</规则>\n\n"
    "<输出格式>\n"
    "严格 JSON：\n"
    '{"add": [{"subject": "...", "keywords": ["词1"], "content": "..."}], '
    '"update": {"<id>": {"subject": "...", "keywords": ["..."], "content": "..."}}}\n'
    "update 的字段可选；只印证时只输出 subject。\n"
    "</输出格式>"
)


def _extract_json(text: str):
    """从 LLM 输出中提取 JSON 对象（容忍 ``` 代码块和前后杂文本）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"输出里没有 JSON: {text[:80]}")
    return json.loads(text[start:end + 1])


def _clamp_importance(v) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 3


async def _consolidate_episode(group_id: int, dialogue: str, window: list[dict]) -> dict:
    """一次 LLM 调用完成闭合检测 + 事件生成。

    返回 episode dict（含 closed_until）；没有收束话题时 closed_until=0。
    抛异常表示 LLM/网络失败，由上层退回旧摘要。
    """
    from . import llm_call, memory

    ms = memory.load_model_settings()
    resp = await llm_call.llm_create(
        model=ms.get("model", "gemini-3.5-flash-free"),
        max_tokens=400,
        temperature=0.2,
        system=_CONSOLIDATE_PROMPT,
        messages=[{"role": "user", "content": f"群聊记录：\n{dialogue}"}],
    )
    data = _extract_json(resp.content[0].text)
    closed_until = int(data.get("closed_until") or 0)
    if closed_until <= 0:
        return {"closed_until": 0}
    closed_until = min(closed_until, len(window))
    now_cn = datetime.now()
    return {
        "closed_until": closed_until,
        "date": str(data.get("date") or f"{now_cn.month}月{now_cn.day}日"),
        "time": str(data.get("time") or now_cn.strftime("%H:%M")),
        "participants": str(data.get("participants") or "")[:100],
        "keywords": [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:8],
        "importance": _clamp_importance(data.get("importance")),
        "title": str(data.get("title") or "群聊事件")[:30],
        "content": str(data.get("content") or "").strip()[:300],
    }


async def _patch_understandings(group_id: int, episode_id: str, episode: dict):
    """把新事件沉淀为对群友的稳定认知（AgentGal Understanding Patch）。"""
    from . import llm_call, memory

    existing = db.get_understandings(group_id, limit=30)
    existing_text = "\n".join(
        f"[{u['id']}] subject={u['subject']} content={u['content']}"
        for u in existing
    ) or "（暂无）"

    new_record = (
        f"id={episode_id}，标题：{episode['title']}。"
        f"内容：{episode['content']}"
    )

    ms = memory.load_model_settings()
    resp = await llm_call.llm_create(
        model=ms.get("model", "gemini-3.5-flash-free"),
        max_tokens=400,
        temperature=0.2,
        system=_UNDERSTANDING_PROMPT,
        messages=[{"role": "user", "content":
                   f"<existing>\n{existing_text}\n</existing>\n\n"
                   f"<new_record>\n{new_record}\n</new_record>"}],
    )
    data = _extract_json(resp.content[0].text)

    known_ids = {u["id"] for u in existing}
    applied = 0
    for item in (data.get("add") or []):
        if not isinstance(item, dict) or not item.get("subject"):
            continue
        db.add_understanding(
            group_id=group_id,
            subject=str(item["subject"])[:60],
            keywords=[str(k) for k in (item.get("keywords") or [])][:6],
            content=str(item.get("content") or "")[:200],
            linked_episode=episode_id,
        )
        applied += 1
    for uid, patch in (data.get("update") or {}).items():
        if uid not in known_ids or not isinstance(patch, dict):
            continue
        db.update_understanding(
            understanding_id=uid,
            subject=str(patch.get("subject") or "")[:60] or "",
            keywords=[str(k) for k in (patch.get("keywords") or [])][:6] or None,
            content=str(patch.get("content") or "")[:200],
            linked_episode=episode_id,
            history_title=episode["title"],
        )
        applied += 1
    return applied


async def maybe_consolidate(group_id: int, force: bool = False) -> bool:
    """整合入口：闭合才整合，未闭合的留在缓冲区。

    返回 True = 本次消息已被结构化整合（或确认无话题可整合）；
    返回 False = 交给上层退回旧摘要（LLM 不可用 / 缓冲满仓且无闭合）。
    """
    from . import memory

    buf = memory._message_buffers.get(group_id, [])
    if len(buf) < memory.SUMMARY_MIN_MSGS and not force:
        return False
    if len(buf) < 3:
        return False

    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return False

    # 整合窗口取 buffer 尾部，逐条编号（AgentGal 的 turn 语义）
    window = buf[-memory.SUMMARY_MAX_MSGS:]
    lines = []
    for i, m in enumerate(window, 1):
        name = m.get("user_name") or m.get("user_qq")
        lines.append(f"[{i}] {m.get('time', '')} {name}: {m.get('message', '')}")
    dialogue = "\n".join(lines)

    # 每次"尝试"都刷新节流时间，防止每条消息都重试 LLM
    memory._last_summary_time[group_id] = time.time()

    try:
        episode = await _consolidate_episode(group_id, dialogue, window)
    except Exception as e:
        logger.warning(f"事件整合失败 [群{group_id}]，退回旧摘要: {e}")
        return False

    if episode.get("closed_until", 0) <= 0:
        if force or len(buf) >= memory.SUMMARY_MAX_MSGS:
            # 手动整合 / 缓冲满仓仍无闭合 → 退回旧摘要，防止无限积压
            logger.info(f"[群{group_id}] 无闭合话题且缓冲满({len(buf)})，退回旧摘要")
            return False
        logger.debug(f"[群{group_id}] 暂无收束话题，{len(buf)} 条消息留在缓冲区")
        return True

    eid = db.add_episode(
        group_id=group_id,
        date=episode["date"], time=episode["time"],
        participants=episode["participants"],
        keywords=episode["keywords"],
        importance=episode["importance"],
        title=episode["title"], content=episode["content"],
        message_count=episode["closed_until"],
    )

    applied = 0
    try:
        applied = await _patch_understandings(group_id, eid, episode)
    except Exception as e:
        logger.debug(f"认知更新失败 [群{group_id}]（不影响事件记忆）: {e}")

    # 清理缓冲区：已整合的移除，未闭合的保留（AgentGal draft 语义）
    consumed = episode["closed_until"]
    keep_from = len(buf) - len(window) + consumed
    memory._message_buffers[group_id] = buf[keep_from:]

    # 同步写一条轻量旧式摘要，让日报「今日话题」也能看到
    db.add_conversation_memory(
        group_id=group_id,
        topic=episode["title"],
        summary=episode["content"],
        participants=[],
        key_facts=episode["keywords"],
        message_count=episode["closed_until"],
        importance=episode["importance"] / 5.0,
    )

    logger.info(
        f"🧩 事件整合 [群{group_id}]: «{episode['title']}» "
        f"imp={episode['importance']} 认知±{applied} "
        f"剩余缓冲={len(memory._message_buffers[group_id])}"
    )
    return True
