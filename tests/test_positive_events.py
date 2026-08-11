"""正向事件降低 irritation 测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import on_mentioned, on_replied_to_bot, on_praised, EMOTION_BASELINE


def _state(irritation=0.5):
    base = {k: 0.5 for k in EMOTION_BASELINE}
    base["irritation"] = irritation
    return {
        "emotions": base,
        "today": {"interactions": 0, "praised": 0, "highlights": []},
        "relationships": {},
        "traits": {"stubbornness": 0.5},
    }


class TestPositiveEventsReduceIrritation(unittest.TestCase):

    def test_on_mentioned_reduces_irritation(self):
        s = _state(irritation=0.5)
        on_mentioned(s, "测试用户", 123456)
        self.assertLess(s["emotions"]["irritation"], 0.5,
                        "被@后 irritation 应下降")

    def test_on_replied_to_bot_reduces_irritation(self):
        s = _state(irritation=0.5)
        on_replied_to_bot(s, "测试用户")
        self.assertLess(s["emotions"]["irritation"], 0.5,
                        "被回复后 irritation 应下降")

    def test_on_praised_reduces_irritation_more(self):
        s = _state(irritation=0.5)
        before = s["emotions"]["irritation"]
        on_praised(s, "测试用户")
        after = s["emotions"]["irritation"]
        self.assertLess(after, before)
        s2 = _state(irritation=0.5)
        on_mentioned(s2, "测试用户", 123456)
        self.assertLess(after, s2["emotions"]["irritation"],
                        "被夸减 irritation 应比被@减得多")


if __name__ == "__main__":
    unittest.main()
