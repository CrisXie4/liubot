"""
拟人化发送模块 — 让 Bot 说话的节奏像真人
============================================
真人在 QQ 群里聊天的特征:
  1. 收到消息后有"看到 + 想 + 打字"的时间，不会秒回一大段
  2. 想说的多时会拆成几条短消息连发，而不是一整块文字
  3. 不会输出 markdown（**加粗**、# 标题、- 列表），手机上也打不出来
  4. 短句结尾往往不带句号
  5. 偶尔会打错字然后马上纠正（打错→撤回→重发，像真人一样）

本模块把 AI 生成的回复做三件事:
  - strip_ai_flavor: 清理 markdown 等书面痕迹
  - split_bubbles:   按换行/句子拆成 1~N 条气泡
  - send_human_like: 按打字速度分条延迟发送（扣除 LLM 生成已花的时间）
"""
import asyncio
import logging
import random
import re
import time

logger = logging.getLogger("qqbot")

# ─── CQ 码与表情快捷码不参与字数统计、也不能被拆断 ───
_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_FACE_SHORTCUT_RE = re.compile(r":\d{1,3}:")
_REPLY_CQ_RE = re.compile(r"\[CQ:reply,id=-?\d+\]")


def _extract_reply_code(text: str) -> tuple[str, str]:
    """
    把回复引用码从正文里摘出来。

    QQ 只认整条消息开头的 [CQ:reply]，而气泡是分多条发的——
    引用码要是落在第二、三条气泡里，那条就变成一个孤零零的引用，
    看着很怪。所以统一摘出来，只贴回第一条气泡的最前面。

    Returns: (剩余正文, 引用码或空串)
    """
    codes = _REPLY_CQ_RE.findall(text)
    if not codes:
        return text, ""
    return _REPLY_CQ_RE.sub("", text).strip(), codes[0]

# ─── 节奏参数（模拟手机打字）───
TYPING_CPS = (5.5, 9.0)       # 打字速度: 每秒字符数（正常）
FAST_CPS = (8.0, 13.0)        # 兴奋/生气时打字快
SLOW_CPS = (3.0, 5.5)         # 困/犹豫时打字慢
FIRST_REACT = (0.4, 1.6)      # 看到消息的反应时间（秒）
BUBBLE_GAP = (0.2, 0.6)       # 两条气泡之间的最小停顿
MAX_BUBBLE_DELAY = 4.5        # 单条最长等待
MAX_TOTAL_DELAY = 9.0         # 整个回复累计最长等待
MAX_BUBBLES = 4               # 一次最多连发条数
SPLIT_THRESHOLD = 42          # 可见字数超过该值的段落尝试按句子再拆
MIN_MERGE_LEN = 5             # 比这短的碎片并入相邻气泡（纯表情除外）
TYPO_CHANCE = 0.03            # 3% 概率打错字
HESITATE_CHANCE = 0.05        # 5% 概率多犹豫一会
LONG_HESITATE_CHANCE = 0.02   # 2% 概率"正在输入..."很久


def strip_ai_flavor(text: str) -> str:
    """清理 LLM 的书面痕迹: markdown、列表符号等（QQ 不渲染 markdown）"""
    t = text
    # 先修复/剥除残缺 CQ 码——必须在拆气泡前做，
    # 否则超长残码可能被拆散到多条气泡，后续气泡全是乱码
    if "[CQ:" in t:
        from .onebot_client import sanitize_cq_codes
        t = sanitize_cq_codes(t)
    t = re.sub(r"\*{1,2}([^*\n]+?)\*{1,2}", r"\1", t)     # **加粗** / *斜体*
    t = re.sub(r"^#{1,4}\s*", "", t, flags=re.M)           # # 标题
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)         # - 列表项
    t = re.sub(r"`([^`\n]+?)`", r"\1", t)                  # `行内代码`
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _visible_len(s: str) -> int:
    """去掉 CQ 码/表情快捷码后的可见字数（用于估算打字时间）"""
    return len(_FACE_SHORTCUT_RE.sub("", _CQ_RE.sub("", s)))


def _protect_cq(text: str) -> tuple[str, list[str]]:
    """把 CQ 码替换成占位符，防止被句子切分拆坏"""
    codes: list[str] = []

    def _repl(m):
        codes.append(m.group(0))
        return f"\x00{len(codes) - 1}\x00"

    return _CQ_RE.sub(_repl, text), codes


def _restore_cq(text: str, codes: list[str]) -> str:
    for i, code in enumerate(codes):
        text = text.replace(f"\x00{i}\x00", code)
    return text


_SENT_END_RE = re.compile(r"([。！？!?…~～]+)")


def _split_sentences(text: str) -> list[str]:
    """长段落按句末标点拆开（CQ 码保护）"""
    protected, codes = _protect_cq(text)
    parts = _SENT_END_RE.split(protected)
    # split 结果是 [句, 标点, 句, 标点, ...] — 两两拼回
    merged: list[str] = []
    for i in range(0, len(parts), 2):
        seg = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        piece = (seg + punct).strip()
        if piece:
            merged.append(_restore_cq(piece, codes))
    return merged or [text]


def _tidy_bubble(b: str) -> str:
    """短气泡结尾的句号去掉——真人打字很少在结尾放"。" """
    b = b.strip()
    if _visible_len(b) <= 30 and b.endswith("。"):
        b = b[:-1]
    return b


def split_bubbles(text: str, max_bubbles: int = MAX_BUBBLES) -> list[str]:
    """
    把一段回复拆成 1~N 条聊天气泡:
      1. 先按换行拆（提示词已告诉 AI: 换行 = 分条发送）
      2. 过长的段落再按句子拆
      3. 过短的碎片并入相邻气泡；总条数超限时合并尾部
      4. 短回复不拆分，防 AI 输出冗余产生"说两遍"的观感
    """
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if not parts:
        return []

    # 整体很短（<60字 且 ≤3行）→ 不拆，合并为一条
    total_visible = sum(_visible_len(p) for p in parts)
    if total_visible < 60 and len(parts) <= 3:
        return [_tidy_bubble(" ".join(parts))]

    pieces: list[str] = []
    for p in parts:
        if _visible_len(p) <= SPLIT_THRESHOLD:
            pieces.append(p)
        else:
            pieces.extend(_split_sentences(p))

    # 合并过短碎片（纯表情/纯 CQ 码的气泡保留独立——真人也会单发一个表情）
    bubbles: list[str] = []
    for piece in pieces:
        standalone_media = _visible_len(piece) == 0 and piece
        if (
            bubbles
            and not standalone_media
            and _visible_len(piece) < MIN_MERGE_LEN
            and _visible_len(bubbles[-1]) < SPLIT_THRESHOLD
        ):
            bubbles[-1] = bubbles[-1] + " " + piece
        else:
            bubbles.append(piece)

    # 条数超限 → 尾部合并
    if len(bubbles) > max_bubbles:
        head = bubbles[: max_bubbles - 1]
        tail = " ".join(bubbles[max_bubbles - 1:])
        bubbles = head + [tail]

    return [_tidy_bubble(b) for b in bubbles if b.strip()]


def _detect_mood_from_text(text: str) -> str:
    """从回复文本检测情绪状态来调整打字速度。返回 'normal'/'fast'/'slow'"""
    # 短且带感叹号/语气词 → 兴奋/快速
    fast_patterns = ["哈哈哈", "笑死", "草", "牛逼", "太强", "卧槽", "绝了",
                     "厉害", "666", "太棒", "开心", "好耶", "芜湖"]
    # 长回复/省略号/犹豫词 → 困/犹豫
    slow_patterns = ["...", "嗯", "emmm", "呃", "就是", "怎么说", "那个",
                     "不太确定", "可能", "好像", "也许", "大概"]
    if any(p in text for p in fast_patterns):
        return "fast"
    if any(p in text for p in slow_patterns) or len(text) > 80:
        return "slow"
    return "normal"


# 群级发送冷却 — 防止并发导致同一群短时间内发多条
_last_send_time: dict[int, float] = {}
SEND_COOLDOWN = 5.0  # 同一群两次发送最少间隔（秒）


async def send_human_like(
    bot,
    group_id: int,
    text: str,
    *,
    started_at: float | None = None,
    max_bubbles: int = MAX_BUBBLES,
    private_user_id: int = 0,
) -> list[str]:
    """
    以真人节奏发送回复。

    Args:
        bot: OneBotClient 实例
        group_id: 群号（0 表示私聊）
        text: 完整回复文本（可含换行、CQ 码、:14: 表情快捷码）
        started_at: 触发消息的时间戳
        max_bubbles: 最多拆几条
        private_user_id: 私聊目标 QQ（group_id=0 时生效）

    Returns:
        实际发送的气泡列表
    """
    text = strip_ai_flavor(text)
    if not text:
        return []

    # 引用码单独拎出来，最后只贴到第一条气泡上
    text, reply_code = _extract_reply_code(text)
    if not text:
        return []

    bubbles = split_bubbles(text, max_bubbles)
    if not bubbles:
        return []
    if reply_code:
        bubbles[0] = reply_code + bubbles[0]

    # 群聊冷却 — 防止并发/竞态导致同一秒内多发
    ctx_key = private_user_id if group_id == 0 else group_id
    now_ts = time.time()
    last = _last_send_time.get(ctx_key, 0)
    if ctx_key and now_ts - last < SEND_COOLDOWN:
        logger.debug(f"发送冷却中 [{'私聊' if group_id == 0 else '群'}{ctx_key}], 跳过")
        return []
    _last_send_time[ctx_key] = now_ts

    is_private = (group_id == 0)

    # 根据回复情绪选择打字速度
    mood = _detect_mood_from_text(text)
    if mood == "fast":
        cps_range = FAST_CPS
    elif mood == "slow":
        cps_range = SLOW_CPS
    else:
        cps_range = TYPING_CPS

    elapsed = (time.time() - started_at) if started_at else 0.0
    total_wait = 0.0

    # 真人行为：偶尔多犹豫一会
    if random.random() < HESITATE_CHANCE and elapsed < 2.0:
        extra_hesitate = random.uniform(0.4, 1.2)
        await asyncio.sleep(extra_hesitate)
        total_wait += extra_hesitate

    # 发送函数选择
    async def _send(target_id: int, msg: str) -> int | None:
        if is_private:
            return await bot.send_private_msg(target_id, msg)
        else:
            return await bot.send_group_msg(target_id, msg)

    target = private_user_id if is_private else group_id

    for i, bubble in enumerate(bubbles):
        cps = random.uniform(*cps_range)
        delay = _visible_len(bubble) / cps
        if i == 0:
            delay += random.uniform(*FIRST_REACT)
            delay -= elapsed
            if random.random() < LONG_HESITATE_CHANCE and delay < 1.0:
                delay += random.uniform(1.0, 2.5)
        else:
            delay += random.uniform(*BUBBLE_GAP)

        delay = max(0.0, min(delay, MAX_BUBBLE_DELAY))
        if total_wait + delay > MAX_TOTAL_DELAY:
            delay = max(0.0, MAX_TOTAL_DELAY - total_wait)
        if delay > 0:
            await asyncio.sleep(delay)
        total_wait += delay

        # 真人行为：极小概率打错字→撤回→重发（仅群聊，私聊不支持撤回）
        # 注意：气泡以 CQ 码开头时跳过——按字符切会把 CQ 码切成乱码发出去
        if (
            not is_private
            and not bubble.startswith("[CQ:")
            and random.random() < TYPO_CHANCE
            and len(bubble) > 3
            and _visible_len(bubble) > 2
        ):
            split_at = min(random.randint(1, 3), len(bubble) - 1)
            typo_part = bubble[:split_at]
            msg_id = await bot.send_group_msg(group_id, typo_part)
            if msg_id:
                await asyncio.sleep(random.uniform(0.6, 1.5))
                await bot.delete_msg(msg_id)
                await asyncio.sleep(random.uniform(0.3, 0.6))
                await bot.send_group_msg(group_id, bubble)
                continue

        await _send(target, bubble)

    ctx = f"私聊{private_user_id}" if is_private else f"群{group_id}"
    if len(bubbles) > 1:
        logger.debug(f"拟人发送 [{ctx}]: {len(bubbles)} 条气泡, 总延迟 {total_wait:.1f}s [{mood}]")
    return bubbles
