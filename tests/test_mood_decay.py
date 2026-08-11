"""情绪衰减测试 — 验证负向情绪衰减到低基线，不卡在 0.5"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import apply_natural_decay, EMOTION_BASELINE


def _full_emotions(**overrides):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base.update(overrides)
    return base


class TestMoodDecay(unittest.TestCase):

    def test_irritation_decays_to_low_baseline(self):
        state = {"emotions": _full_emotions(irritation=0.51)}
        apply_natural_decay(state, 60.0)
        self.assertLess(state["emotions"]["irritation"], 0.45,
                        "irritation 应明显低于 0.5，不再卡死")

    def test_irritation_baseline_is_low(self):
        self.assertLessEqual(EMOTION_BASELINE["irritation"], 0.15)

    def test_joy_decays_to_neutral_positive(self):
        state = {"emotions": _full_emotions(joy=0.9)}
        apply_natural_decay(state, 120.0)
        self.assertGreater(state["emotions"]["joy"], 0.5)

    def test_boredom_growth_caps_at_baseline(self):
        state = {"emotions": _full_emotions(boredom=0.2)}
        apply_natural_decay(state, 60.0)
        self.assertLessEqual(state["emotions"]["boredom"], 0.86)

    def test_sadness_decays_low(self):
        state = {"emotions": _full_emotions(sadness=0.7)}
        apply_natural_decay(state, 60.0)
        self.assertLess(state["emotions"]["sadness"], 0.5)


if __name__ == "__main__":
    unittest.main()
