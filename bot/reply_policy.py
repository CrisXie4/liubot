"""
回复目标策略 — 什么时候该引用、什么时候该 @
==========================================
现象: Bot 每次被 @ 都用「引用那条消息 + @对方」的方式回，看着很像客服工单。
真人不这么聊天——对方刚说完话你直接回就行了，引用只在下面这些时候才用:

  1. 回一条已经被刷过去的旧消息（中间隔了好几条别人的话）
  2. 群里同时有好几个话头，不引用别人不知道你在回哪条
  3. 隔了很久才回（人已经走远了）

@ 同理: 对方刚跟你说完话，回他不用 @；只有喊一个当前不在对话里的人才需要 @。

本模块做两件事:
  - 记一本轻量的群消息流水账（谁、哪条、什么时候）
  - sanitize_targeting(): 在发送前把 AI 加的多余 [CQ:reply] / 开头的 @ 剥掉，
    该留的留。这是代码层兜底——光靠提示词管不住。
"""
import logging
import re
import time
from collections import deque

logger = logging.getLogger("qqbot")

# ─── 群消息流水: {group_id: deque[{"mid","qq","ts"}]} ───
_log: dict[int, deque] = {}
LOG_KEEP = 60

# ─── 判定阈值（可被 settings 覆盖）───
DEFAULTS = {
    "quote_gap_msgs": 3,       # 目标消息后面隔了 >= 这么多条，才值得引用
    "quote_gap_seconds": 180,  # 或者过了这么久
    "at_when_absent": True,    # 只有对方"不在当前对话里"时才允许 @
    "max_quotes": 1,           # 一条回复最多保留 1 个引用
}

_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)\]")
_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
# AI 有时不用 CQ 码，直接打字 "@名字 " 开头
_TEXT_AT_PREFIX_RE = re.compile(r"^\s*@([^\s:：,，]{1,20})[\s:：,，]*")


def _cfg() -> dict:
    """读取运行时配置（Web UI 可改），失败则用默认值"""
    try:
        from . import settings as _s
        saved = _s.load().get("reply_policy") or {}
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        return cfg
    except Exception:
        return dict(DEFAULTS)


# ═══════════════════════════════════════════════════════════
#  消息流水账
# ═══════════════════════════════════════════════════════════

def note_message(group_id: int, message_id: int, user_qq: int):
    """每收到一条群消息就记一笔"""
    if not group_id or not message_id:
        return
    dq = _log.setdefault(group_id, deque(maxlen=LOG_KEEP))
    dq.append({"mid": int(message_id), "qq": int(user_qq or 0), "ts": time.time()})


def messages_after(group_id: int, message_id: int) -> int:
    """这条消息之后又来了多少条群消息。查不到该消息时返回 -1。"""
    dq = _log.get(group_id)
    if not dq or not message_id:
        return -1
    items = list(dq)
    for i, item in enumerate(items):
        if item["mid"] == int(message_id):
            return len(items) - i - 1
    return -1


def age_of(group_id: int, message_id: int) -> float:
    """这条消息距今多少秒。查不到返回 -1。"""
    dq = _log.get(group_id)
    if not dq or not message_id:
        return -1.0
    for item in dq:
        if item["mid"] == int(message_id):
            return time.time() - item["ts"]
    return -1.0


def is_still_current(group_id: int, message_id: int) -> bool:
    """这条消息是不是"还在当前话头上"（后面没被刷太多、也没过太久）"""
    cfg = _cfg()
    after = messages_after(group_id, message_id)
    if after < 0:
        # 流水账里没有 → 多半是很旧的消息，当作"已经过去了"
        return False
    if after >= cfg["quote_gap_msgs"]:
        return False
    age = age_of(group_id, message_id)
    if age >= 0 and age >= cfg["quote_gap_seconds"]:
        return False
    return True


def spoke_recently(group_id: int, user_qq: int, within: int = 3) -> bool:
    """这个人是不是最近 within 条消息里说过话（即"还在对话里"）"""
    dq = _log.get(group_id)
    if not dq or not user_qq:
        return False
    for item in list(dq)[-within:]:
        if item["qq"] == int(user_qq):
            return True
    return False


# ═══════════════════════════════════════════════════════════
#  引用判定
# ═══════════════════════════════════════════════════════════

def should_quote(group_id: int, target_mid: int, *, trigger_mid: int = 0) -> bool:
    """
    该不该引用 target_mid 这条消息（参考判定）。

    2026-10-13 起 sanitize 不再按此强制剥引用——引用是 AI 自己的选择
    （回旧消息必须引、回新消息随它），规则由提示词引导。本函数保留作
    外部参考/测试。

    引用的唯一意义是"消除歧义"——如果对方刚说完话、你马上回，
    没有任何歧义，引用纯属多余。
    """
    if not target_mid:
        return False
    # 引用自己正在回的那条、而且它还是最新的话头 → 不需要引用
    if trigger_mid and int(target_mid) == int(trigger_mid):
        return not is_still_current(group_id, target_mid)
    # 引用的是别的消息 → 只有那条已经被刷过去了才值得引用
    return not is_still_current(group_id, target_mid)


def should_at(group_id: int, target_qq: int, *, trigger_qq: int = 0) -> bool:
    """该不该 @ 这个人。对方刚跟你说完话 → 不用 @。"""
    if not target_qq:
        return False
    cfg = _cfg()
    if not cfg["at_when_absent"]:
        return True
    if trigger_qq and int(target_qq) == int(trigger_qq):
        # 说话的就是他本人，直接回，不用 @
        return False
    # 他最近说过话 → 还在对话里，不用 @
    return not spoke_recently(group_id, target_qq, within=3)


# ═══════════════════════════════════════════════════════════
#  发送前清洗
# ═══════════════════════════════════════════════════════════

def sanitize_targeting(
    text: str,
    *,
    group_id: int,
    trigger_message_id: int = 0,
    trigger_user_qq: int = 0,
    trigger_user_name: str = "",
) -> str:
    """
    发送前清洗引用/@。

    - [CQ:reply,id=N]: 引用是 AI 自己的选择（2026-10-13 起不再按"新旧话头"
      强制剥除——回旧消息必须引、回新消息随它引，规则由提示词引导）。
      这里只做：最多保留 max_quotes 个、统一挪到整段开头（QQ 只认开头的
      reply 码）、无效 id 丢弃
    - [CQ:at,qq=X]:    用 should_at 过一遍
    - 开头手打的 "@名字 " 前缀: 如果名字就是当前说话人，直接去掉
    """
    if not text:
        return text
    cfg = _cfg()

    # ── 1. 引用码 ──
    kept_quote = ""
    quota = cfg["max_quotes"]

    def _reply_sub(m):
        nonlocal kept_quote, quota
        try:
            mid = int(m.group(1))
        except (TypeError, ValueError):
            return ""
        if mid <= 0:
            return ""
        if quota > 0:
            quota -= 1
            if not kept_quote:
                kept_quote = f"[CQ:reply,id={mid}]"
            return ""      # 先摘掉，最后统一放到开头
        return ""          # 超出配额的引用丢弃

    text = _REPLY_RE.sub(_reply_sub, text)

    # ── 2. @ 码 ──
    def _at_sub(m):
        try:
            qq = int(m.group(1))
        except (TypeError, ValueError):
            return ""
        if should_at(group_id, qq, trigger_qq=trigger_user_qq):
            return m.group(0)
        return ""

    text = _AT_RE.sub(_at_sub, text)

    # ── 3. 手打的 "@名字" 开头 ──
    if trigger_user_name:
        m = _TEXT_AT_PREFIX_RE.match(text)
        if m and m.group(1).strip() == trigger_user_name.strip():
            text = text[m.end():]

    # ── 4. 收尾：清理剥离后残留的空白/空行 ──
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    if kept_quote and text:
        text = kept_quote + text
    elif kept_quote:
        text = kept_quote
    return text


def clear_group(group_id: int):
    _log.pop(group_id, None)
