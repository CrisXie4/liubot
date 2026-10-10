"""send_voice / get_user_status / get_profile_like 工具测试。

本地源码核实的两个坑（本文件锁定）：
1. NapCat send_group_ai_record 的角色参数名是 character（非 character_id）；
2. get_ai_characters 需要 group_id 且返回按类别嵌套，工具层拍平。
send_voice 首次拉角色列表缓存 24h；默认角色读 QQBOT_AI_VOICE_CHARACTER。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools
from bot.onebot_client import OneBotClient


def _run(coro):
    return asyncio.run(coro)


def _make_bot(characters=None, send_ok=True):
    bot = mock.MagicMock()
    bot.get_ai_characters = mock.AsyncMock(return_value=characters if characters is not None else [
        {"type": "热门", "characters": [
            {"character_id": "c1", "character_name": "甜甜", "preview_url": ""},
            {"character_id": "c2", "character_name": "低沉", "preview_url": ""},
        ]},
        {"type": "方言", "characters": [
            {"character_id": "c3", "character_name": "东北", "preview_url": ""},
        ]},
    ])
    bot.send_group_ai_record = mock.AsyncMock(return_value=send_ok)
    bot.get_user_status = mock.AsyncMock(return_value={"status": 10, "ext_status": 0})
    bot.get_profile_like = mock.AsyncMock(return_value={
        "favoriteInfo": {"today_count": 5, "total_count": 100,
                         "userInfos": [{"uin": 10086, "nick": "小明"}]},
        "voteInfo": {"total_count": 233, "new_count": 2, "userInfos": []},
    })
    bot.last_fail_msg = mock.MagicMock(return_value="")
    return bot


def _call_tool(bot, name, tool_input, group_id=123):
    return _run(ai_tools.execute_tool(bot, group_id, 456, name, tool_input))


class TestSendVoice(unittest.TestCase):

    def setUp(self):
        ai_tools._VOICE_CHARACTER_CACHE.clear()
        self._old_env = os.environ.pop("QQBOT_AI_VOICE_CHARACTER", None)

    def tearDown(self):
        ai_tools._VOICE_CHARACTER_CACHE.clear()
        if self._old_env is not None:
            os.environ["QQBOT_AI_VOICE_CHARACTER"] = self._old_env
        else:
            os.environ.pop("QQBOT_AI_VOICE_CHARACTER", None)

    def test_first_call_fetches_and_flattens(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_voice", {"text": "晚安"})
        self.assertIn("✓", r)
        bot.get_ai_characters.assert_awaited_once_with(123)
        bot.send_group_ai_record.assert_awaited_once_with(123, "c1", "晚安")

    def test_cache_24h_avoids_refetch(self):
        bot = _make_bot()
        _call_tool(bot, "send_voice", {"text": "一"})
        _call_tool(bot, "send_voice", {"text": "二"})
        self.assertEqual(bot.get_ai_characters.await_count, 1)

    def test_expired_cache_refetches(self):
        bot = _make_bot()
        ai_tools._VOICE_CHARACTER_CACHE[123] = ([], 0.0)  # 1970 年 → 过期
        _call_tool(bot, "send_voice", {"text": "嗨"})
        self.assertEqual(bot.get_ai_characters.await_count, 1)

    def test_env_default_character_used(self):
        os.environ["QQBOT_AI_VOICE_CHARACTER"] = "c3"
        bot = _make_bot()
        r = _call_tool(bot, "send_voice", {"text": "嗨"})
        self.assertIn("✓", r)
        bot.send_group_ai_record.assert_awaited_once_with(123, "c3", "嗨")

    def test_groups_cache_independent(self):
        bot = _make_bot()
        _call_tool(bot, "send_voice", {"text": "a"}, group_id=123)
        _call_tool(bot, "send_voice", {"text": "b"}, group_id=456)
        self.assertEqual(bot.get_ai_characters.await_count, 2)

    def test_empty_text_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_voice", {"text": "  "})
        self.assertIn("不能为空", r)
        bot.send_group_ai_record.assert_not_awaited()

    def test_no_characters_reported(self):
        bot = _make_bot(characters=[])
        r = _call_tool(bot, "send_voice", {"text": "嗨"})
        self.assertTrue(r.startswith("发送失败"))
        bot.send_group_ai_record.assert_not_awaited()

    def test_send_failure_readable(self):
        bot = _make_bot(send_ok=False)
        r = _call_tool(bot, "send_voice", {"text": "嗨"})
        self.assertIn("语音发送失败", r)


class TestUserStatusTool(unittest.TestCase):

    def test_online_status_mapped(self):
        bot = _make_bot()
        r = _call_tool(bot, "get_user_status", {"user_id": 10086})
        self.assertIn("在线", r)
        self.assertIn("10086", r)

    def test_query_failure(self):
        bot = _make_bot()
        bot.get_user_status = mock.AsyncMock(return_value=None)
        r = _call_tool(bot, "get_user_status", {"user_id": 10086})
        self.assertIn("失败", r)

    def test_missing_user_id(self):
        bot = _make_bot()
        r = _call_tool(bot, "get_user_status", {})
        self.assertIn("user_id", r)
        bot.get_user_status.assert_not_awaited()


class TestProfileLikeTool(unittest.TestCase):

    def test_counts_and_likers(self):
        bot = _make_bot()
        r = _call_tool(bot, "get_profile_like", {})
        self.assertIn("5", r)
        self.assertIn("233", r)
        self.assertIn("10086", r)

    def test_query_failure(self):
        bot = _make_bot()
        bot.get_profile_like = mock.AsyncMock(return_value=None)
        r = _call_tool(bot, "get_profile_like", {})
        self.assertIn("失败", r)


class TestClientActionContract(unittest.TestCase):
    """client 包装 → NapCat action 名与参数形状（本地源码锁定）"""

    def test_get_ai_characters_requires_group(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok", "data": []})
        _run(client.get_ai_characters(123))
        client._call.assert_awaited_once_with(
            "get_ai_characters", {"group_id": "123", "chat_type": 1})

    def test_send_group_ai_record_uses_character_param(self):
        """NapCat 参数名是 character —— character_id 会被 schema 校验 400 拒掉"""
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        self.assertTrue(_run(client.send_group_ai_record(123, "c1", "嗨")))
        client._call.assert_awaited_once_with("send_group_ai_record", {
            "group_id": "123", "character": "c1", "text": "嗨"})

    def test_get_user_status_action(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok", "data": {"status": 10}})
        self.assertEqual(_run(client.get_user_status(10086)), {"status": 10})
        client._call.assert_awaited_once_with("nc_get_user_status", {"user_id": 10086})

    def test_get_profile_like_action(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok", "data": {}})
        _run(client.get_profile_like())
        client._call.assert_awaited_once_with("get_profile_like", {})


if __name__ == "__main__":
    unittest.main()
