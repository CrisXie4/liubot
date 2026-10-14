"""名片撤回 + 群名检查下线测试 — python tests/test_contact_card_recall.py"""
import asyncio
import inspect
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler


def _run(coro):
    return asyncio.run(coro)


class TestRecallContactCard(unittest.TestCase):

    def setUp(self):
        from bot import cache
        cache.bot_admin_cache.invalidate("321")

    def _bot(self, admin=True):
        bot = MagicMock()
        bot.bot_qq = "10000"
        bot.delete_msg = AsyncMock(return_value=True)
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.send_private_msg = AsyncMock(return_value=2)
        bot.get_group_member_info = AsyncMock(
            return_value={"role": "admin" if admin else "member"})
        return bot

    def test_group_card_recalled(self):
        bot = self._bot()
        with patch.object(handler.db, "query_violations", return_value=[]), \
             patch.object(handler.db, "add_violation", return_value=1) as mv:
            out = _run(handler._recall_contact_card(
                bot, 321, 42, "张三", 9001,
                "[CQ:contact,type=group,id=777] 来这个群"))
        self.assertTrue(out)
        bot.delete_msg.assert_awaited_once_with(9001)
        mv.assert_called_once()
        self.assertIn("名片", mv.call_args.kwargs["violation_note"])
        bot.send_group_msg.assert_called_once()
        self.assertIn("[CQ:at,qq=42]", bot.send_group_msg.call_args[0][1])
        self.assertIn("群名片", bot.send_group_msg.call_args[0][1])

    def test_qq_card_kind_detected(self):
        bot = self._bot()
        with patch.object(handler.db, "query_violations", return_value=[]), \
             patch.object(handler.db, "add_violation", return_value=1):
            _run(handler._recall_contact_card(
                bot, 321, 42, "李四", 9002,
                "[CQ:contact,type=qq,id=555] 加我"))
        self.assertIn("个人名片", bot.send_group_msg.call_args[0][1])

    def test_three_strikes_creates_kick_approval(self):
        bot = self._bot()
        with patch.object(handler.db, "query_violations", return_value=[{}, {}]), \
             patch.object(handler.db, "add_violation", return_value=1), \
             patch.object(handler.guardian, "has_pending_action", return_value=False), \
             patch.object(handler.guardian, "create_action_request",
                          return_value="A9") as mg:
            _run(handler._recall_contact_card(
                bot, 321, 42, "王五", 9003,
                "[CQ:contact,type=group,id=777]"))
        mg.assert_called_once()
        self.assertEqual(mg.call_args.kwargs.get("action"), "kick")
        bot.send_private_msg.assert_called_once()

    def test_non_admin_falls_through(self):
        bot = self._bot(admin=False)
        out = _run(handler._recall_contact_card(
            bot, 321, 42, "张三", 9001, "[CQ:contact,type=group,id=777]"))
        self.assertFalse(out)
        bot.delete_msg.assert_not_called()
        bot.send_group_msg.assert_not_called()

    def test_owner_exempt_in_message_flow(self):
        """主人发名片不走撤回（源码契约：入口守卫带 owner/official 豁免）"""
        src = inspect.getsource(handler.handle_group_message)
        self.assertIn("[CQ:contact,", src)
        self.assertIn("_is_owner(user_id)", src)
        self.assertIn("_is_official_account(user_id)", src)


class TestGroupNameCheckRemoved(unittest.TestCase):

    def test_check_group_name_disabled(self):
        """群名检查已下线——永远返回 None、不查库不评分"""
        bot = MagicMock()
        bot.get_group_info = AsyncMock(return_value={"group_name": "六哥API交流群"})
        out = _run(handler.check_group_name(bot, 1084012604))
        self.assertIsNone(out)
        bot.get_group_info.assert_not_called()  # 空壳：连 API 都不调

    def test_monitor_no_group_name_block(self):
        """定时监控里不再有群名检查调用"""
        src = inspect.getsource(handler.monitor_all_group_names)
        self.assertNotIn("check_group_name", src)
        self.assertIn("check_member_names", src)  # 昵称检查保留

    def test_owner_command_reports_disabled(self):
        """查群名 主人命令 → 提示功能已下线，不再产出违规警告"""
        src = inspect.getsource(handler.handle_group_message)
        self.assertIn("已下线", src)


if __name__ == "__main__":
    unittest.main()
