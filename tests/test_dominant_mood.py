"""get_dominant_mood 优先级测试 — 修复边缘 irritation 卡死 grumpy"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import get_dominant_mood, EMOTION_BASELINE


def _state(**emotions):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base.update(emotions)
    return {"emotions": base, "sleeping": False}


class TestDominantMood(unittest.TestCase):

    def test_edge_irritation_with_high_joy_is_happy(self):
        """irritation=0.51 + joy=0.57 → happy，不是 grumpy（核心 bug 场景）"""
        s = _state(irritation=0.51, joy=0.57, energy=0.45, satisfaction=0.86, curiosity=0.99)
        self.assertEqual(get_dominant_mood(s), "happy")

    def test_high_irritation_still_grumpy(self):
        s = _state(irritation=0.7, joy=0.4)
        self.assertEqual(get_dominant_mood(s), "grumpy")

    def test_excited_when_joy_and_energy_high(self):
        s = _state(joy=0.8, energy=0.7, irritation=0.2)
        self.assertEqual(get_dominant_mood(s), "excited")

    def test_curious_when_low_irritation(self):
        s = _state(curiosity=0.85, joy=0.4, energy=0.5, irritation=0.2)
        self.assertEqual(get_dominant_mood(s), "curious")

    def test_grumpy_threshold_is_06(self):
        """irritation=0.55（低于新阈值 0.6）+ 低 joy → 不是 grumpy"""
        s = _state(irritation=0.55, joy=0.3, energy=0.3)
        mood = get_dominant_mood(s)
        self.assertNotEqual(mood, "grumpy")

    def test_sleeping_overrides_all(self):
        s = _state(joy=0.9, energy=0.9)
        s["sleeping"] = True
        self.assertEqual(get_dominant_mood(s), "sleeping")


if __name__ == "__main__":
    unittest.main()
