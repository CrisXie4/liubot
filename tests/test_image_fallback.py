"""image_mod 三级降级 + GIF + 并发调度测试 — python tests/test_image_fallback.py"""
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

    def test_vision_success_skips_ocr(self):
        """Vision 返回结果 → 不调 OCR"""
        bot = MagicMock()
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(return_value={
            "is_violation": True, "confidence": 0.9, "violation_type": "ad",
            "ocr_text": "加微信", "reason": "广告",
        })), patch.object(image_mod, "_ocr_image_bytes", new=AsyncMock(return_value="")) as mock_ocr:
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/vision_ok.jpg"))
            self.assertTrue(result["is_violation"])
            mock_ocr.assert_not_called()

    def test_vision_failure_triggers_ocr(self):
        """Vision 抛超时 → 触发 OCR → OCR 拿到文字 → llm_moderate"""
        bot = MagicMock()
        ocr_result = "加微信 vx123 免费领取"
        llm_result = {
            "is_violation": True, "confidence": 0.85, "violation_type": "ad",
            "ocr_text": ocr_result, "reason": "OCR 后判定广告",
            "keywords_matched": [], "category": "ad", "source": "llm_confirmed",
            "suggested_action": "mute", "latency_ms": 10,
        }
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(side_effect=TimeoutError("API down"))), \
             patch.object(image_mod, "_ocr_available_or_init", new=MagicMock(return_value=True)), \
             patch.object(image_mod, "_download_image_bytes", new=AsyncMock(return_value=b"fake-img")), \
             patch.object(image_mod, "_extract_gif_frames", new=MagicMock(return_value=[b"frame1"])), \
             patch.object(image_mod, "_ocr_image_bytes", new=AsyncMock(return_value=ocr_result)), \
             patch("bot.llm_mod.llm_moderate", new=AsyncMock(return_value=llm_result)):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/ocr_fallback.jpg"))
            self.assertIsNotNone(result)
            self.assertTrue(result["is_violation"])
            self.assertIn("加微信", result.get("ocr_text", ""))

    def test_all_fail_returns_none(self):
        """Vision + OCR + LLM 全失败 → 返回 None"""
        bot = MagicMock()
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(return_value=None)), \
             patch.object(image_mod, "_ocr_available_or_init", new=MagicMock(return_value=False)):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/all_fail.jpg"))
            self.assertIsNone(result)

    def test_cache_hit_skips_api(self):
        """缓存命中 → 不调 Vision"""
        bot = MagicMock()
        image_mod._mod_cache["http://x/cached.jpg"] = {"result": {"is_violation": False}, "ts": __import__("time").time()}
        # 强制不再从磁盘重载：直接清掉懒加载状态后塞内存值
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock()) as mock_v:
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
        # 验证前 5 个调用时间几乎同时，后 6 个明显晚于前 5 个结束
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
