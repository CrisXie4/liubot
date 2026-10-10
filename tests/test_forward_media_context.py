"""转发包内图片进 AI 上下文测试 — 节流 + 容错。

转发内的图此前只进审核不进 AI 上下文；现在经 describe_image（自带
24h 缓存）转成 media_context，每条转发最多 2 张防打爆 vision 配额。
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.handler import _forward_images_to_media_ctx


def _run(coro):
    return asyncio.run(coro)


class TestForwardImagesToMediaCtx(unittest.TestCase):

    def test_two_images_two_entries(self):
        """2 张图 → 2 条 {"type":"image"} 条目"""
        async def fake_describe(url, context=""):
            return f"描述:{url}"

        images = [{"url": "http://x/1.jpg"}, {"url": "http://x/2.jpg"}]
        with patch("bot.media_reader.describe_image", new=fake_describe):
            out = _run(_forward_images_to_media_ctx(images))
        self.assertEqual(len(out), 2)
        for item in out:
            self.assertEqual(item["type"], "image")
            self.assertIn("描述:", item["description"])
            self.assertTrue(item["url"])
            self.assertEqual(item["raw_cq"], "")

    def test_more_than_two_only_first_two(self):
        """>2 张只取前 2（节流）"""
        async def fake_describe(url, context=""):
            return "一只橘猫"

        images = [{"url": f"http://x/{i}.jpg"} for i in range(5)]
        with patch("bot.media_reader.describe_image", new=fake_describe):
            out = _run(_forward_images_to_media_ctx(images))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["url"], "http://x/0.jpg")
        self.assertEqual(out[1]["url"], "http://x/1.jpg")

    def test_describe_none_skipped(self):
        """describe 返回 None → 跳过且不抛错、不计入"""
        async def fake_describe(url, context=""):
            return None

        images = [{"url": "http://x/a.jpg"}, {"url": "http://x/b.jpg"}]
        with patch("bot.media_reader.describe_image", new=fake_describe):
            out = _run(_forward_images_to_media_ctx(images))
        self.assertEqual(out, [])

    def test_describe_raises_skipped(self):
        """describe 抛异常 → 单张跳过，不影响其余"""
        async def fake_describe(url, context=""):
            if url.endswith("bad.jpg"):
                raise RuntimeError("vision 挂了")
            return "ok 描述"

        images = [{"url": "http://x/bad.jpg"}, {"url": "http://x/good.jpg"}]
        with patch("bot.media_reader.describe_image", new=fake_describe):
            out = _run(_forward_images_to_media_ctx(images))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "http://x/good.jpg")

    def test_empty_and_missing_url(self):
        async def fake_describe(url, context=""):
            return "不该被调到"

        with patch("bot.media_reader.describe_image", new=fake_describe):
            self.assertEqual(_run(_forward_images_to_media_ctx([])), [])
            self.assertEqual(_run(_forward_images_to_media_ctx(None)), [])
            self.assertEqual(
                _run(_forward_images_to_media_ctx([{"url": ""}, {"file_id": "x"}])), [])


if __name__ == "__main__":
    unittest.main()
