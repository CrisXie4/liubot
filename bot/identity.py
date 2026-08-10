"""
群成员身份解析 — 让 AI 始终知道"现在在跟谁说话"
================================================
问题背景:
  AI 看到的原始消息是 "[CQ:at,qq=123456] 他喽"，既不知道 123456 是谁，
  也不知道这条消息是谁发的。结果就是 @ 错人、认错对象、乱抢答。

本模块提供三样东西:
  - get_member_name / resolve_at_mentions: QQ号 ↔ 群名片 的双向解析（带 TTL 缓存）
  - note_speaker: 记录最近发言者，构成"在场的人"
  - build_roster: 生成注入提示词的花名册 —— 名字 + QQ号成对出现，
    AI 想 @ 谁、私聊谁，直接就能拿到号码，不用猜
"""
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("qqbot")

# ─── 成员名缓存: {group_id: {qq: (name, ts)}} ───
_name_cache: dict[int, dict[int, tuple[str, float]]] = {}
NAME_TTL = 900  # 群名片改动不频繁，15 分钟够用

# ─── 最近发言者: {group_id: deque[(qq, name, ts)]} ───
_recent_speakers: dict[int, deque] = {}
SPEAKER_KEEP = 12       # 每群最多记住最近 12 个发言人
SPEAKER_TTL = 1800      # 半小时没说话就不算"在场"

_AT_RE = None  # 延迟编译


def _at_pattern():
    global _AT_RE
    if _AT_RE is None:
        import re
        _AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
    return _AT_RE


# ═══════════════════════════════════════════════════════════
#  名字解析
# ═══════════════════════════════════════════════════════════

def cache_name(group_id: int, qq: int, name: str):
    """外部已知名字时主动写入缓存（省一次 API 调用）"""
    if not name or not qq:
        return
    _name_cache.setdefault(group_id, {})[qq] = (name, time.time())


def peek_name(group_id: int, qq: int) -> str:
    """只查缓存，不发 API 请求。查不到返回空串。"""
    entry = _name_cache.get(group_id, {}).get(qq)
    if not entry:
        return ""
    name, ts = entry
    if time.time() - ts > NAME_TTL:
        return ""
    return name


async def get_member_name(bot, group_id: int, qq: int) -> str:
    """获取群成员显示名（群名片优先，其次 QQ 昵称，最后退化为 QQ 号）"""
    cached = peek_name(group_id, qq)
    if cached:
        return cached
    name = str(qq)
    try:
        info = await bot.get_group_member_info(group_id, qq)
        if info:
            name = (info.get("card", "") or "").strip() \
                or (info.get("nickname", "") or "").strip() \
                or str(qq)
    except Exception:
        pass
    cache_name(group_id, qq, name)
    return name


def extract_at_qqs(text: str, exclude: set[int] | None = None) -> list[int]:
    """从原始消息里抽出所有被 @ 的 QQ 号（保持出现顺序、去重）"""
    if not text or "[CQ:at,qq=" not in text:
        return []
    exclude = exclude or set()
    seen: list[int] = []
    for m in _at_pattern().finditer(text):
        try:
            qq = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if qq in exclude or qq in seen:
            continue
        seen.append(qq)
    return seen


async def resolve_at_mentions(bot, group_id: int, text: str) -> str:
    """把消息里的 [CQ:at,qq=xxx] 换成 @群名片，让 AI 读得懂"""
    if not text or "[CQ:at,qq=" not in text:
        return text
    result = text
    for qq in extract_at_qqs(text):
        name = await get_member_name(bot, group_id, qq)
        result = result.replace(f"[CQ:at,qq={qq}]", f"@{name}")
    return result


# ═══════════════════════════════════════════════════════════
#  在场人员
# ═══════════════════════════════════════════════════════════

def note_speaker(group_id: int, qq: int, name: str):
    """记录一次发言（同时刷新名字缓存）"""
    if not group_id or not qq:
        return
    if name:
        cache_name(group_id, qq, name)
    dq = _recent_speakers.setdefault(group_id, deque(maxlen=SPEAKER_KEEP))
    # 同一个人重复发言只保留最新一条记录
    for item in list(dq):
        if item[0] == qq:
            dq.remove(item)
            break
    dq.append((qq, name or str(qq), time.time()))


def get_recent_speakers(group_id: int, limit: int = 8, exclude: set[int] | None = None) -> list[dict]:
    """最近还在场的发言者，最近说话的排最前"""
    exclude = exclude or set()
    now = time.time()
    dq = _recent_speakers.get(group_id)
    if not dq:
        return []
    out = []
    for qq, name, ts in reversed(dq):
        if qq in exclude or now - ts > SPEAKER_TTL:
            continue
        out.append({"qq": qq, "name": name, "idle": int(now - ts)})
        if len(out) >= limit:
            break
    return out


def _fmt_idle(seconds: int) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    return f"{seconds // 3600}小时前"


async def build_roster(
    bot,
    group_id: int,
    *,
    speaker_qq: int,
    speaker_name: str,
    mentioned_qqs: list[int] | None = None,
    quoted_qq: int = 0,
    quoted_name: str = "",
    bot_qq: int = 0,
) -> str:
    """
    生成"在场人员"提示块。每个人都是「名字 (QQ=号码)」的形式，
    这样 AI 要 @ 谁、私聊谁，号码是现成的，不用瞎猜也不会 @ 错人。
    """
    lines: list[str] = []
    skip = {bot_qq} if bot_qq else set()

    if speaker_qq:
        lines.append(
            f"【正在跟你说话的人】{speaker_name or speaker_qq} (QQ={speaker_qq})"
            f" —— 这条消息就是他发的，回他直接说就行，不用 @ 他也不用引用他"
        )
        skip.add(speaker_qq)

    mentioned_qqs = [q for q in (mentioned_qqs or []) if q not in skip]
    if mentioned_qqs:
        parts = []
        for qq in mentioned_qqs[:6]:
            name = peek_name(group_id, qq) or await get_member_name(bot, group_id, qq)
            parts.append(f"{name} (QQ={qq})")
        lines.append(
            "【这条消息里 @ 到的人】" + "、".join(parts)
            + " —— 他在跟这些人说话，不一定是在跟你说话"
        )
        skip.update(mentioned_qqs)

    if quoted_qq and quoted_qq not in skip:
        qname = quoted_name or peek_name(group_id, quoted_qq) or str(quoted_qq)
        lines.append(f"【他引用的那条消息是谁发的】{qname} (QQ={quoted_qq})")
        skip.add(quoted_qq)

    others = get_recent_speakers(group_id, limit=6, exclude=skip)
    if others:
        parts = [f"{o['name']} (QQ={o['qq']}, {_fmt_idle(o['idle'])}说过话)" for o in others]
        lines.append("【群里最近还在的人】" + "、".join(parts))

    if not lines:
        return ""
    return "\n".join(lines)


def clear_group(group_id: int):
    """清理某群的缓存（调试/重置用）"""
    _name_cache.pop(group_id, None)
    _recent_speakers.pop(group_id, None)


# ═══════════════════════════════════════════════════════════
#  名字 → QQ 解析（让 AI 能按名字找人）
# ═══════════════════════════════════════════════════════════

_FIND_LIMIT = 5  # 最多返回几个候选


async def find_members_by_name(bot, group_id: int, query: str) -> list[dict]:
    """按群名片/QQ 昵称查群成员。

    匹配策略（用户已确认）：
    - 第一轮：card 或 nickname 完全等于 query → match_type="exact"
    - 第二轮：query 是 card 或 nickname 的子串 → match_type="substring"
    - 大小写无关（英文场景）
    - 按 exact 优先排序，封顶 _FIND_LIMIT 个

    返回 [{"qq", "card", "nickname", "role", "match_type"}]，空列表=没找到
    """
    query = (query or "").strip().lower()
    if not query:
        return []

    try:
        members = await bot.get_group_member_list(group_id)
    except Exception as e:
        logger.warning(f"find_members_by_name 拉成员列表失败: {e}")
        return []

    exact_hits: list[dict] = []
    sub_hits: list[dict] = []

    for m in members or []:
        qq = int(m.get("user_id", 0) or 0)
        if not qq:
            continue
        card = (m.get("card", "") or "").strip()
        nick = (m.get("nickname", "") or "").strip()
        role = m.get("role", "member")

        if query == card.lower() or query == nick.lower():
            exact_hits.append({
                "qq": qq, "card": card, "nickname": nick,
                "role": role, "match_type": "exact",
            })
        elif query in card.lower() or query in nick.lower():
            sub_hits.append({
                "qq": qq, "card": card, "nickname": nick,
                "role": role, "match_type": "substring",
            })

    # exact 优先，封顶
    return (exact_hits + sub_hits)[:_FIND_LIMIT]
