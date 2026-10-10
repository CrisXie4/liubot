"""被引用图片的视觉描述测试 — 引用一张图不再是只有 📷 标记。

get_quoted_context 检测到图片时调 media_reader.get_quoted_media_description
（内部走 describe_image，自带 24h 缓存），结果截 200 字存 image_desc。
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import quote_handler


def _run(coro):
    return asyncio.run(coro)


class TestQuotedImageDescribe(unittest.TestCase):

    def setUp(self):
        self.bot = MagicMock()
        self.bot.bot_qq = "10000"

    def test_image_quoted_gets_desc(self):
        """被引消息含图 → quoted["image_desc"] 正确、格式化输出含描述"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "看这张[CQ:image,file=a.jpg,url=https://x/a.jpg]",
            "sender": {"user_id": 789, "card": "阿强"},
        })

        async def fake_desc(raw):
            return "一只橘猫"

        with patch("bot.media_reader.get_quoted_media_description", new=fake_desc):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 777))
        self.assertEqual(quoted["image_desc"], "一只橘猫")
        self.assertTrue(quoted["has_image"])

        out = quote_handler.format_quoted_context_for_ai(quoted)
        self.assertIn("一只橘猫", out)
        self.assertIn("图片内容: ", out)
        self.assertIn("📷图片", out)

    def test_image_desc_truncated_200(self):
        """描述截 200 字"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:image,file=b.jpg,url=https://x/b.jpg]",
            "sender": {"user_id": 1},
        })

        async def fake_desc(raw):
            return "图" * 500

        with patch("bot.media_reader.get_quoted_media_description", new=fake_desc):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 778))
        self.assertEqual(len(quoted["image_desc"]), 200)

    def test_describe_failure_no_key(self):
        """描述失败 → 无 image_desc 键，不炸"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:image,file=c.jpg,url=https://x/c.jpg]",
            "sender": {"user_id": 2},
        })

        async def boom(raw):
            raise RuntimeError("vision 挂了")

        with patch("bot.media_reader.get_quoted_media_description", new=boom):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 779))
        self.assertIsNotNone(quoted)
        self.assertNotIn("image_desc", quoted)

    def test_no_image_no_key(self):
        """无图引用不产生 image_desc 键、不触发描述调用"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "纯文本引用",
            "sender": {"user_id": 3},
        })
        called = []

        async def fake_desc(raw):
            called.append(raw)
            return "不该出现"

        with patch("bot.media_reader.get_quoted_media_description", new=fake_desc):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 780))
        self.assertNotIn("image_desc", quoted)
        self.assertEqual(called, [])
        out = quote_handler.format_quoted_context_for_ai(quoted)
        self.assertNotIn("图片内容: ", out)

    def test_empty_desc_no_key(self):
        """描述为空串 → 不产生空键"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:image,file=d.jpg,url=https://x/d.jpg]",
            "sender": {"user_id": 4},
        })

        async def fake_desc(raw):
            return ""

        with patch("bot.media_reader.get_quoted_media_description", new=fake_desc):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 781))
        self.assertNotIn("image_desc", quoted)


if __name__ == "__main__":
    unittest.main()
