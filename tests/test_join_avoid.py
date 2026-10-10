"""入群避让测试 — python tests/test_join_avoid.py

回归背景（2026-09-25 反馈）：新人琉璃盏进群，verify 已单行 @ 出题，
AI 聊天又对"琉璃盏 下午这个点进群？"补了一句"欢迎欢迎"，两条叠一起刷屏。
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GID = 999001  # 独立群号，避免和其他测试的冷却状态串台


class TestJoinAvoid(unittest.TestCase):
    def setUp(self):
        from bot import decision_engine as de
        from bot import verify as verify_mod
        self.de = de
        self.verify = verify_mod
        de._last_reply_time.clear()
        verify_mod._recent_joins.clear()
        self.addCleanup(de._last_reply_time.clear)
        self.addCleanup(verify_mod._recent_joins.clear)

    def _decide(self, message):
        bot = MagicMock()
        bot.bot_qq = "10000"
        return asyncio_run(self.de.should_reply_to_message(
            bot=bot, group_id=GID, user_qq=123, user_name="丛雨丸",
            message=message, is_at_bot=False, is_reply_to_bot=False,
        ))

    def test_avoids_chatter_about_recently_quizzed_newbie(self):
        """新人刚被出题 → 提到其名字的消息不再触发自主回复。"""
        self.verify.note_recent_join(GID, 999, "琉璃盏")
        ok, reason = self._decide("琉璃盏 下午这个点进群？")
        self.assertFalse(ok)
        self.assertIn("入群避让", reason)

    def test_no_avoidance_without_recent_join(self):
        """没有最近入群 → 同样的消息照常走决策（不被避让拦下）。"""
        ok, reason = self._decide("琉璃盏 下午这个点进群？")
        self.assertNotIn("入群避让", reason)

    def test_avoidance_expires(self):
        """超过避让窗口 → 恢复正常决策。"""
        self.verify._recent_joins[GID] = {999: ("琉璃盏", time.time() - 301)}
        ok, reason = self._decide("琉璃盏 下午这个点进群？")
        self.assertNotIn("入群避让", reason)

    def test_at_bot_still_replies(self):
        """被直接 @ 时永远必回，避让不拦。"""
        bot = MagicMock()
        bot.bot_qq = "10000"
        self.verify.note_recent_join(GID, 999, "琉璃盏")
        ok, reason = asyncio_run(self.de.should_reply_to_message(
            bot=bot, group_id=GID, user_qq=123, user_name="丛雨丸",
            message="琉璃盏 是谁啊", is_at_bot=True, is_reply_to_bot=False,
        ))
        self.assertTrue(ok)

    def test_short_names_not_matched(self):
        """单字名不参与避让匹配（误伤率太高）。"""
        self.verify.note_recent_join(GID, 999, "A")
        ok, reason = self._decide("A 进群？")
        self.assertNotIn("入群避让", reason)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
