"""
群事件感知 —— 让 AI 知道群里刚才发生了什么
============================================
问题背景:
  handle_notice 收到撤回/禁言/踢人等通知，但以前只打日志、不喂给 AI。
  AI 对群事件"瞎"，反应慢几拍。

本模块提供内存环形缓冲（不持久化——只要实时感知），存每群最近 N 条事件，
供 _build_roster 末尾拼成「【群里最近发生的事】」注入 AI 上下文。
"""
import logging
import time
from collections import deque

logger = logging.getLogger("qqbot")

# 每群最多保留多少条事件
EVENT_KEEP = 8
# 事件保留多久（秒）—— 30 分钟外的视为过期
EVENT_TTL = 1800

# 内存状态: {group_id: deque[event_dict]}
_events: dict[int, deque] = {}

# 事件类型 → 中文描述（用于 build_block）
_TYPE_LABEL = {
    "recall": "撤回了消息",
    "mute": "被禁言",
    "unmute": "被解除禁言",
    "kick": "被踢出群",
    "leave": "退群了",
    "join": "加入了群",
}


def record(group_id: int, event_type: str, who_qq: int,
           who_name: str = "", detail: str = "") -> None:
    """记录一条群事件。

    event_type: recall / mute / unmute / kick / leave / join
    who_qq: 事件主角的 QQ（被禁言的人、撤回消息的人等）
    who_name: 主角的名字（可为空，build 时再补）
    detail: 补充信息（如禁言时长 "600s"）
    """
    if not group_id or not who_qq:
        return
    dq = _events.setdefault(group_id, deque(maxlen=EVENT_KEEP))
    dq.append({
        "ts": time.time(),
        "type": event_type,
        "who_qq": who_qq,
        "who_name": who_name or str(who_qq),
        "detail": detail,
    })


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    return f"{int(seconds // 3600)}小时前"


def build_recent_events_block(group_id: int, limit: int = 5) -> str:
    """生成「【群里最近发生的事】」提示块。无内容时返回空串。

    返回的每条形如："- 嘟嘟 (QQ=123456) 被禁言 · 600s · 3分钟前"
    按时间倒序（最新在最前），limit 限制条数。
    """
    dq = _events.get(group_id)
    if not dq:
        return ""
    now = time.time()
    lines = []
    # 从最新往回看
    for ev in reversed(dq):
        if now - ev["ts"] > EVENT_TTL:
            continue
        label = _TYPE_LABEL.get(ev["type"], ev["type"])
        name = ev["who_name"] or str(ev["who_qq"])
        age = _fmt_age(now - ev["ts"])
        detail = f" · {ev['detail']}" if ev["detail"] else ""
        lines.append(f"- {name} (QQ={ev['who_qq']}) {label}{detail} · {age}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "【群里最近发生的事】\n" + "\n".join(lines)


def clear_group(group_id: int) -> None:
    """清理某群的事件缓冲（调试/重置用）"""
    _events.pop(group_id, None)
