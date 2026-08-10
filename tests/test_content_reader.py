"""content_reader 单元测试 — 用 python tests/test_content_reader.py 直接跑"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader


def _run(coro):
    """同步跑 async 测试"""
    return asyncio.run(coro)


class TestExtractForward(unittest.TestCase):

    def setUp(self):
        # 每次清缓存
        content_reader._forward_cache.clear()

    def test_forward_text_extraction(self):
        """合并转发能提取子消息文本"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [
                {"nickname": "Alice", "message": [{"type": "text", "data": {"text": "加微信 vx123"}}]},
                {"nickname": "Bob", "message": [{"type": "text", "data": {"text": "免费领取"}}]},
            ]
        })
        raw = "[CQ:forward,id=abc123]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertTrue(result["has_forward"])
        self.assertEqual(result["forward"]["count"], 2)
        self.assertIn("加微信 vx123", result["forward"]["text"])
        self.assertIn("免费领取", result["forward"]["text"])

    def test_forward_resid_expired(self):
        """resId 过期：返回 errors + 占位符，不抛异常"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value=None)
        raw = "[CQ:forward,id=expired_id]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertTrue(result["has_forward"])
        self.assertEqual(result["forward"]["text"], "[合并转发-无法解析]")
        self.assertTrue(len(result["errors"]) > 0)

    def test_nested_forward_depth_limit(self):
        """嵌套 forward 深度上限 3"""
        bot = MagicMock()
        # 构造 5 层嵌套：每层 forward 包含一个文本 + 下一层 forward
        call_count = [0]
        async def fake_get_forward(*args, **kwargs):
            call_count[0] += 1
            depth = call_count[0]
            if depth > 5:
                return None
            return {"messages": [{"nickname": "u", "message": [
                {"type": "text", "data": {"text": f"L{depth}"}},
                {"type": "forward", "data": {"id": f"nest{depth+1}"}},
            ]}]}
        bot.get_forward_msg = fake_get_forward

        raw = "[CQ:forward,id=nest1]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        # 深度上限 3 → 最多展开 3 层（根 + 2 层嵌套）
        self.assertTrue(result["has_forward"])
        self.assertTrue(result["forward"]["nested"])
        # 第 4 层不该被展开（errors 里有深度告警）
        self.assertTrue(any("深度" in e or "depth" in e.lower() for e in result["errors"]))

    def test_forward_submessage_count_limit(self):
        """子消息条数上限 50"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [{"nickname": "u", "message": [{"type": "text", "data": {"text": f"msg{i}"}}]} for i in range(60)]
        })
        raw = "[CQ:forward,id=bigpack]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertEqual(result["forward"]["count"], 50)
        self.assertIn("msg49", result["forward"]["text"])
        self.assertNotIn("msg59", result["forward"]["text"])


class TestExtractImagesAndStickers(unittest.TestCase):

    def test_image_extraction_from_segments(self):
        """从消息段提取图片 url/file_id"""
        bot = MagicMock()
        segments = [
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"url": "http://x/a.jpg", "file": "abc.gif"}},
        ]
        result = _run(content_reader.extract_full_content(bot, "看图[CQ:image,url=http://x/a.jpg]", segments))
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["url"], "http://x/a.jpg")
        self.assertEqual(result["images"][0]["file_id"], "abc.gif")
        self.assertEqual(result["text"], "看图")

    def test_mface_extraction_as_sticker(self):
        """mface 商城表情归入 stickers，含 url + summary"""
        bot = MagicMock()
        segments = [{"type": "mface", "data": {"url": "http://x/sticker.png", "summary": "[委屈]"}}]
        result = _run(content_reader.extract_full_content(bot, "[CQ:mface,url=http://x/sticker.png,summary=[委屈]]", segments))
        self.assertEqual(len(result["stickers"]), 1)
        self.assertEqual(result["stickers"][0]["url"], "http://x/sticker.png")
        self.assertEqual(result["stickers"][0]["summary"], "[委屈]")

    def test_face_small_emoji_skipped(self):
        """face 基础表情被跳过（不进 images/stickers）"""
        bot = MagicMock()
        segments = [{"type": "face", "data": {"id": 96}}]
        result = _run(content_reader.extract_full_content(bot, "[CQ:face,id=96]", segments))
        self.assertEqual(result["images"], [])
        self.assertEqual(result["stickers"], [])


class TestMixedMessage(unittest.TestCase):

    def test_text_plus_image_plus_forward(self):
        """文本 + 图片 + 转发混合"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [{"nickname": "u", "message": [{"type": "text", "data": {"text": "子消息"}}]}]
        })
        segments = [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "http://x/a.jpg", "file": "a.jpg"}},
        ]
        raw = "看这个[CQ:image,file=a.jpg,url=http://x/a.jpg][CQ:forward,id=mix1]"
        result = _run(content_reader.extract_full_content(bot, raw, segments))
        self.assertEqual(result["text"], "看这个")
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(result["has_forward"])
        self.assertIn("子消息", result["forward"]["text"])


if __name__ == "__main__":
    unittest.main()
