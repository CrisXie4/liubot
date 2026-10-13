"""新增 AI 工具 dispatch 测试（move/rename/私聊历史）— python tests/test_nc_file_tools.py"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools


def _run(coro):
    return asyncio.run(coro)


class TestNewToolDispatch(unittest.TestCase):

    def _bot(self):
        bot = MagicMock()
        bot.move_group_file = AsyncMock(return_value=True)
        bot.rename_group_file = AsyncMock(return_value=True)
        bot.get_friend_msg_history = AsyncMock(return_value={
            "messages": [
                {"time": 1700000000, "sender": {"nickname": "六哥"},
                 "message": "上次说的那个东西呢"},
            ]})
        return bot

    def test_move_group_file(self):
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 123, 42, "move_group_file",
            {"file_id": "F1", "current_folder_id": "", "target_folder_id": "/docs"}))
        self.assertIn("已移动", out)
        bot.move_group_file.assert_awaited_once_with(123, "F1", "", "/docs")

    def test_rename_group_file(self):
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 123, 42, "rename_group_file",
            {"file_id": "F1", "current_folder_id": "", "new_name": "总结.txt"}))
        self.assertIn("总结.txt", out)
        bot.rename_group_file.assert_awaited_once_with(123, "F1", "", "总结.txt")

    def test_rename_requires_name(self):
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 123, 42, "rename_group_file", {"file_id": "F1", "new_name": "  "}))
        self.assertIn("缺少", out)
        bot.rename_group_file.assert_not_awaited()

    def test_private_history_in_private_chat(self):
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 0, 42, "get_private_chat_history", {}))
        self.assertIn("六哥", out)
        self.assertIn("上次说的那个东西呢", out)
        bot.get_friend_msg_history.assert_awaited_once()

    def test_private_history_rejected_in_group(self):
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 123, 42, "get_private_chat_history", {}))
        self.assertIn("私聊", out)
        bot.get_friend_msg_history.assert_not_awaited()

    def test_tools_registered_and_gated(self):
        names = {t["name"] for t in ai_tools.ALL_TOOLS}
        self.assertIn("move_group_file", names)
        self.assertIn("rename_group_file", names)
        self.assertIn("get_private_chat_history", names)
        # 管理员工具在非管理员时被过滤
        admin_view = {t["name"] for t in ai_tools.get_tools_for_group(True)}
        plain_view = {t["name"] for t in ai_tools.get_tools_for_group(False)}
        self.assertIn("move_group_file", admin_view)
        self.assertNotIn("move_group_file", plain_view)
        self.assertIn("get_private_chat_history", plain_view)


if __name__ == "__main__":
    unittest.main()
