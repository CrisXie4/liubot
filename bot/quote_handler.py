"""
消息引用/回复处理模块
- 解析被引用的消息内容
- AI 可使用引用格式回复
- 支持引用文本、图片、视频
"""
import logging
from typing import Optional

from . import db
from .onebot_client import OneBotClient

logger = logging.getLogger("qqbot")


def build_reply_cq(message_id: int) -> str:
    """构建回复引用 CQ 码"""
    return f"[CQ:reply,id={message_id}]"


def build_quote_reply(message_id: int, text: str) -> str:
    """构建带引用的回复消息"""
    return f"{build_reply_cq(message_id)}{text}"


def extract_reply_id(message: list[dict]) -> Optional[int]:
    """从消息段中提取被引用的消息 ID"""
    for seg in message:
        if seg.get("type") == "reply":
            try:
                return int(seg["data"].get("id", 0))
            except (ValueError, TypeError):
                pass
    return None


def extract_reply_text(message: list[dict]) -> Optional[str]:
    """从消息段中提取被引用消息的文本（如果有 text 段）"""
    # OneBot 回复段可能包含被引用消息的文本片段
    for seg in message:
        if seg.get("type") == "reply":
            text = seg.get("data", {}).get("text", "")
            if text:
                return text
    return None


async def get_quoted_context(bot: OneBotClient, reply_msg_id: int) -> Optional[dict]:
    """
    获取被引用消息的完整上下文。
    返回: {"sender_qq": xxx, "sender_name": "xxx", "text": "xxx",
           "has_image": bool, "has_video": bool, "raw_message": "xxx"}
    """
    try:
        msg_data = await bot.get_msg(reply_msg_id)
        if not msg_data:
            return None

        raw = msg_data.get("raw_message", "")
        sender = msg_data.get("sender", {})
        sender_qq = sender.get("user_id", msg_data.get("user_id", 0))
        sender_name = sender.get("card", "") or sender.get("nickname", "")

        # 检查是否有媒体
        has_image = "[CQ:image" in raw
        has_video = "[CQ:video" in raw
        has_face = "[CQ:face" in raw or "[CQ:mface" in raw

        return {
            "sender_qq": sender_qq,
            "sender_name": sender_name,
            "message_id": reply_msg_id,
            "text": raw,
            "has_image": has_image,
            "has_video": has_video,
            "has_face": has_face,
            "raw_message": raw,
        }
    except Exception as e:
        logger.warning(f"获取引用消息内容失败: {e}")
        return None


def format_quoted_context_for_ai(quoted: dict) -> str:
    """将被引用消息格式化为 AI 可理解的文本（带 QQ 号，方便 AI 认人）"""
    if not quoted:
        return ""

    parts = []
    sender_qq = quoted.get("sender_qq", "")
    sender_name = quoted.get("sender_name", "") or f"QQ({sender_qq})"
    sender = f"{sender_name} (QQ={sender_qq})" if sender_qq else sender_name
    text = quoted.get("text", "")
    mid = quoted.get("message_id", "")

    # 标记媒体类型
    media_flags = []
    if quoted.get("has_image"):
        media_flags.append("📷图片")
    if quoted.get("has_video"):
        media_flags.append("📹视频")
    if quoted.get("has_face"):
        media_flags.append("😊表情")

    media_str = f" [{', '.join(media_flags)}]" if media_flags else ""
    id_str = f" [消息ID={mid}]" if mid else ""

    parts.append(f"他引用了 {sender} 的这条消息{media_str}{id_str}:\n> {text[:300]}")

    return "\n".join(parts)
