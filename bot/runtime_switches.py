"""
运行时开关 — Bot 主开关 / AI 主开关
====================================
两个全局开关，UI 一键控制：

  - bot_enabled:    False 时机器人完全不发消息、不响应（@了也不回）
  - ai_enabled:     False 时所有 LLM 调用短路，直接用兜底回复
                   （仅审核仍工作；主动聊天/记忆提取/拟人发言全部停）

默认值都开。重启进程会回到默认值，配置改 .env 可永久关。
"""
import logging
from . import config

logger = logging.getLogger("qqbot")

# 启动时按 .env 里的开关初始化；之后可被 API 改
_bot_on = bool(int(getattr(config, "BOT_ENABLED", 1) or 1))
_ai_on = bool(int(getattr(config, "AI_ENABLED", 1) or 1))


def is_bot_on() -> bool:
    return _bot_on


def set_bot_on(on: bool):
    global _bot_on
    _bot_on = bool(on)
    logger.warning(f"Bot 主开关 → {'开' if on else '关'}")


def is_ai_on() -> bool:
    return _ai_on


def set_ai_on(on: bool):
    global _ai_on
    _ai_on = bool(on)
    logger.warning(f"AI 主开关 → {'开' if on else '关'}")


def snapshot() -> dict:
    return {"bot_enabled": _bot_on, "ai_enabled": _ai_on}