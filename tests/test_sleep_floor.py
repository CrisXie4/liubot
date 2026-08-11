"""睡眠底线测试 — 24h 内必须睡够 60 分钟"""
import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import consciousness as cs
from bot.consciousness import EMOTION_BASELINE


def _full_emotions(**overrides):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base.update(overrides)
    return base


def _state_awake_23h():
    return {
        "sleeping": False,
        "sleepiness": 0.5,
        "sleeping_until": "",
        "last_24h_sleep_minutes": 0,
        "last_wake_ts": (datetime.now() - timedelta(hours=23)).timestamp(),
        "version": 2,
        "emotions": _full_emotions(energy=0.2, irritation=0.1, sleepiness=0.5),
        "today": {"date": datetime.now().strftime("%Y-%m-%d"), "messages_sent": 0,
                  "interactions": 0, "new_friends": 0, "praised": 0, "insulted": 0,
                  "ignored_count": 0, "interesting_topics": [], "highlights": [],
                  "proactive_messages": [], "consecutive_ignored": 0},
        "diary": [],
        "relationships": {},
        "traits": {},
        "idle_minutes_today": 0,
        "idle_thoughts_today": 0,
        "_idle_think_count": 0,
    }


class TestSleepFloor(unittest.TestCase):

    def test_23h_awake_triggers_forced_sleep(self):
        s = _state_awake_23h()
        cs._check_forced_sleep(s)
        self.assertTrue(s["sleeping"])

    def test_forced_sleep_meets_60min_floor(self):
        s = _state_awake_23h()
        cs._check_forced_sleep(s)
        import time as _time
        from datetime import datetime as _dt
        wake_at = _dt.fromisoformat(s["sleeping_until"])
        span_min = (wake_at.timestamp() - _time.time()) / 60
        self.assertGreaterEqual(span_min, 59, "强制睡时长应 >= 60 分钟")

    def test_extreme_sleepiness_triggers_sleep(self):
        s = _state_awake_23h()
        s["sleepiness"] = 0.99
        s["last_wake_ts"] = datetime.now().timestamp()
        cs._check_forced_sleep(s)
        self.assertTrue(s["sleeping"])


if __name__ == "__main__":
    unittest.main()
