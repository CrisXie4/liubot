"""长回复降级路径的引用码保活测试 — 副本剥码 + 轻引用补发。

长回复走 转发/文字图 降级时 [CQ:reply] 会被破坏：现在在副本上剥码
（原 bubbles 不动，落回逐条气泡时引用还在），降级成功后补发一条轻引用。
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import humanizer

LONG_TEXT = ("这是一段很长的回复内容。" * 46)  # ~552 可见字 > 500 阈值
REPLY_TEXT = "[CQ:reply,id=9]" + LONG_TEXT


def _run(coro):
    return asyncio.run(coro)


def _make_bot():
    bot = MagicMock()
    bot.bot_qq = "10000"
    bot.send_group_msg = AsyncMock(return_value=999)
    bot.send_group_forward_msg = AsyncMock(return_value=888)
    bot.delete_msg = AsyncMock(return_value=True)
    return bot


class TestHumanizerReplyFallback(unittest.TestCase):

    def setUp(self):
        humanizer._last_send_time.clear()
        humanizer.last_send_failure = None

    def _send(self, bot):
        # 打平随机性（typo/犹豫）与真实延迟，专注降级链路本身
        with patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
             patch("bot.humanizer.random.random", return_value=0.99):
            return _run(humanizer.send_human_like(bot, 9527, REPLY_TEXT))

    def _reply_code_msgs(self, bot):
        return [c for c in bot.send_group_msg.await_args_list
                if "[CQ:reply,id=9]" in str(c.args[1])]

    def test_a_forward_success_nodes_clean_and_followup(self):
        """(a) 转发成功：nodes 无引用码 + 恰补发 1 条含码轻引用"""
        bot = _make_bot()
        sent = self._send(bot)
        self.assertTrue(sent)

        # 转发被调用，nodes 的 content 里无 [CQ:reply
        bot.send_group_forward_msg.assert_awaited_once()
        nodes = bot.send_group_forward_msg.await_args.args[1]
        self.assertTrue(nodes)
        for node in nodes:
            for seg in node["data"]["content"]:
                self.assertNotIn("[CQ:reply", str(seg.get("data", {})),
                                 "转发节点里不应残留引用码")

        # 补发：send_group_msg 恰被以含码消息调用 1 次（轻引用）
        self.assertEqual(len(self._reply_code_msgs(bot)), 1)
        self.assertIn("长文在上", str(self._reply_code_msgs(bot)[0].args[1]))

    def test_b_both_degradations_fail_falls_back_intact(self):
        """(b) 转发+文字图都失败 → 落回逐条气泡：含码消息恰 1 条（首气泡自带码，无补发）"""
        bot = _make_bot()
        bot.send_group_forward_msg = AsyncMock(return_value=None)  # 转发失败
        with patch("bot.text_image.render_text_image", return_value=None):  # 文字图失败
            sent = self._send(bot)
        self.assertTrue(sent)

        # 没有发图片/转发卡片
        for c in bot.send_group_msg.await_args_list:
            self.assertNotIn("[CQ:image", str(c.args[1]))

        # 恰 1 条含码消息（首气泡），且不是"补发"的轻引用（无 👆 字样）
        code_msgs = self._reply_code_msgs(bot)
        self.assertEqual(len(code_msgs), 1)
        self.assertNotIn("长文在上", str(code_msgs[0].args[1]))

    def test_c_forward_fail_text_image_success(self):
        """(c) 转发失败+文字图成功：图片消息不含码、含码 send_group_msg 恰 1 次"""
        bot = _make_bot()
        bot.send_group_forward_msg = AsyncMock(return_value=None)  # 转发失败
        with patch("bot.text_image.render_text_image",
                   return_value="D:/fake/long_text.png"):
            sent = self._send(bot)
        self.assertTrue(sent)

        calls = bot.send_group_msg.await_args_list
        self.assertEqual(len(calls), 2)  # 图片 + 轻引用
        img_msg = str(calls[0].args[1])
        self.assertIn("[CQ:image", img_msg)
        self.assertNotIn("[CQ:reply", img_msg)
        followup_msg = str(calls[1].args[1])
        self.assertIn("[CQ:reply,id=9]", followup_msg)
        self.assertIn("长文在上", followup_msg)

    def test_bubbles_not_polluted_on_fallback(self):
        """锁『副本剥码不污染原 bubbles』：返回的气泡首条仍带引用码"""
        bot = _make_bot()
        bot.send_group_forward_msg = AsyncMock(return_value=None)
        with patch("bot.text_image.render_text_image", return_value=None):
            sent = self._send(bot)
        self.assertTrue(sent)
        self.assertTrue(sent[0].startswith("[CQ:reply,id=9]"))

    def test_short_reply_no_followup(self):
        """短回复不走降级路径 → 没有轻引用补发，首气泡带码照旧"""
        bot = _make_bot()
        with patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
             patch("bot.humanizer.random.random", return_value=0.99):
            sent = _run(humanizer.send_human_like(bot, 9528, "[CQ:reply,id=9]收到喵"))
        self.assertEqual(len(sent), 1)
        bot.send_group_forward_msg.assert_not_called()
        self.assertEqual(len(self._reply_code_msgs(bot)), 1)


if __name__ == "__main__":
    unittest.main()
