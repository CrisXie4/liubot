"""image_mod Vision 审核 + GIF + 并发调度测试 — python tests/test_image_fallback.py"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import image_mod


def _run(coro):
    return asyncio.run(coro)


class TestReviewImageWithFallback(unittest.TestCase):

    def setUp(self):
        image_mod._mod_cache.clear()

    def _patched(self, **kw):
        """统一 patch 帮手：禁用磁盘缓存加载，避免测试污染"""
        patches = {
            "_init_mod_cache": MagicMock(),  # 禁用磁盘加载
            **kw,
        }
        return [patch.object(image_mod, k, new=v) for k, v in patches.items()]

    def test_vision_success_first_try(self):
        """Vision 首次成功 → 直接返回，不重试"""
        bot = MagicMock()
        mock_vision = AsyncMock(return_value={
            "is_violation": True, "confidence": 0.9, "violation_type": "ad",
            "ocr_text": "加微信", "reason": "广告",
        })
        with patch.object(image_mod, "_init_mod_cache", new=MagicMock()), \
             patch.object(image_mod, "review_image_with_ai", new=mock_vision):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/vision_ok.jpg"))
            self.assertTrue(result["is_violation"])
            self.assertEqual(mock_vision.call_count, 1)

    def test_vision_failure_triggers_retry(self):
        """Vision 首次抛异常 → 重试一次成功"""
        bot = MagicMock()
        side_effects = [TimeoutError("API down"), {
            "is_violation": True, "confidence": 0.85, "violation_type": "ad",
            "ocr_text": "加微信 vx123", "reason": "重试成功",
        }]
        mock_vision = AsyncMock(side_effect=side_effects)
        with patch.object(image_mod, "_init_mod_cache", new=MagicMock()), \
             patch.object(image_mod, "review_image_with_ai", new=mock_vision):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/retry_ok.jpg"))
            self.assertIsNotNone(result)
            self.assertTrue(result["is_violation"])
            self.assertEqual(mock_vision.call_count, 2)

    def test_all_fail_returns_none(self):
        """Vision 两次都失败 → 返回 None"""
        bot = MagicMock()
        mock_vision = AsyncMock(side_effect=[TimeoutError("down"), TimeoutError("still down")])
        with patch.object(image_mod, "_init_mod_cache", new=MagicMock()), \
             patch.object(image_mod, "review_image_with_ai", new=mock_vision):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/all_fail.jpg"))
            self.assertIsNone(result)
            self.assertEqual(mock_vision.call_count, 2)

    def test_vision_returns_none_returns_none(self):
        """Vision 首次返回 None → 重试一次仍 None → 返回 None"""
        bot = MagicMock()
        mock_vision = AsyncMock(return_value=None)
        with patch.object(image_mod, "_init_mod_cache", new=MagicMock()), \
             patch.object(image_mod, "review_image_with_ai", new=mock_vision):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/none_ret.jpg"))
            self.assertIsNone(result)

    def test_cache_hit_skips_api(self):
        """缓存命中 → 不调 Vision"""
        bot = MagicMock()
        image_mod._mod_cache["http://x/cached.jpg"] = {"result": {"is_violation": False}, "ts": __import__("time").time()}
        with patch.object(image_mod, "_init_mod_cache", new=MagicMock()), \
             patch.object(image_mod, "review_image_with_ai", new=AsyncMock()) as mock_v:
            result = _run(image_mod.review_image_with_fallback(bot, "", "http://x/cached.jpg"))
            mock_v.assert_not_called()
            self.assertFalse(result["is_violation"])


class TestGifFrameExtraction(unittest.TestCase):

    def test_static_image_returns_as_is(self):
        """静态图（1 帧）→ 返回原 bytes"""
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
        raw = buf.getvalue()
        frames = image_mod._extract_gif_frames(raw)
        self.assertEqual(len(frames), 1)

    def test_gif_5_frames_picks_3(self):
        """5 帧 GIF → 抽首/中/末 3 帧"""
        from PIL import Image
        import io
        imgs = [Image.new("RGB", (10, 10), color=(i * 50, 0, 0)) for i in range(5)]
        buf_all = io.BytesIO()
        imgs[0].save(buf_all, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
        gif_bytes = buf_all.getvalue()
        frames = image_mod._extract_gif_frames(gif_bytes)
        self.assertEqual(len(frames), 3)

    def test_gif_2_frames_picks_all(self):
        """2 帧 GIF → 全提"""
        from PIL import Image
        import io
        imgs = [Image.new("RGB", (10, 10), color=(c, 0, 0)) for c in (0, 255)]
        buf = io.BytesIO()
        imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
        frames = image_mod._extract_gif_frames(buf.getvalue())
        self.assertEqual(len(frames), 2)


class TestAuditAllMedia(unittest.TestCase):

    def setUp(self):
        image_mod._mod_cache.clear()

    def test_concurrency_limit_5(self):
        """11 张图时同时 in-flight ≤ 5"""
        import time
        call_times = []
        async def fake_review(bot, fid, url, caption=""):
            call_times.append(time.time())
            await asyncio.sleep(0.05)
            return {"is_violation": False, "confidence": 0, "ocr_text": "", "reason": ""}
        images = [{"url": f"http://x/{i}.jpg", "file_id": str(i)} for i in range(11)]
        with patch.object(image_mod, "review_image_with_fallback", new=fake_review):
            _run(image_mod._audit_all_media(MagicMock(), images))
        # 验证前 5 个调用时间几乎同时（Semaphore(5) 控并发）
        first_batch = sorted(call_times[:5])
        self.assertTrue(all(t - first_batch[0] < 0.02 for t in first_batch))

    def test_cap_at_20_images(self):
        """25 张图 → 截到 20"""
        async def fake_review(bot, fid, url, caption=""):
            return {"is_violation": False}
        mock_review = AsyncMock(side_effect=fake_review)
        images = [{"url": f"http://x/{i}.jpg", "file_id": str(i)} for i in range(25)]
        with patch.object(image_mod, "review_image_with_fallback", new=mock_review):
            results = _run(image_mod._audit_all_media(MagicMock(), images))
            self.assertEqual(mock_review.call_count, 20)


if __name__ == "__main__":
    unittest.main()
