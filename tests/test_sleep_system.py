"""睡眠系统测试 — sleepiness 驱动 + go_to_sleep + wake_up + tick 集成"""
import sys
import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import consciousness as cs
from bot.consciousness import EMOTION_BASELINE


def _full_emotions(**overrides):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base.update(overrides)
    return base


def _state(sleepiness=0.0, energy=0.5, sleeping=False, last_wake_ts=None, last_24h=0,
           irritation=0.1):
    if last_wake_ts is None:
        last_wake_ts = datetime.now().timestamp()
    s = {
        "sleepiness": sleepiness,
        "sleeping": sleeping,
        "sleeping_until": "",
        "last_24h_sleep_minutes": last_24h,
        "last_wake_ts": last_wake_ts,
        "version": 2,
        "emotions": _full_emotions(energy=energy, irritation=irritation, sleepiness=sleepiness),
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
    if sleeping:
        s["_sleep_start_ts"] = datetime.now().timestamp() - 3600
    return s


class TestSleepinessAccumulation(unittest.TestCase):

    @patch.object(cs.timeutil, 'now_shanghai')
    def test_deep_night_accumulates_fast(self, mock_now):
        mock_now.return_value = datetime(2026, 8, 11, 3, 0)
        s = _state(sleepiness=0.0)
        cs.update_sleepiness(s, elapsed_minutes=30)
        self.assertGreater(s["sleepiness"], 0.3)

    @patch.object(cs.timeutil, 'now_shanghai')
    def test_afternoon_accumulates_slow(self, mock_now):
        mock_now.return_value = datetime(2026, 8, 11, 14, 0)
        s = _state(sleepiness=0.0)
        cs.update_sleepiness(s, elapsed_minutes=10)
        self.assertLess(s["sleepiness"], 0.1)


class TestGoToSleep(unittest.TestCase):

    def test_go_to_sleep_sets_sleeping_until(self):
        s = _state()
        cs.go_to_sleep(s, minutes=90)
        self.assertTrue(s["sleeping"])
        self.assertTrue(len(s["sleeping_until"]) > 0)

    def test_go_to_sleep_enforces_floor_when_deficient(self):
        s = _state(last_24h=30)
        cs.go_to_sleep(s, minutes=20)
        import time as _time
        from datetime import datetime as _dt
        wake_at = _dt.fromisoformat(s["sleeping_until"])
        now_ts = _time.time()
        wake_ts = wake_at.timestamp()
        span_min = (wake_ts - now_ts) / 60
        self.assertGreaterEqual(span_min + 30, 59, "不足 60 分钟应被补齐")

    def test_go_to_sleep_clamps_minutes(self):
        s = _state()
        cs.go_to_sleep(s, minutes=999)
        self.assertTrue(s["sleeping"])


class TestWakeUp(unittest.TestCase):

    def test_wake_up_restores_energy(self):
        s = _state(sleeping=True, energy=0.2)
        s["sleeping_until"] = "2099-01-01T00:00:00"
        cs.wake_up(s)
        self.assertFalse(s["sleeping"])
        self.assertGreater(s["emotions"]["energy"], 0.2)
        self.assertEqual(s["sleepiness"], 0.0)

    def test_wake_up_cranky_increases_irritation(self):
        s = _state(sleeping=True, energy=0.2, irritation=0.1)
        s["sleeping_until"] = "2099-01-01T00:00:00"
        before = s["emotions"]["irritation"]
        cs.wake_up(s, cranky=True)
        self.assertGreater(s["emotions"]["irritation"], before)


class TestTickIntegration(unittest.TestCase):

    def test_tick_updates_sleepiness(self):
        s = _state(sleepiness=0.0)
        cs.tick(s)
        self.assertGreaterEqual(s["sleepiness"], 0.0)

    def test_tick_wakes_when_sleeping_until_passed(self):
        s = _state(sleeping=True, energy=0.2)
        s["sleeping_until"] = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        cs.tick(s)
        self.assertFalse(s["sleeping"])


if __name__ == "__main__":
    unittest.main()
