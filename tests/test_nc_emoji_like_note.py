"""表情回应感知测试（group_msg_emoji_like → consciousness state）。

本地源码核实：emoji_likes_list 字段只在 get_msg 响应里填充（GetMsg.ts），
消息事件本体不带；回应归属靠 get_msg 反查 sender 判断。命中时写
state["emoji_like_note"]（覆盖式，只留最新一条），读取侧在 chat.py。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler
from bot.config import config

_BOT_QQ = "10001"  # 运行时占位（非真实凭据）


def _run(coro):
    return asyncio.run(coro)


def _make_bot(own_message=True, likes=None, get_msg_ok=True):
    bot = mock.MagicMock()
    bot.bot_qq = _BOT_QQ
    if get_msg_ok:
        sender_uid = int(_BOT_QQ) if own_message else 99999
        msg = {"sender": {"user_id": sender_uid}}
        if likes is not None:
            msg["emoji_likes_list"] = likes
        bot.get_msg = mock.AsyncMock(return_value=msg)
    else:
        bot.get_msg = mock.AsyncMock(side_effect=RuntimeError("消息不存在"))
    return bot


class TestEmojiLikeNote(unittest.TestCase):

    def setUp(self):
        self._state = {}
        patcher = mock.patch("bot.consciousness.get_state", return_value=self._state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_own_message_likes_written(self):
        """回应的是 Bot 自己的消息 → 写人话汇总（unicode emoji 映射 + 小黄脸占位）"""
        bot = _make_bot(own_message=True, likes=[
            {"emoji_id": "128514", "emoji_type": "1", "likes_cnt": "3"},
            {"emoji_id": "96", "emoji_type": "1", "likes_cnt": "1"},
        ])
        _run(handler._note_emoji_likes(bot, 123, 55, []))
        note = self._state.get("emoji_like_note")
        self.assertIsNotNone(note)
        self.assertIn("3 个😂", note["text"])
        self.assertIn("1 个[表情96]", note["text"])
        self.assertIsInstance(note["ts"], int)

    def test_other_message_ignored(self):
        """回应的不是 Bot 的消息 → 不写状态"""
        bot = _make_bot(own_message=False, likes=[
            {"emoji_id": "128514", "emoji_type": "1", "likes_cnt": "3"},
        ])
        _run(handler._note_emoji_likes(bot, 123, 55, []))
        self.assertNotIn("emoji_like_note", self._state)

    def test_get_msg_without_likes_falls_back_to_event_delta(self):
        """get_msg 响应不带 emoji_likes_list（旧版 NapCat）→ 退回事件增量列表"""
        bot = _make_bot(own_message=True, likes=None)
        _run(handler._note_emoji_likes(bot, 123, 55, [
            {"emoji_id": "128077", "count": 2},
        ]))
        note = self._state.get("emoji_like_note")
        self.assertIsNotNone(note)
        self.assertIn("2 个👍", note["text"])

    def test_overwrite_keeps_latest_only(self):
        """覆盖式：新回应覆盖旧记录"""
        bot = _make_bot(own_message=True, likes=[
            {"emoji_id": "128514", "emoji_type": "1", "likes_cnt": "1"},
        ])
        _run(handler._note_emoji_likes(bot, 123, 55, []))
        first = self._state["emoji_like_note"]["text"]
        bot2 = _make_bot(own_message=True, likes=[
            {"emoji_id": "128077", "emoji_type": "1", "likes_cnt": "7"},
        ])
        _run(handler._note_emoji_likes(bot2, 123, 66, []))
        self.assertEqual(self._state["emoji_like_note"]["text"].count("\n"), first.count("\n"))
        self.assertIn("7 个👍", self._state["emoji_like_note"]["text"])
        self.assertNotIn("😂", self._state["emoji_like_note"]["text"])

    def test_get_msg_failure_silent(self):
        bot = _make_bot(get_msg_ok=False)
        _run(handler._note_emoji_likes(bot, 123, 55, []))
        self.assertNotIn("emoji_like_note", self._state)

    def test_empty_likes_no_write(self):
        bot = _make_bot(own_message=True, likes=[])
        _run(handler._note_emoji_likes(bot, 123, 55, []))
        self.assertNotIn("emoji_like_note", self._state)

    def test_zero_message_id_noop(self):
        bot = _make_bot()
        _run(handler._note_emoji_likes(bot, 123, 0, [{"emoji_id": "128077", "count": 1}]))
        bot.get_msg.assert_not_awaited()
        self.assertNotIn("emoji_like_note", self._state)


class TestNoticeDispatch(unittest.TestCase):

    def setUp(self):
        self._state = {}
        patcher = mock.patch("bot.consciousness.get_state", return_value=self._state)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 戳一戳分支读 config.BOT_QQ，且回戳有 60s 冷却（全局状态需清理）
        self._old_bot_qq = config.BOT_QQ
        config.BOT_QQ = _BOT_QQ
        handler._poke_cooldown.clear()
        self.addCleanup(lambda: setattr(config, "BOT_QQ", self._old_bot_qq))
        self.addCleanup(handler._poke_cooldown.clear)

    def test_handle_notice_routes_emoji_like(self):
        """notice 事件进入 handle_notice 后走感知分支"""
        bot = _make_bot(own_message=True, likes=[
            {"emoji_id": "128514", "emoji_type": "1", "likes_cnt": "2"},
        ])
        event = {
            "post_type": "notice",
            "notice_type": "group_msg_emoji_like",
            "group_id": 123,
            "user_id": 99999,
            "message_id": 55,
            "likes": [{"emoji_id": "128514", "count": 1}],
            "is_add": True,
        }
        _run(handler.handle_notice(bot, event))
        note = self._state.get("emoji_like_note")
        self.assertIsNotNone(note)
        self.assertIn("😂", note["text"])
        bot.get_msg.assert_awaited_once_with(55)

    def test_handle_notice_poke_still_works(self):
        """原有戳一戳分支不受新分支影响"""
        bot = mock.MagicMock()
        bot.bot_qq = _BOT_QQ
        bot.send_group_poke = mock.AsyncMock(return_value=True)
        event = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 123, "user_id": 99999, "target_id": int(_BOT_QQ),
        }
        _run(handler.handle_notice(bot, event))
        self.assertEqual(bot.send_group_poke.await_count, 1)


if __name__ == "__main__":
    unittest.main()
