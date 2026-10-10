"""被引用的合并转发展开测试 — 引用一条转发时 AI 能读到内容。

此前 quote_handler 直接把 [CQ:forward,id=..] 当 text 喂给 AI；
现在经 content_reader 展开成 forward_text（截 500 字）。
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader, quote_handler


def _run(coro):
    return asyncio.run(coro)


class TestQuotedForwardExpand(unittest.TestCase):

    def setUp(self):
        content_reader._forward_cache.clear()
        self.bot = MagicMock()
        self.bot.bot_qq = "10000"

    def test_forward_quoted_expanded(self):
        """被引 raw 含 [CQ:forward → quoted 带 forward_text"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:forward,id=fwd-1]",
            "sender": {"user_id": 123, "card": "小王"},
        })

        async def fake_extract(bot_, raw, segments):
            return {"text": "", "images": [], "stickers": [], "has_forward": True,
                    "forward": {"text": "内层第一句\n内层第二句", "images": [],
                                "stickers": [], "count": 2, "nested": False},
                    "errors": []}

        with patch("bot.content_reader.extract_full_content", new=fake_extract):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 555))
        self.assertEqual(quoted["forward_text"], "内层第一句\n内层第二句")
        # text 键保持原 raw 不动
        self.assertEqual(quoted["text"], "[CQ:forward,id=fwd-1]")

        out = quote_handler.format_quoted_context_for_ai(quoted)
        self.assertIn("[转发记录]", out)
        self.assertIn("内层第一句", out)

    def test_forward_text_truncated_500(self):
        """forward_text 独立截 500 字"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:forward,id=fwd-2]",
            "sender": {"user_id": 123},
        })

        async def fake_extract(bot_, raw, segments):
            return {"has_forward": True,
                    "forward": {"text": "长" * 2000, "count": 1}, "errors": []}

        with patch("bot.content_reader.extract_full_content", new=fake_extract):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 556))
        self.assertEqual(len(quoted["forward_text"]), 500)

    def test_extract_failure_no_key(self):
        """展开异常 → 无 forward_text 键，不炸"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:forward,id=fwd-3]",
            "sender": {"user_id": 123},
        })

        async def boom(*a, **kw):
            raise RuntimeError("展开失败")

        with patch("bot.content_reader.extract_full_content", new=boom):
            quoted = _run(quote_handler.get_quoted_context(self.bot, 557))
        self.assertIsNotNone(quoted)
        self.assertNotIn("forward_text", quoted)

    def test_non_forward_quote_unaffected(self):
        """无 forward 的引用不受影响，无 forward_text 键"""
        self.bot.get_msg = AsyncMock(return_value={
            "raw_message": "普通一句话",
            "sender": {"user_id": 456, "nickname": "老李"},
        })
        quoted = _run(quote_handler.get_quoted_context(self.bot, 558))
        self.assertNotIn("forward_text", quoted)
        self.assertEqual(quoted["sender_qq"], 456)
        out = quote_handler.format_quoted_context_for_ai(quoted)
        self.assertNotIn("[转发记录]", out)
        self.assertIn("普通一句话", out)

    def test_format_forward_line_independent_of_text_trunc(self):
        """> [转发记录] 在 text[:300] 之外追加"""
        quoted = {
            "sender_qq": 1, "sender_name": "a", "message_id": 2,
            "text": "x" * 400, "has_image": False, "has_video": False,
            "has_face": False, "raw_message": "x" * 400,
            "forward_text": "F" * 400,
        }
        out = quote_handler.format_quoted_context_for_ai(quoted)
        # 转发记录行有完整 400 字（独立于首行 300 截断）
        self.assertIn("F" * 400, out)
        self.assertIn("x" * 300, out)
        self.assertNotIn("x" * 400, out)


if __name__ == "__main__":
    unittest.main()
