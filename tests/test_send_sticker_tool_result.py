"""send_sticker 工具直接发送契约测试。

2026-10-10 改版：工具不再返回 CQ 码让模型拼进回复（下一轮 LLM 空响应时
表情整个丢失），改为工具直接发消息、返回给模型的只是「已发出」的说明。
锁定：真发了消息、返回值不含 CQ 码也不含括号说明；使用说明固定在
ALL_TOOLS 的 description 里。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools


class TestSendStickerToolResult(unittest.TestCase):

    def setUp(self):
        ai_tools._STICKER_RATE.clear()

    def tearDown(self):
        ai_tools._STICKER_RATE.clear()

    def _call(self, emotion: str = "开心", group_id: int = 0):
        bot = mock.MagicMock()
        bot.send_group_msg = mock.AsyncMock(return_value=12345)
        bot.send_private_msg = mock.AsyncMock(return_value=12345)
        bot.last_fail_msg = mock.MagicMock(return_value="")
        r = asyncio.run(ai_tools.execute_tool(
            bot, group_id, 123, "send_sticker", {"emotion": emotion}))
        return r, bot

    def test_sent_directly_not_returned(self):
        """群聊：真调 send_group_msg 发出，返回值是「已发出」说明而非 CQ 码"""
        for emotion in ("开心", "无奈", ""):
            r, bot = self._call(emotion, group_id=555)
            self.assertIn("已直接发出", r, f"emotion={emotion!r} → {r!r}")
            self.assertNotIn("[CQ:", r, f"返回值不该再带 CQ 码: {r!r}")
            self.assertNotIn("（", r, f"返回值带了括号说明: {r!r}")
            sent = bot.send_group_msg.call_args[0]
            self.assertEqual(sent[0], 555)
            self.assertTrue(str(sent[1]).startswith("[CQ:face,id="))

    def test_private_chat_sends_private(self):
        """私聊（group_id=0）：走 send_private_msg"""
        r, bot = self._call("开心", group_id=0)
        self.assertIn("已直接发出", r)
        bot.send_private_msg.assert_called_once()
        self.assertTrue(str(bot.send_private_msg.call_args[0][1]).startswith("[CQ:face,id="))

    def test_description_carries_usage_note(self):
        """使用说明固定在 ALL_TOOLS 的 send_sticker description 里"""
        desc = next(t["description"] for t in ai_tools.ALL_TOOLS
                    if t["name"] == "send_sticker")
        self.assertIn("直接把一个表情发到群里", desc)
        self.assertIn("只需配一句话", desc)
        self.assertNotIn("放进回复文本", desc, "旧契约（返回码拼回复）描述应已移除")


if __name__ == "__main__":
    unittest.main()
