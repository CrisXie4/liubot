"""Quick smoke test for the rate limiter and SPEAK/SILENT parsing."""
import asyncio
import sys
import os

# Windows console encoding fix
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import rate_limiter
from bot import consciousness as cs


async def test_rate_limiter():
    print("=== Rate Limiter (no timeout, no skip) ===")
    # Force low tokens for deterministic test
    rate_limiter._set_tokens_for_test(2)
    rate_limiter.reset_stats()

    # P0 should succeed immediately
    ok = await rate_limiter.acquire_token("P0")
    print(f"P0 with 2 tokens: {ok} (expect True)")
    assert ok

    # Now 1 token left. P3 should ALSO succeed (no longer skips)
    ok = await rate_limiter.acquire_token("P3")
    print(f"P3 with 1 token: {ok} (expect True — P3 no longer skips)")
    assert ok

    # Now 0 tokens. acquire_token would wait ~60s for refill,
    # so we don't test that path here (would hang the test).
    # Instead force tokens back up.
    rate_limiter._set_tokens_for_test(13)
    ok = await rate_limiter.acquire_token("P3")
    print(f"P3 with 13 tokens: {ok} (expect True)")
    assert ok

    print(f"Stats: {rate_limiter.get_stats()}")
    print("Rate limiter OK\n")


def test_speak_silent_parsing():
    print("=== SPEAK/SILENT parsing ===")
    # Simulate LLM outputs
    cases = [
        ("SPEAK: 卧槽这天气真离谱", "speak", "卧槽这天气真离谱"),
        ("SILENT: 没什么好说的", "silent", "没什么好说的"),
        ("speak: 你好", "speak", "你好"),  # case insensitive
        ("SPEAK:   空格测试  ", "speak", "空格测试"),
        ("garbage response", "silent", None),  # default to silent
    ]
    for raw, expected_type, expected_text in cases:
        upper = raw.upper()
        if upper.startswith("SPEAK:"):
            text = raw.split(":", 1)[1].strip().strip('"').strip("'").strip().split("\n")[0].strip()
            got_type = "speak"
            got_text = text
        elif upper.startswith("SILENT:"):
            got_type = "silent"
            got_text = raw.split(":", 1)[1].strip()[:50]
        else:
            got_type = "silent"
            got_text = None
        ok = got_type == expected_type
        print(f"  «{raw}» → {got_type}/{got_text!r} (expect {expected_type}) {'✓' if ok else '✗'}")
        assert ok, f"Failed: {raw}"
    print("SPEAK/SILENT parsing OK\n")


def test_ignored_tracking():
    print("=== Ignored tracking ===")
    state = {"today": {"proactive_messages": [], "consecutive_ignored": 0}}
    # No messages → 0
    assert cs.check_and_update_ignored(state) == 0
    print("  empty: 0 ✓")

    # Add an old proactive message (30 min ago, beyond 15 min window)
    import time
    old_ts = time.time() - 30 * 60
    state["today"]["proactive_messages"] = [{"ts": old_ts, "group_id": 123}]
    n = cs.check_and_update_ignored(state)
    print(f"  after old msg: consecutive_ignored={n} (expect 1)")
    assert n == 1
    assert state["today"]["proactive_messages"] == []  # removed
    print("  old message counted and removed ✓")

    # Reset on response
    cs.reset_ignored_counter(state)
    print(f"  after reset: {state['today']['consecutive_ignored']} (expect 0)")
    assert state["today"]["consecutive_ignored"] == 0
    print("  reset OK ✓")

    # Recent message (1 min ago) → not ignored yet
    recent_ts = time.time() - 60
    state["today"]["proactive_messages"] = [{"ts": recent_ts, "group_id": 123}]
    n = cs.check_and_update_ignored(state)
    print(f"  recent msg (1min): consecutive_ignored={n} (expect 0)")
    assert n == 0
    assert len(state["today"]["proactive_messages"]) == 1  # kept
    print("  recent message not counted ✓")

    # 兼容旧格式（纯时间戳）
    state["today"]["proactive_messages"] = [old_ts]
    assert cs.check_and_update_ignored(state) == 1
    print("  legacy float entry counted ✓")

    print("Ignored tracking OK\n")


def test_group_response_consumes_pending():
    print("=== Group response consumes pending proactive entries ===")
    import time
    now = time.time()
    state = {"today": {
        "proactive_messages": [
            {"ts": now - 300, "group_id": 1},   # 群1：5 分钟前主动发言
            {"ts": now - 300, "group_id": 2},   # 群2：5 分钟前主动发言
        ],
        "consecutive_ignored": 0,
    }}

    # 用户在群1 说话 → 群1 的主动发言算被接话，群2 不受影响
    cs.note_group_response(state, 1)
    assert [m["group_id"] for m in state["today"]["proactive_messages"]] == [2]
    print("  group-1 response consumed group-1 entry only ✓")

    # 15 分钟后：只有群2 计一次被忽视
    state["today"]["proactive_messages"][0]["ts"] = now - 20 * 60
    n = cs.check_and_update_ignored(state)
    assert n == 1
    print("  only unanswered group counted as ignored ✓")

    # 回应早于主动发言（msg_ts 在发言之前）→ 不消费
    state2 = {"today": {
        "proactive_messages": [{"ts": now, "group_id": 9}],
        "consecutive_ignored": 0,
    }}
    cs.note_group_response(state2, 9, msg_ts=now - 60)
    assert len(state2["today"]["proactive_messages"]) == 1
    print("  earlier-than-proactive message does not consume ✓")

    print("Group response OK\n")


def test_emotion_cap():
    print("=== Emotion cap (boredom/loneliness ≤ 0.85) ===")
    # Use full emotion dict matching DECAY_RATES keys
    state = {"emotions": {
        "joy": 0.5, "calm": 0.5, "energy": 0.5, "curiosity": 0.5, "social": 0.5,
        "irritation": 0.5, "boredom": 0.80, "loneliness": 0.80, "satisfaction": 0.5,
        "sadness": 0.5,
    }}
    # Apply large negative decay (would push past 1.0 without cap)
    cs.apply_natural_decay(state, 100)  # 100 minutes
    boredom = state["emotions"]["boredom"]
    loneliness = state["emotions"]["loneliness"]
    print(f"  after huge growth: boredom={boredom:.3f} loneliness={loneliness:.3f} (expect ≤0.85)")
    assert boredom <= 0.851, f"boredom exceeded cap: {boredom}"
    assert loneliness <= 0.851, f"loneliness exceeded cap: {loneliness}"
    print("  cap OK ✓")

    print("Emotion cap OK\n")


async def main():
    await test_rate_limiter()
    test_speak_silent_parsing()
    test_ignored_tracking()
    test_group_response_consumes_pending()
    test_emotion_cap()
    print("=" * 50)
    print("  ALL SMOKE TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
