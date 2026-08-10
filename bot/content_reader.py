"""
统一内容提取器 — 把任意 OneBot 消息拍平成结构化 payload。

合并转发（[CQ:forward,id=xxx]）通过 NapCat get_forward_msg API 反查；
嵌套转发递归展开（深度上限 3）；图片/表情从 segments 提取。

自动审核（handler）和手动标记（violation）两条路径共享此提取器，
消除"不对称 bug"。
"""
import asyncio
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("qqbot")

# ─── 常量 ───
FORWARD_DEPTH_LIMIT = 3
FORWARD_SUBMSG_LIMIT = 50
FORWARD_CACHE_TTL = 300  # 5 分钟

# resId → (result_text, ts)
_forward_cache: dict[str, tuple] = {}

# ─── CQ 码解析（与 media_reader 一致的 regex）───
_CQ_FORWARD_RE = re.compile(r"\[CQ:forward,id=([^\],]+)\]")


async def extract_full_content(bot, raw_message: str, segments: list[dict]) -> dict:
    """
    把任意 OneBot 消息拍平。

    Args:
        bot: OneBotClient（用其 get_forward_msg）
        raw_message: 原始 CQ 字符串
        segments: 消息段数组（event["message"]）

    Returns: 见模块 docstring 的 schema
    """
    text_parts: list[str] = []
    images: list[dict] = []
    stickers: list[dict] = []
    errors: list[str] = []
    has_forward = False
    forward_block: Optional[dict] = None

    # 1. 从 segments 提取文本/图片/表情
    for seg in segments or []:
        t = seg.get("type", "")
        data = seg.get("data", {})
        if t == "text":
            text_parts.append(data.get("text", ""))
        elif t == "image":
            url = data.get("url", "")
            file_id = data.get("file", "")
            if url:
                images.append({"url": url, "file_id": file_id})
        elif t == "mface":
            url = data.get("url", "")
            if url:
                stickers.append({"url": url, "summary": data.get("summary", "")})
        # face / record / video 等：本期 images/stickers 不收

    # 2. 兜底：如果 segments 没有 text，从 raw_message 剥 CQ 码取纯文本
    if not text_parts and raw_message:
        clean = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
        if clean:
            text_parts.append(clean)

    # 3. 检测合并转发
    forward_ids = _CQ_FORWARD_RE.findall(raw_message or "")
    if forward_ids:
        has_forward = True
        fwd_text_parts: list[str] = []
        fwd_images: list[dict] = []
        fwd_stickers: list[dict] = []
        total_count = 0
        nested_seen = False

        for fid in forward_ids:
            sub_text, sub_imgs, sub_stks, sub_nested, sub_count, sub_errors = await _unfold_forward(
                bot, fid, depth=1
            )
            fwd_text_parts.append(sub_text)
            fwd_images.extend(sub_imgs)
            fwd_stickers.extend(sub_stks)
            if sub_nested:
                nested_seen = True
            total_count += sub_count
            errors.extend(sub_errors)

        forward_block = {
            "text": "\n".join(p for p in fwd_text_parts if p),
            "images": fwd_images,
            "stickers": fwd_stickers,
            "count": total_count,
            "nested": nested_seen,
        }

    return {
        "text": "".join(text_parts).strip(),
        "images": images,
        "stickers": stickers,
        "has_forward": has_forward,
        "forward": forward_block,
        "errors": errors,
    }


async def _unfold_forward(bot, forward_id: str, depth: int) -> tuple:
    """
    递归展开合并转发。返回 (text, images, stickers, nested, count, errors)。

    深度超过 FORWARD_DEPTH_LIMIT 时停止递归，记错误。
    """
    text_parts: list[str] = []
    images: list[dict] = []
    stickers: list[dict] = []
    errors: list[str] = []
    nested = False
    count = 0

    # 深度上限检查
    if depth > FORWARD_DEPTH_LIMIT:
        errors.append(f"合并转发嵌套深度超限 (id={forward_id}, depth={depth})")
        return ("", images, stickers, nested, count, errors)

    # 缓存命中
    cached = _forward_cache.get(forward_id)
    if cached and time.time() - cached[3] < FORWARD_CACHE_TTL:
        # 注意：缓存只存扁平内容（不递归），简化处理
        return (cached[0], cached[1], cached[2], nested, count, errors)

    # 调 NapCat API
    try:
        data = await bot.get_forward_msg(id=forward_id)
    except Exception as e:
        errors.append(f"get_forward_msg 异常 (id={forward_id}): {str(e)[:80]}")
        return ("[合并转发-无法解析]", images, stickers, nested, count, errors)

    if not data or not isinstance(data, dict):
        errors.append(f"合并转发无法解析 (id={forward_id})")
        return ("[合并转发-无法解析]", images, stickers, nested, count, errors)

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    # 子消息条数上限
    truncated = False
    if len(messages) > FORWARD_SUBMSG_LIMIT:
        messages = messages[:FORWARD_SUBMSG_LIMIT]
        truncated = True

    for sub in messages:
        count += 1
        nickname = sub.get("nickname", "")
        sub_segs = sub.get("message", [])
        if not isinstance(sub_segs, list):
            sub_segs = []
        sub_text_parts: list[str] = []
        for seg in sub_segs:
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                sub_text_parts.append(d.get("text", ""))
            elif t == "image":
                url = d.get("url", "")
                if url:
                    images.append({"url": url, "file_id": d.get("file", "")})
            elif t == "mface":
                url = d.get("url", "")
                if url:
                    stickers.append({"url": url, "summary": d.get("summary", "")})
            elif t == "forward":
                nested = True
                nested_id = d.get("id", "")
                if nested_id and depth < FORWARD_DEPTH_LIMIT:
                    n_text, n_imgs, n_stks, n_nested, n_count, n_errors = await _unfold_forward(
                        bot, nested_id, depth + 1
                    )
                    sub_text_parts.append(n_text)
                    images.extend(n_imgs)
                    stickers.extend(n_stks)
                    if n_nested:
                        nested = True
                    count += n_count
                    errors.extend(n_errors)
                elif nested_id:
                    errors.append(f"嵌套转发未展开 (depth={depth+1}, id={nested_id})")
        sub_text = "".join(sub_text_parts).strip()
        if sub_text:
            prefix = f"[{nickname}] " if nickname else ""
            text_parts.append(f"{prefix}{sub_text}")

    if truncated:
        errors.append(f"子消息超过 {FORWARD_SUBMSG_LIMIT} 条上限 (id={forward_id})")

    combined = "\n".join(text_parts)
    # 写缓存
    _forward_cache[forward_id] = (combined, images, stickers, time.time())
    return (combined, images, stickers, nested, count, errors)
