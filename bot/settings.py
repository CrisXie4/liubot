"""
Bot 运行时设置管理
- 所有可通过 Web UI 修改的配置项
- 自动持久化到 data/bot_settings.json
- 优先级: 运行时设置 > .env 默认值
"""
import json
import logging
import os
from typing import Any

logger = logging.getLogger("qqbot")

SETTINGS_PATH = ""  # 在 init 时设置


def _path() -> str:
    if not SETTINGS_PATH:
        from .config import config
        return os.path.join(config.DATA_DIR, "bot_settings.json")
    return SETTINGS_PATH


DEFAULTS: dict[str, Any] = {
    "bot_name": "小Q",
    "auto_mod_enabled": True,
    "auto_accept_invite": False,
    "chat_context_size": 10,
    "owner_notifications": {
        "on_violation": True,       # 有人违规时通知
        "on_high_risk": True,        # 高置信度违规通知
        "on_kick_request": True,     # 需要审批踢人时通知
        "on_new_group": False,       # 被拉入新群通知
        "on_error": True,            # Bot 异常通知
    },
    "auto_actions": {
        "mute_duration_low": 600,    # 低置信度禁言 10分钟
        "mute_duration_high": 3600,  # 高置信度禁言 1小时
        "auto_mute_enabled": True,   # 自动禁言开关
        "auto_recall_enabled": True, # 自动撤回开关
    },
    # ── 拟人化：引用 / @ 的克制程度 ──
    # 真人聊天极少引用，也很少 @。这几个阈值决定"什么时候才值得引用"
    "reply_policy": {
        "quote_gap_msgs": 3,        # 目标消息后面隔了 >= N 条，才值得引用
        "quote_gap_seconds": 180,   # 或者已经过了 N 秒
        "at_when_absent": True,     # 只在对方"不在当前对话里"时才允许 @
        "max_quotes": 1,            # 单条回复最多保留几个引用
    },
    # ── 拟人化：深夜静默时段（这段时间不主动冒泡，被 @ 了还是会回）──
    "quiet_hours": {
        "enabled": True,
        "start": 1,   # 01:00
        "end": 7,     # 07:00
    },
}


def load() -> dict:
    """加载运行时设置（合并默认值）"""
    try:
        if os.path.exists(_path()):
            with open(_path(), "r", encoding="utf-8") as f:
                saved = json.load(f)
        else:
            saved = {}
    except Exception:
        saved = {}

    settings = dict(DEFAULTS)
    # 深度合并（支持嵌套字典）
    for k, v in saved.items():
        if k in settings and isinstance(settings[k], dict) and isinstance(v, dict):
            settings[k].update(v)
        else:
            settings[k] = v
    return settings


def save(updates: dict) -> dict:
    """保存并返回完整设置"""
    current = load()
    current.update(updates)
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    logger.info(f"Bot 设置已保存: {list(updates.keys())}")
    return current


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)
