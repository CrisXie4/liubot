"""状态迁移 v1→v2 测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import _migrate_v1_to_v2, EMOTION_BASELINE


def _full_emotions(**overrides):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base.update(overrides)
    return base


class TestStateMigration(unittest.TestCase):

    def test_stuck_irritation_with_high_joy_resets(self):
        state = {
            "version": 1,
            "emotions": _full_emotions(irritation=0.5098, joy=0.57,
                                       curiosity=0.99, satisfaction=0.86),
            "sleeping": False,
        }
        _migrate_v1_to_v2(state)
        self.assertEqual(state["version"], 2)
        self.assertLess(state["emotions"]["irritation"], 0.2,
                        "卡死的 irritation 应被重置到低值")

    def test_normal_low_irritation_not_touched(self):
        state = {"version": 1, "emotions": _full_emotions(irritation=0.15, joy=0.6)}
        _migrate_v1_to_v2(state)
        self.assertAlmostEqual(state["emotions"]["irritation"], 0.15)

    def test_genuinely_grumpy_kept(self):
        state = {"version": 1, "emotions": _full_emotions(irritation=0.8, joy=0.2)}
        _migrate_v1_to_v2(state)
        self.assertGreaterEqual(state["emotions"]["irritation"], 0.7)

    def test_loneliness_capped(self):
        state = {"version": 1, "emotions": _full_emotions(irritation=0.1, joy=0.5, loneliness=1.0)}
        _migrate_v1_to_v2(state)
        self.assertLess(state["emotions"]["loneliness"], 0.9)

    def test_already_v2_noop(self):
        state = {"version": 2, "emotions": _full_emotions(irritation=0.51, joy=0.57)}
        _migrate_v1_to_v2(state)
        self.assertAlmostEqual(state["emotions"]["irritation"], 0.51)


if __name__ == "__main__":
    unittest.main()
