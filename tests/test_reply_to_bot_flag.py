"""真·回复Bot判定测试 — 回复任何人不该被当成回复 Bot。

- _is_reply_to_bot：被引用消息的发送者就是 Bot 自己才 True
- 影响力账本（reply_to_bot 记账）与主动决策（必回分支）都以它为守卫
"""
import inspect
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler
from bot.handler import _is_reply_to_bot


class TestIsReplyToBot(unittest.TestCase):

    def test_none_context(self):
        self.assertFalse(_is_reply_to_bot(None, 10000))
        self.assertFalse(_is_reply_to_bot({}, 10000))

    def test_other_sender(self):
        """sender_qq 是别人 → False（此前群友互回被误判）"""
        ctx = {"sender_qq": 12345, "sender_name": "路人"}
        self.assertFalse(_is_reply_to_bot(ctx, 10000))

    def test_bot_sender_int(self):
        ctx = {"sender_qq": 10000}
        self.assertTrue(_is_reply_to_bot(ctx, 10000))

    def test_bot_sender_str_forms(self):
        """int/str 两种形态都能对上"""
        self.assertTrue(_is_reply_to_bot({"sender_qq": "10000"}, "10000"))
        self.assertTrue(_is_reply_to_bot({"sender_qq": 10000}, "10000"))
        self.assertTrue(_is_reply_to_bot({"sender_qq": "10000"}, 10000))

    def test_missing_or_zero_sender(self):
        """sender_qq 缺失/0 → False（不能因为 bot_qq 也空就误判 True）"""
        self.assertFalse(_is_reply_to_bot({"sender_name": "x"}, 10000))
        self.assertFalse(_is_reply_to_bot({"sender_qq": 0}, 10000))
        self.assertFalse(_is_reply_to_bot({"sender_qq": 0}, ""))
        self.assertFalse(_is_reply_to_bot({"sender_qq": None}, 10000))

    def test_garbage_sender_qq(self):
        self.assertFalse(_is_reply_to_bot({"sender_qq": "abc"}, 10000))


class TestGuardWiredInHandler(unittest.TestCase):
    """handle_group_message 源中记账行/决策行已用 _is_reply_to_bot 守卫"""

    def setUp(self):
        self.src = inspect.getsource(handler.handle_group_message)

    def test_influence_accounting_guarded(self):
        """reply_to_bot 记账行被 _is_reply_to_bot 守卫，旧的 if is_reply 已移除"""
        self.assertIn(
            'if _is_reply_to_bot(quoted_context, bot.bot_qq):\n'
            '            influence.track_interaction(group_id, user_id, user_name, "reply_to_bot")',
            self.src,
        )

    def test_proactive_decision_guarded(self):
        """主动决策 is_reply_to_bot= 改用 _is_reply_to_bot"""
        self.assertIn("is_reply_to_bot=_is_reply_to_bot(quoted_context, bot.bot_qq)", self.src)

    def test_guard_used_twice(self):
        """影响力记账 + 主动决策两处守卫都在"""
        self.assertGreaterEqual(
            self.src.count("_is_reply_to_bot(quoted_context, bot.bot_qq)"), 2)


if __name__ == "__main__":
    unittest.main()
