"""表情回应感知（emoji_like_note）的一次性消费测试。

约定形状（handler 写入）：state["emoji_like_note"] = {"text": "...", "ts": epoch秒}。
chat 侧只负责 读+清+注入，锁定四件事：
  1. 有效备注读出来并立即清除（一次性）
  2. 键不存在 / 形状不对 / 过期 → 不注入、绝不报错
  3. 注入行格式固定
  4. 二次读取拿不到东西（消费即没）
"""
import time
import uuid

from bot import chat


def _now() -> float:
    return time.time()


def test_valid_note_popped_and_cleared():
    """有效备注：返回文本 + 键被立即清除。"""
    state = {"emoji_like_note": {"text": "3 个😂 1 个👍", "ts": _now()}}
    assert chat.pop_emoji_like_note(state) == "3 个😂 1 个👍"
    assert "emoji_like_note" not in state  # 读取即清除


def test_note_is_one_shot():
    """一次性：消费之后再读就是空。"""
    state = {"emoji_like_note": {"text": "哈哈", "ts": _now()}}
    assert chat.pop_emoji_like_note(state) == "哈哈"
    assert chat.pop_emoji_like_note(state) == ""


def test_missing_key_no_error():
    """键不存在 → 空串，绝不报错。"""
    assert chat.pop_emoji_like_note({}) == ""
    assert chat.pop_emoji_like_note({"emoji_like_note": None}) == ""


def test_malformed_shapes_no_error():
    """形状不对（写方是并行代理，按最坏情况防御）→ 空串 + 键清掉。"""
    for bad in ("哈哈", ["3 个😂"], 42, {"no_text": 1}, {"text": "   "}):
        state = {"emoji_like_note": bad}
        assert chat.pop_emoji_like_note(state) == "", f"shape={bad!r}"
        assert "emoji_like_note" not in state  # 坏数据也要清掉，不能积着


def test_bad_ts_types_no_error():
    """ts 缺失/为 0/非数字 → 一律当作没有，不报错。"""
    assert chat.pop_emoji_like_note({"emoji_like_note": {"text": "哈哈"}}) == ""
    assert chat.pop_emoji_like_note(
        {"emoji_like_note": {"text": "哈哈", "ts": 0}}) == ""
    assert chat.pop_emoji_like_note(
        {"emoji_like_note": {"text": "哈哈", "ts": "abc"}}) == ""


def test_expired_note_not_injected():
    """超过 6 小时的旧回应不再提起（但键照样清）。"""
    state = {"emoji_like_note": {"text": "哈哈", "ts": _now() - 7 * 3600}}
    assert chat.pop_emoji_like_note(state) == ""
    assert "emoji_like_note" not in state


def test_future_ts_beyond_tolerance_rejected():
    """时间戳严重超前（脏数据）→ 当作没有；几分钟内的时钟偏差可容忍。"""
    state = {"emoji_like_note": {"text": "哈哈", "ts": _now() + 3600}}
    assert chat.pop_emoji_like_note(state) == ""
    # 小幅超前（<5 分钟）容忍为有效
    state2 = {"emoji_like_note": {"text": "哈哈", "ts": _now() + 60}}
    assert chat.pop_emoji_like_note(state2) == "哈哈"


def test_injected_block_format():
    """注入行格式固定：『你上一条消息被群友回应了：{text}』。"""
    state = {"emoji_like_note": {"text": "3 个😂 1 个👍", "ts": _now()}}
    assert chat.emoji_like_block(state) == "你上一条消息被群友回应了：3 个😂 1 个👍"
    assert "emoji_like_note" not in state


def test_block_empty_when_nothing_to_say():
    """没东西可注入 → 空串（调用方按空串跳过 dynamic block）。"""
    assert chat.emoji_like_block({}) == ""
    assert chat.emoji_like_block(
        {"emoji_like_note": {"text": "哈哈", "ts": _now() - 8 * 3600}}) == ""


def test_unique_state_isolated():
    """不同 state 互不影响（每次传入的都是独立意识状态快照）。"""
    tag = uuid.uuid4().hex[:8]
    s1 = {"emoji_like_note": {"text": f"n1-{tag}", "ts": _now()}}
    s2 = {}
    assert chat.pop_emoji_like_note(s1) == f"n1-{tag}"
    assert chat.pop_emoji_like_note(s2) == ""
