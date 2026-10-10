"""活水位窗测试：群聊热长窗 / 冷场短窗 / 切换防抖 / 冻结起点不滑动。

纯逻辑测试，不联网不调 LLM。锁定三件事：
  1. 冷热判定与防抖（一条消息不能让窗长来回跳）
  2. 高/低水位窗口行为（低水位内只涨不缩，超高水位一批缩回低水位）
  3. 持久锚点（压缩摘要）不受窗口缩放影响
"""
import time
import uuid

import pytest

from bot import chat


def _gid() -> int:
    """运行时随机群号：避免用例间共享模块级状态。"""
    return 900_000_000 + int(uuid.uuid4().int % 100_000_000)


def _msg(i: int, ts: float, content: str = "普通消息") -> dict:
    return {"role": "user", "content": f"{content}{i}", "importance": 0.5,
            "ts": ts, "msg_id": i}


@pytest.fixture(autouse=True)
def _clean():
    yield
    # clear_context 会把 _wm_state / _chat_contexts / 冻结窗口一并清掉
    for gid in list(chat._wm_state):
        chat.clear_context(gid)


# ─── 冷热判定 ───

def test_group_is_hot_thresholds():
    """3 分钟内 >=3 条算热闹；2 条（一问一答）不算；超时的消息不算数。"""
    now = time.time()
    gid = _gid()
    chat.clear_context(gid)

    cold_ctx = [_msg(i, now - 20) for i in range(2)]
    assert chat._group_is_hot(cold_ctx, now) is False

    hot_ctx = [_msg(i, now - 10 * i) for i in range(3)]
    assert chat._group_is_hot(hot_ctx, now) is True

    # 三条里只有两条在窗内（最旧的 4 分钟前）→ 不算热闹
    stale_ctx = [_msg(0, now - 240)] + [_msg(i, now - 5 * i) for i in range(1, 3)]
    assert chat._group_is_hot(stale_ctx, now) is False
    chat.clear_context(gid)


def test_cold_group_starts_with_short_window():
    """没观察过热闹 → 冷，窗长 = COLD_WINDOW_MSGS。"""
    now = time.time()
    gid = _gid()
    ctx = [_msg(i, now - 3600 + i) for i in range(30)]  # 全是旧消息
    assert chat._window_msg_budget(gid, ctx, now) == chat.COLD_WINDOW_MSGS


def test_hot_switch_needs_two_obs_and_dwell():
    """防抖：翻转到热需要连续 2 次同向观察 + 距建立状态满 dwell。"""
    now = time.time()
    gid = _gid()
    chat.clear_context(gid)
    cold_ctx = [_msg(i, now - 3600 + i) for i in range(30)]
    hot_ctx = [_msg(i, now - 10 * i) for i in range(6)]

    # 第 1 次热观察：状态刚建立（dwell 不足）→ 仍是冷窗
    assert chat._window_msg_budget(gid, cold_ctx, now) == chat.COLD_WINDOW_MSGS
    assert chat._window_msg_budget(gid, hot_ctx, now + 5) == chat.COLD_WINDOW_MSGS
    # dwell 满了 + 已连续 2 次热观察 → 翻转
    assert chat._window_msg_budget(gid, hot_ctx, now + 61) == chat.HOT_WINDOW_MSGS
    chat.clear_context(gid)


def test_single_message_cannot_flip_mode():
    """防抖核心：单条消息（1 次热观察后马上又冷）绝不能翻转模式。"""
    now = time.time()
    gid = _gid()
    chat.clear_context(gid)
    cold_ctx = [_msg(i, now - 3600 + i) for i in range(30)]
    hot_ctx = [_msg(i, now - 10 * i) for i in range(6)]

    assert chat._window_msg_budget(gid, cold_ctx, now) == chat.COLD_WINDOW_MSGS
    # 一条热闹消息（热观察 1 次）→ 仍冷
    assert chat._window_msg_budget(gid, hot_ctx, now + 10) == chat.COLD_WINDOW_MSGS
    # 立刻又冷下去 → streak 清零，还是冷窗
    assert chat._window_msg_budget(gid, cold_ctx, now + 20) == chat.COLD_WINDOW_MSGS
    chat.clear_context(gid)


def test_flip_to_cold_also_debounced():
    """热 → 冷同样要防抖：热闹了很久的群，一条冷观察不该立刻收窗。"""
    now = time.time()
    gid = _gid()
    chat.clear_context(gid)
    hot_ctx = [_msg(i, now - 10 * i) for i in range(6)]
    cold_ctx = [_msg(i, now - 3600 + i) for i in range(30)]

    # 先把模式稳固成热（跨过 streak+dwell）
    assert chat._window_msg_budget(gid, hot_ctx, now) == chat.COLD_WINDOW_MSGS
    assert chat._window_msg_budget(gid, hot_ctx, now + 61) == chat.HOT_WINDOW_MSGS
    st = chat._wm_state[gid]
    st["since"] = now  # 人为把上次切换时间拨回去，模拟"热了很久"

    # 第 1 次冷观察（dwell 已满但 streak 只有 1）→ 仍是热窗
    assert chat._window_msg_budget(gid, cold_ctx, now + 120) == chat.HOT_WINDOW_MSGS
    # 第 2 次冷观察 → 翻转成冷
    assert chat._window_msg_budget(gid, cold_ctx, now + 180) == chat.COLD_WINDOW_MSGS
    chat.clear_context(gid)


# ─── 窗口水位行为 ───

def _settings(monkeypatch):
    monkeypatch.setattr(
        chat.memory, "load_model_settings",
        lambda: {"context_size": 400, "max_context_tokens": 800000},
    )


def test_window_grows_inside_high_watermark_without_sliding(monkeypatch):
    """低水位内追加消息：起点冻结不动，窗口只往尾部涨（前缀缓存不碎）。"""
    _settings(monkeypatch)
    gid = _gid()
    chat.clear_context(gid)
    now = time.time()
    chat._chat_contexts[gid] = [_msg(i, now - 3600 + i) for i in range(40)]

    msgs1 = chat._build_context_messages(gid)
    assert len(msgs1) == chat.COLD_WINDOW_MSGS  # 冷窗低水位

    chat._chat_contexts[gid].extend(
        _msg(40 + i, now - 3600 + 40 + i) for i in range(3))
    msgs2 = chat._build_context_messages(gid)

    # 起点没动：msgs1 原样是 msgs2 的前缀，尾部只多了新消息
    assert len(msgs2) == len(msgs1) + 3
    assert [m["content"] for m in msgs2[:len(msgs1)]] == \
        [m["content"] for m in msgs1]
    chat.clear_context(gid)


def test_window_retrims_over_high_watermark(monkeypatch):
    """冻结窗涨过高水位（低水位+余量）才重裁，重裁直接缩回低水位。"""
    _settings(monkeypatch)
    gid = _gid()
    chat.clear_context(gid)
    now = time.time()
    chat._chat_contexts[gid] = [_msg(i, now - 3600 + i) for i in range(40)]
    chat._build_context_messages(gid)  # 冻结冷窗起点（低水位 8）

    # 追加 9 条 → 冻结窗 17 条 > 8+8（高水位）→ 重裁
    chat._chat_contexts[gid].extend(
        _msg(40 + i, now - 3600 + 40 + i) for i in range(9))
    msgs = chat._build_context_messages(gid)

    assert len(msgs) == chat.COLD_WINDOW_MSGS  # 缩回低水位
    # 而且是最新的 8 条：最后一条是 48
    assert msgs[-1]["content"].endswith("48")
    chat.clear_context(gid)


def test_start_never_slides_backwards_across_retrims(monkeypatch):
    """连续多轮追加+重裁：窗口起点只能往后挪（丢更旧的），绝不回滑。"""
    _settings(monkeypatch)
    gid = _gid()
    chat.clear_context(gid)
    now = time.time()
    base = 0
    chat._chat_contexts[gid] = [_msg(i, now - 3600 + i) for i in range(40)]

    last_start_msg = None
    for r in range(6):
        for i in range(5):
            base += 1
            chat._chat_contexts[gid].append(_msg(base, now - 3600 + base))
        msgs = chat._build_context_messages(gid)
        if last_start_msg is not None:
            # 上一轮窗口的首条消息，要么还在窗口里，要么已经被裁掉
            contents = [m["content"] for m in msgs]
            if last_start_msg in contents:
                assert contents.index(last_start_msg) == 0, \
                    "旧消息出现在窗口中间——起点回滑了"
        last_start_msg = msgs[0]["content"]
    chat.clear_context(gid)


def test_summary_anchor_survives_window_shrink(monkeypatch):
    """持久锚点：压缩摘要永远在最前，不受冷热窗缩放影响。"""
    _settings(monkeypatch)
    gid = _gid()
    chat.clear_context(gid)
    chat._compressed_summaries[gid] = "昨天大家聊了晚饭吃什么"
    now = time.time()
    chat._chat_contexts[gid] = [_msg(i, now - 3600 + i) for i in range(40)]

    msgs = chat._build_context_messages(gid)
    assert msgs[0]["content"].startswith("[之前的对话摘要")
    assert "昨天大家聊了晚饭吃什么" in msgs[0]["content"]

    # 人为切到热模式（直接拨状态，跳过防抖）并重裁一次，摘要锚点不动
    chat._wm_state[gid] = {"hot": True, "streak": 0, "since": time.time()}
    chat._frozen_window.pop(gid, None)  # 模式切换后的重裁
    msgs_hot = chat._build_context_messages(gid)
    assert msgs_hot[0]["content"] == msgs[0]["content"]
    assert len(msgs_hot) > len(msgs)  # 热窗确实更长
    chat.clear_context(gid)


def test_hot_window_allows_more_history(monkeypatch):
    """热窗低水位 20：同一段热闹对话，能看到的历史比冷窗多。"""
    _settings(monkeypatch)
    gid = _gid()
    chat.clear_context(gid)
    now = time.time()
    # 全部最近 3 分钟内 → 稳定观测为热
    chat._chat_contexts[gid] = [
        _msg(i, now - (40 - i) * 2) for i in range(40)
    ]
    chat._wm_state[gid] = {"hot": True, "streak": 0, "since": time.time()}

    msgs_hot = chat._build_context_messages(gid)
    assert len(msgs_hot) >= chat.HOT_WINDOW_MSGS

    chat._wm_state[gid] = {"hot": False, "streak": 0, "since": time.time()}
    chat._frozen_window.pop(gid, None)  # 换模式模拟一次重裁
    msgs_cold = chat._build_context_messages(gid)
    assert len(msgs_cold) <= chat.COLD_WINDOW_MSGS + chat.WM_HIGH_MARGIN
    chat.clear_context(gid)


def test_select_window_max_count_zero_keeps_old_behavior():
    """max_count=0（默认）时行为与旧版完全一致：只看 token 预算。"""
    gid = _gid()
    chat.clear_context(gid)
    chat._chat_contexts[gid] = [_msg(i, 1000.0 + i) for i in range(50)]
    start1 = chat._select_window(gid, chat._chat_contexts[gid], 100000)
    chat._chat_contexts[gid].extend(_msg(50 + i, 1050.0 + i) for i in range(5))
    start2 = chat._select_window(gid, chat._chat_contexts[gid], 100000)
    assert start2 == start1
    chat.clear_context(gid)
