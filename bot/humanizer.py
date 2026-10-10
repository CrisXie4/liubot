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
NIGHT_SLOWDOWN = 1.6          # 深夜（23~8 点）整体节奏放慢倍率
TAIL_BUBBLE_CHANCE = 0.15     # 15% 概率在末尾补一条极短尾泡
TAIL_BUBBLES = ("哈哈", "对", "笑死", "草", "确实", "绷")  # 尾泡内容池


def _bubble_gap() -> float:
    """两条气泡之间的停顿——混合分布，方差比均匀随机大。

    真人打字是"顺手连发 → 换口气 → 走神看了一眼别处"的混合体，
    均匀分布的间隔听感像节拍器。三档全部从 BUBBLE_GAP 推导，
    测试里把它归零即可整体静音。
    """
    hi = BUBBLE_GAP[1]
    r = random.random()
    if r < 0.70:
        return random.uniform(*BUBBLE_GAP)       # 顺手连发
    if r < 0.93:
        return random.uniform(hi, hi * 3.0)      # 正常换气
    return random.uniform(hi * 3.0, hi * 6.0)    # 走神了一下


def _night_factor(hour: int | None = None) -> float:
    """深夜（23~8 点）整体节奏放慢的倍率——那个点反应和手速都懒。"""
    if hour is None:
        try:
            from . import timeutil
            hour = timeutil.now_shanghai().hour
        except Exception:
            hour = time.localtime().tm_hour
    return NIGHT_SLOWDOWN if (hour >= 23 or hour < 8) else 1.0


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
    if total_visible < 35 and len(parts) <= 2:
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

# 最近一次发送失败详情（handler 发送后立刻读取，用于把"没发出去"告知 AI）
last_send_failure: dict | None = None

# ═══ 长消息 → 合并转发（学 Jianer"3段规则" + LangBot 降级链）═══
# 旧路径：split_bubbles 最多 4 条气泡，超限的尾部合并成一条巨长气泡，
# 单条超长会被 QQ/NapCat 直接拒发（retcode 失败）——这就是"长回复被拒绝"。
# 新路径：长回复拆成多条节点，走 send_group_forward_msg 发成"聊天记录"卡片，
# 每个节点 = bot 昵称 + 一个自然段，视觉上是可滑动的拟人对话。
# 转发失败（API 挂/风控）→ 降级回旧的逐条气泡路径。
LONG_TEXT_FORWARD_THRESHOLD = 500   # 可见字符总数超过 → 转发模式
LONG_BUBBLE_THRESHOLD = 1000        # 单条气泡可见字符超过 → 强制转发模式
FORWARD_NODE_MAX_LEN = 600          # 单个转发节点上限，超出按句子再切


def _split_for_forward_nodes(bubbles: list[str]) -> list[str]:
    """把气泡列表展开成转发节点文本段：超长气泡按句子边界细分。"""
    import re as _re
    pieces: list[str] = []
    for b in bubbles:
        if _visible_len(b) <= FORWARD_NODE_MAX_LEN:
            pieces.append(b)
            continue
        # 按句子边界切（保留标点），凑段不超过上限
        sentences = _re.findall(r"[^。！？!?\n]+[。！？!?\n]?", b)
        buf = ""
        for s in sentences:
            if buf and _visible_len(buf) + _visible_len(s) > FORWARD_NODE_MAX_LEN:
                pieces.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            pieces.append(buf.strip())
    return [p for p in pieces if p.strip()]


async def _send_as_forward(bot, group_id: int, bubbles: list[str]) -> bool:
    """尝试把回复作为合并转发消息发送，成功返回 True。"""
    try:
        from . import group_personality as _gp
        from .onebot_client import cq_text_to_segments
        nickname = _gp.get_group_bot_name(group_id)
        sender_id = int(bot.bot_qq)
        nodes = []
        for piece in _split_for_forward_nodes(bubbles):
            nodes.append({
                "type": "node",
                "data": {
                    "user_id": sender_id,
                    "nickname": nickname,
                    "content": cq_text_to_segments(piece),
                },
            })
        if not nodes:
            return False
        msg_id = await bot.send_group_forward_msg(group_id, nodes)
        if msg_id:
            logger.info(
                f"📤 长回复转合并转发 [群{group_id}]: {len(nodes)} 节点 "
                f"({sum(_visible_len(b) for b in bubbles)} 字)"
            )
            return True
    except Exception as e:
        logger.debug(f"合并转发送失败，降级逐条发送: {e}")
    return False


async def _send_quote_followup(bot, group_id: int, reply_code: str):
    """长回复走转发/文字图降级后，补发一条轻引用兜底（失败静默）。

    转发卡片和文字图都带不上 [CQ:reply]，两条降级路径引用都会丢——
    在成功发出长文后补一条轻引用指回原消息。直走 bot.send_group_msg，
    不经群发送冷却（该冷却只包 send_human_like 的调用入口）。
    """
    if not reply_code:
        return
    try:
        await bot.send_group_msg(group_id, f"{reply_code}（长文在上👆）")
    except Exception:
        pass


async def send_human_like(
    bot,
    group_id: int,
    text: str,
    *,
    started_at: float | None = None,
    max_bubbles: int = MAX_BUBBLES,
    private_user_id: int = 0,
    priority: bool = False,
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
        priority: 系统关键消息（验证题/判题反馈/踢人通知等）。
            不参与群冷却、也不会被冷却丢弃——这类消息丢了后果是
            新人无题被踢，宁可两条连发也不能不发。

    Returns:
        实际发送的气泡列表
    """
    global last_send_failure
    # 归因窗口：每次调用只反映本次的失败，防 handler 读到上一轮的旧账
    last_send_failure = None

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
    # priority=True 的关键消息（验证题等）不参与冷却，也不会被冷却丢弃
    ctx_key = private_user_id if group_id == 0 else group_id
    now_ts = time.time()
    last = _last_send_time.get(ctx_key, 0)
    if not priority and ctx_key and now_ts - last < SEND_COOLDOWN:
        ctx = f"私聊{ctx_key}" if group_id == 0 else f"群{ctx_key}"
        reason = (f"发送冷却中（距上次发送 {now_ts - last:.1f}s < {SEND_COOLDOWN}s），"
                  f"整条消息未发出")
        logger.warning(f"拟人发送丢弃 [{ctx}]: {reason} | {text[:40]}")
        last_send_failure = {"ctx": ctx, "gid": ctx_key, "is_private": group_id == 0,
                             "failed": len(bubbles), "total": len(bubbles), "reason": reason}
        return []
    _last_send_time[ctx_key] = now_ts

    is_private = (group_id == 0)

    # ★ 长消息 → 合并转发模式（群聊）：解决单条超长被 QQ 拒发的问题，
    # 且"聊天记录卡片"比一坨文字好读得多（学 Jianer/LangBot）。
    # 降级链：转发失败 → 渲染文字图（LangBot）→ 都不行才逐条气泡。
    total_visible = sum(_visible_len(b) for b in bubbles)
    longest = max(_visible_len(b) for b in bubbles)
    if not is_private and (total_visible > LONG_TEXT_FORWARD_THRESHOLD
                           or longest > LONG_BUBBLE_THRESHOLD):
        # 引用码在副本上剥：转发节点/文字图里 [CQ:reply] 会被破坏，
        # 原 bubbles 不动——两条降级都失败落回逐条气泡时，首气泡引用还在
        fwd_bubbles = bubbles
        if reply_code and bubbles[0].startswith(reply_code):
            first = bubbles[0][len(reply_code):]
            fwd_bubbles = ([first] if first else []) + bubbles[1:]
        if await _send_as_forward(bot, group_id, fwd_bubbles):
            await _send_quote_followup(bot, group_id, reply_code)
            return bubbles
        # 转发失败 → 文字图兜底（渲染用原 text，本就摘掉了引用码）
        try:
            from . import text_image
            img_path = await asyncio.to_thread(text_image.render_text_image, text)
            if img_path:
                mid = await bot.send_group_msg(group_id, f"[CQ:image,file={img_path}]")
                if mid:
                    logger.info(f"🖼️ 长回复渲染文字图 [群{group_id}]（转发不可用）")
                    await _send_quote_followup(bot, group_id, reply_code)
                    return bubbles
        except Exception as e:
            logger.debug(f"文字图降级失败: {e}")
        # 都不行 → 落到下面的逐条气泡路径（不剥码、不补发：首气泡自带引用码）

    # 根据回复情绪选择打字速度
    mood = _detect_mood_from_text(text)
    if mood == "fast":
        cps_range = FAST_CPS
    elif mood == "slow":
        cps_range = SLOW_CPS
    else:
        cps_range = TYPING_CPS

    # 深夜（23~8 点）整体放慢：反应、打字、气泡间隔一起懒下来；
    # 单条/整单的上限也同步放宽，不然慢了等于没慢（全被钳回原值）
    night = _night_factor()
    max_bubble_delay = MAX_BUBBLE_DELAY * night
    max_total_delay = MAX_TOTAL_DELAY * night

    elapsed = (time.time() - started_at) if started_at else 0.0
    total_wait = 0.0

    # 真人行为：偶尔多犹豫一会
    if random.random() < HESITATE_CHANCE and elapsed < 2.0:
        extra_hesitate = random.uniform(0.4, 1.2) * night
        await asyncio.sleep(extra_hesitate)
        total_wait += extra_hesitate

    # 发送函数选择
    async def _send(target_id: int, msg: str) -> int | None:
        if is_private:
            return await bot.send_private_msg(target_id, msg)
        else:
            return await bot.send_group_msg(target_id, msg)

    target = private_user_id if is_private else group_id
    sent_bubbles: list[str] = []

    for i, bubble in enumerate(bubbles):
        cps = random.uniform(*cps_range) / night
        delay = _visible_len(bubble) / cps
        if i == 0:
            delay += random.uniform(*FIRST_REACT) * night
            delay -= elapsed
            if random.random() < LONG_HESITATE_CHANCE and delay < 1.0:
                delay += random.uniform(1.0, 2.5)
        else:
            delay += _bubble_gap() * night

        delay = max(0.0, min(delay, max_bubble_delay))
        if total_wait + delay > max_total_delay:
            delay = max(0.0, max_total_delay - total_wait)
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
                if await bot.send_group_msg(group_id, bubble):
                    sent_bubbles.append(bubble)
                continue

        if await _send(target, bubble):
            sent_bubbles.append(bubble)

    # 真人行为：说完正事偶尔像想起来什么似的，再补一条极短的尾泡（15%）。
    # 只在多条气泡时补——单发一条后面跟个"哈哈"更像毛病不像真人；
    # 系统关键消息（验证题/判题反馈等）绝不补。走到这里原文必然非空
    # （空文本在函数开头就返回了）。
    if (
        len(bubbles) >= 2
        and not priority
        and random.random() < TAIL_BUBBLE_CHANCE
    ):
        tail = random.choice(TAIL_BUBBLES)
        await asyncio.sleep(min(_bubble_gap() * night, 2.5))
        # 尾泡是装饰性的：发不出去就当没说，不计入失败上报
        # （否则"哈哈没发出去"也会触发一条"没人看到你的消息"归因）
        if await _send(target, tail):
            bubbles.append(tail)
            sent_bubbles.append(tail)

    ctx = f"私聊{private_user_id}" if is_private else f"群{group_id}"
    if len(bubbles) > 1:
        logger.debug(f"拟人发送 [{ctx}]: {len(bubbles)} 条气泡, 总延迟 {total_wait:.1f}s [{mood}]")
    # 发送失败如实上报：调用方据此知道"AI 说的话其实没人看到"
    failed = len(bubbles) - len(sent_bubbles)
    if failed:
        reason = ""
        if hasattr(bot, "last_fail_msg"):
            try:
                reason = bot.last_fail_msg()
            except Exception:
                reason = ""
        logger.warning(f"拟人发送失败 [{ctx}]: {failed}/{len(bubbles)} 条未送达"
                       + (f"（{reason}）" if reason else ""))
        last_send_failure = {"ctx": ctx, "gid": target, "is_private": is_private,
                             "failed": failed, "total": len(bubbles), "reason": reason}
    return sent_bubbles
