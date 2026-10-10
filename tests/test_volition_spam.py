"""Volition 主动发言防刷屏硬闸测试（2026-09-06 复读机事件回归）。

日志实录：45 分钟内 4 条主动发言里 3 条是「群里静悄悄…摸鱼～」的复读，
连续 3 次被忽视后 LLM 只安静了 8 分钟又开始发——代码层必须硬拦。
"""
import sys
import os
import time
import asyncio
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_brain


def _run(coro):
    return asyncio.run(coro)


class TestNearDuplicateSpeak(unittest.TestCase):
    """近重复拦截：日志里的真实复读序列必须全部拦住"""

    def setUp(self):
        ai_brain._recent_speaks.clear()

    def tearDown(self):
        ai_brain._recent_speaks.clear()

    def test_real_spam_sequence_all_blocked(self):
        """14:09 → 14:21(同句) → 14:40(变体) → 14:53(同变体) 的真实序列"""
        A = "周末午后三点半，群里静悄悄，正好摸鱼～"
        B = "群里静悄悄，正合适摸鱼～"
        self.assertFalse(ai_brain._is_near_duplicate_speak(A))  # 首次放行
        ai_brain._remember_speak(A)
        self.assertTrue(ai_brain._is_near_duplicate_speak(A))   # 同句复读
        self.assertTrue(ai_brain._is_near_duplicate_speak(B))   # 换皮复读
        ai_brain._remember_speak(B)
        self.assertTrue(ai_brain._is_near_duplicate_speak(B))   # 再复读

    def test_different_topic_passes(self):
        ai_brain._remember_speak("群里静悄悄，正合适摸鱼～")
        self.assertFalse(ai_brain._is_near_duplicate_speak("刚看到个乐子，虽然没啥用"))

    def test_old_entries_expire(self):
        ai_brain._recent_speaks.append(
            {"ts": time.time() - ai_brain.SPEAK_DEDUPE_WINDOW - 10,
             "norm": ai_brain._normalize_speak("周末摸鱼")})
        self.assertFalse(ai_brain._is_near_duplicate_speak("周末摸鱼"))

    def test_short_text_not_deduped(self):
        self.assertFalse(ai_brain._is_near_duplicate_speak("哈哈"))


class TestIgnoredBackoff(unittest.TestCase):
    """硬闸 1：连续被忽视 → 退避期内不再开口（连 LLM 都不该调）"""

    def setUp(self):
        self.old_state = ai_brain.cs.get_state
        self.old_can = ai_brain.cs.can_speak_proactively
        self.old_settings = ai_brain.memory.load_model_settings
        ai_brain._recent_speaks.clear()

    def tearDown(self):
        ai_brain.cs.get_state = self.old_state
        ai_brain.cs.can_speak_proactively = self.old_can
        ai_brain.memory.load_model_settings = self.old_settings
        ai_brain._recent_speaks.clear()

    def test_backoff_blocks_before_llm(self):
        ai_brain.cs.get_state = lambda: {
            "sleeping": False,
            "diary": [],
            "today": {"consecutive_ignored": 3},
            "emotions": {"joy": 0.7, "energy": 0.6, "social": 0.5, "boredom": 0.4, "loneliness": 0.3, "sadness": 0.0},
        }
        ai_brain.cs.can_speak_proactively = lambda s: (True, "")
        ai_brain.memory.load_model_settings = lambda: {"api_key": "k"}
        ai_brain._last_speak_sent = time.time()  # 刚发过不久

        async def _boom(*a, **k):
            raise AssertionError("退避期内不应调用 LLM")
        old_create = ai_brain.llm_call.llm_create
        ai_brain.llm_call.llm_create = _boom
        try:
            result = _run(ai_brain.decide_and_speak(bot=None))
        finally:
            ai_brain.llm_call.llm_create = old_create
        self.assertIsNone(result)

    def test_backoff_expires_allows_speak(self):
        ai_brain.cs.get_state = lambda: {
            "sleeping": False,
            "diary": [],
            "today": {"consecutive_ignored": 3},
            "emotions": {"joy": 0.7, "energy": 0.6, "social": 0.5, "boredom": 0.4, "loneliness": 0.3, "sadness": 0.0},
        }
        ai_brain.cs.can_speak_proactively = lambda s: (True, "")
        ai_brain.memory.load_model_settings = lambda: {"api_key": "k"}
        ai_brain._last_speak_sent = (
            time.time() - ai_brain.IGNORED_BACKOFF_SECS - 10)  # 退避已过期

        class _R:
            def __init__(self):
                self.content = [type("T", (), {
                    "text": "SPEAK: 今天学的这个冷知识有点意思"})()]

        async def _fake(*a, **k):
            return _R()
        old_create = ai_brain.llm_call.llm_create
        ai_brain.llm_call.llm_create = _fake
        try:
            result = _run(ai_brain.decide_and_speak(bot=None))
        finally:
            ai_brain.llm_call.llm_create = old_create
        self.assertEqual(result, "今天学的这个冷知识有点意思")


if __name__ == "__main__":
    unittest.main()
