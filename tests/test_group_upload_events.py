"""群文件上传审核 + 新事件感知测试 — python tests/test_group_upload_events.py"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import group_events


def _run(coro):
    return asyncio.run(coro)


class TestGroupEventsNewLabels(unittest.TestCase):

    def setUp(self):
        group_events.clear_group(777)

    def tearDown(self):
        group_events.clear_group(777)

    def test_upload_label(self):
        group_events.record(777, "upload", 42, "张三", detail="破解工具.zip")
        block = group_events.build_recent_events_block(777)
        self.assertIn("上传了文件", block)
        self.assertIn("破解工具.zip", block)

    def test_essence_labels(self):
        group_events.record(777, "essence", 42, "李四")
        block = group_events.build_recent_events_block(777)
        self.assertIn("的消息被设为精华", block)
        group_events.record(777, "essence_del", 42, "李四")
        block = group_events.build_recent_events_block(777)
        self.assertIn("的消息被移出精华", block)


class TestGroupUploadAudit(unittest.TestCase):

    def setUp(self):
        group_events.clear_group(555)
        from bot import handler, cache
        self.handler = handler
        # _is_bot_admin 的 TTL 缓存跨测试污染（非管理员用例会把 False 留给后面）
        cache.bot_admin_cache.invalidate("555")

    def tearDown(self):
        group_events.clear_group(555)

    def _make_bot(self, admin=True):
        bot = MagicMock()
        bot.bot_qq = "10000"
        bot.delete_group_file = AsyncMock(return_value=True)
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.send_private_msg = AsyncMock(return_value=2)
        bot.get_group_member_info = AsyncMock(return_value={"role": "admin" if admin else "member"})
        return bot

    def test_clean_filename_only_records_event(self):
        bot = self._make_bot()
        with patch.object(self.handler.db, "add_violation") as mv, \
             patch.object(self.handler, "_is_owner", return_value=False):
            _run(self.handler._handle_group_upload(bot, 555, 42, {"id": "f1", "name": "笔记.pdf"}))
        mv.assert_not_called()
        bot.delete_group_file.assert_not_called()
        ev = group_events.build_recent_events_block(555)
        self.assertIn("上传了文件", ev)
        self.assertIn("笔记.pdf", ev)

    def test_violation_filename_deleted_and_warned(self):
        bot = self._make_bot()
        score = {"is_violation": True, "confidence": 0.9,
                 "details": {"keyword_hits": [{"keyword": "免费领取"}, {"keyword": "刷单"}]}}
        with patch.object(self.handler.learner, "score_with_learning", return_value=score), \
             patch.object(self.handler.db, "add_violation", return_value=1) as mv, \
             patch.object(self.handler.db, "query_violations", return_value=[]), \
             patch.object(self.handler.guardian, "has_pending_action", return_value=False), \
             patch.object(self.handler, "_is_owner", return_value=False), \
             patch.object(self.handler, "_is_official_account", return_value=False):
            _run(self.handler._handle_group_upload(
                bot, 555, 42, {"id": "f9", "name": "免费领取刷单教程.zip"}))
        mv.assert_called_once()
        bot.delete_group_file.assert_called_once_with(555, "f9")
        bot.send_group_msg.assert_called_once()
        self.assertIn("[CQ:at,qq=42]", bot.send_group_msg.call_args[0][1])
        self.assertIn("第1/3 次", bot.send_group_msg.call_args[0][1])

    def test_three_strikes_creates_kick_approval(self):
        bot = self._make_bot()
        score = {"is_violation": True, "confidence": 0.95,
                 "details": {"keyword_hits": [{"keyword": "刷单"}]}}
        with patch.object(self.handler.learner, "score_with_learning", return_value=score), \
             patch.object(self.handler.db, "add_violation", return_value=1), \
             patch.object(self.handler.db, "query_violations", return_value=[{}, {}]), \
             patch.object(self.handler.guardian, "has_pending_action", return_value=False), \
             patch.object(self.handler.guardian, "create_action_request", return_value="A1") as mg, \
             patch.object(self.handler, "_is_owner", return_value=False), \
             patch.object(self.handler, "_is_official_account", return_value=False):
            _run(self.handler._handle_group_upload(
                bot, 555, 42, {"id": "f9", "name": "刷单.zip"}))
        mg.assert_called_once()
        self.assertEqual(mg.call_args.kwargs.get("action"), "kick")
        bot.send_private_msg.assert_called_once()

    def test_owner_upload_not_audited(self):
        bot = self._make_bot()
        with patch.object(self.handler, "_is_owner", return_value=True):
            _run(self.handler._handle_group_upload(
                bot, 555, 99, {"id": "f2", "name": "免费领取.zip"}))
        bot.delete_group_file.assert_not_called()
        bot.send_group_msg.assert_not_called()

    def test_non_admin_no_actions(self):
        bot = self._make_bot(admin=False)
        score = {"is_violation": True, "confidence": 0.9,
                 "details": {"keyword_hits": [{"keyword": "刷单"}]}}
        with patch.object(self.handler.learner, "score_with_learning", return_value=score), \
             patch.object(self.handler.db, "add_violation", return_value=1), \
             patch.object(self.handler.db, "query_violations", return_value=[]), \
             patch.object(self.handler, "_is_owner", return_value=False), \
             patch.object(self.handler, "_is_official_account", return_value=False):
            _run(self.handler._handle_group_upload(bot, 555, 42, {"id": "f9", "name": "刷单.zip"}))
        bot.delete_group_file.assert_not_called()
        bot.send_group_msg.assert_not_called()


class TestEssenceEvent(unittest.TestCase):

    def setUp(self):
        group_events.clear_group(666)
        from bot import handler
        self.handler = handler
        self.bot = MagicMock()
        self.bot.bot_qq = "10000"

    def tearDown(self):
        group_events.clear_group(666)

    def test_bot_message_essence(self):
        with patch.object(self.handler.group_personality, "get_group_bot_name",
                          return_value="小六"):
            self.handler._handle_essence(
                self.bot, 666,
                {"sub_type": "add", "sender_id": 10000, "operator_id": 42,
                 "message_id": 9})
        block = group_events.build_recent_events_block(666)
        self.assertIn("小六", block)
        self.assertIn("的消息被设为精华", block)

    def test_other_message_essence_and_delete(self):
        fake_identity = MagicMock()
        fake_identity.peek_name.return_value = "王五"
        with patch.object(self.handler, "_get_identity", return_value=fake_identity):
            self.handler._handle_essence(
                self.bot, 666,
                {"sub_type": "add", "sender_id": 42, "operator_id": 1, "message_id": 9})
            self.handler._handle_essence(
                self.bot, 666,
                {"sub_type": "delete", "sender_id": 42, "operator_id": 1, "message_id": 9})
        block = group_events.build_recent_events_block(666, limit=2)
        self.assertIn("王五", block)
        self.assertIn("的消息被移出精华", block)


class TestProfileLike(unittest.TestCase):

    def setUp(self):
        from bot import handler
        self.handler = handler
        self.handler._profile_like_last.clear()
        self.handler._profile_like_daily.clear()

    def tearDown(self):
        self.handler._profile_like_last.clear()
        self.handler._profile_like_daily.clear()

    def _bot(self):
        bot = MagicMock()
        bot.bot_qq = "10000"
        bot.send_like = AsyncMock(return_value=True)
        return bot

    def test_like_back_once(self):
        bot = self._bot()
        ev = {"sub_type": "profile_like", "operator_id": 42, "operator_nick": "张三", "times": 2}

        async def _call():
            self.handler._handle_profile_like(bot, ev)
            self.handler._handle_profile_like(bot, dict(ev))  # 同一人立刻第二次
            await asyncio.sleep(0)  # 让 create_task 的回赞任务跑完
        _run(_call())
        bot.send_like.assert_called_once_with(42, 1)

    def test_daily_cap(self):
        bot = self._bot()
        today = time.strftime("%Y-%m-%d")
        self.handler._profile_like_daily[today] = 20
        self.handler._handle_profile_like(
            bot, {"sub_type": "profile_like", "operator_id": 43, "times": 1})
        bot.send_like.assert_not_called()

    def test_self_like_ignored(self):
        bot = self._bot()
        self.handler._handle_profile_like(
            bot, {"sub_type": "profile_like", "operator_id": 10000, "times": 1})
        bot.send_like.assert_not_called()


class TestGrayTip(unittest.TestCase):

    def setUp(self):
        group_events.clear_group(444)
        from bot import handler
        self.handler = handler

    def tearDown(self):
        group_events.clear_group(444)

    def test_json_content_parsed(self):
        fake_identity = MagicMock()
        fake_identity.peek_name.return_value = "赵六"
        with patch.object(self.handler, "_get_identity", return_value=fake_identity):
            self.handler._handle_gray_tip(
                444, 42, {"sub_type": "gray_tip",
                          "content": '[{"text":"戳了戳"},{"text":"小六"}]'})
        block = group_events.build_recent_events_block(444)
        self.assertIn("系统提示", block)
        self.assertIn("戳了戳小六", block)

    def test_plain_text_fallback(self):
        fake_identity = MagicMock()
        fake_identity.peek_name.return_value = "赵六"
        with patch.object(self.handler, "_get_identity", return_value=fake_identity):
            self.handler._handle_gray_tip(
                444, 42, {"sub_type": "gray_tip", "content": "领取了红包"})
        block = group_events.build_recent_events_block(444)
        self.assertIn("领取了红包", block)

    def test_empty_content_skipped(self):
        self.handler._handle_gray_tip(444, 42, {"sub_type": "gray_tip", "content": ""})
        self.assertEqual(group_events.build_recent_events_block(444), "")


if __name__ == "__main__":
    unittest.main()
