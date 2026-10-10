"""Web 日志查看：级别过滤（含 traceback 续行）+ 尾部读取测试。

背景：日志行头是 "[ERROR]" 方括号格式（main.py basicConfig），旧过滤代码
按 "| ERROR " 竖线匹配导致按级别筛选永远为空；traceback 续行没有级别
标记，过滤时必须跟随上一条命中行，否则按 ERROR 筛出的异常没有堆栈。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from web.api import _filter_by_level, _read_log_tail


SAMPLE = [
    "2026-08-25 10:00:00 [INFO] qqbot: 收到消息",
    "2026-08-25 10:00:01 [ERROR] qqbot: AI 聊天异常: boom",
    "Traceback (most recent call last):",
    '  File "bot/chat.py", line 584, in chat_reply',
    "RuntimeError: 全部 provider 上下文超限",
    "2026-08-25 10:00:02 [WARNING] qqbot: provider 重试",
    "2026-08-25 10:00:03 [INFO] qqbot: 结束",
]


def test_filter_error_keeps_traceback_continuation():
    out = _filter_by_level(SAMPLE, "ERROR")
    assert out == [
        "2026-08-25 10:00:01 [ERROR] qqbot: AI 聊天异常: boom",
        "Traceback (most recent call last):",
        '  File "bot/chat.py", line 584, in chat_reply',
        "RuntimeError: 全部 provider 上下文超限",
    ]


def test_filter_info_drops_error_lines_and_their_traceback():
    out = _filter_by_level(SAMPLE, "INFO")
    assert out == [
        "2026-08-25 10:00:00 [INFO] qqbot: 收到消息",
        "2026-08-25 10:00:03 [INFO] qqbot: 结束",
    ]


def test_filter_no_match_returns_empty():
    assert _filter_by_level(SAMPLE, "DEBUG") == []


def test_filter_pipe_style_backcompat():
    # 兼容 "|" 分隔的日志格式（万一格式再变）
    lines = ["2026-08-25 10:00:00 | ERROR | qqbot: old style"]
    assert _filter_by_level(lines, "ERROR") == lines


def test_read_log_tail_small_file_untruncated(tmp_path):
    p = tmp_path / "bot.log"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    lines, truncated = _read_log_tail(str(p))
    assert lines == ["a", "b", "c"]
    assert truncated is False


def test_read_log_tail_big_file_returns_tail_only(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("x" * 100 + "\n" + "y\n", encoding="utf-8")
    lines, truncated = _read_log_tail(str(p), max_bytes=10)
    assert truncated is True
    assert lines == ["y"]  # 半行被丢弃，只留完整行
