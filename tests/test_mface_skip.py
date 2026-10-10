"""mface 商城表情不收藏回归测试（2026-10 gap-19）。

mface 的 URL 会过期，且选择逻辑（pick_emotion_face/pick_context_sticker）
始终排除 mface，收进库基本永不复用、白占 MAX_STICKERS=300 名额。
锁定：maybe_collect_sticker 对 mface 直接跳过（对齐 maybe_collect_with_ai
「商城表情不收藏」的立场）；face/image 仍正常入库作对照。
测试把 _load_stickers/_save_stickers 指到内存 dict，不碰真实 data/。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import sticker


def _seed_entry(sid: int) -> dict:
    return {
        "id": sid,
        "sticker_type": "face",
        "cq_code": f"[CQ:face,id={sid}]",
        "tags": [],
        "unique_key": f"face:{sid}",
        "use_count": 0,
        "favorite_count": 1,
    }


class TestMfaceSkip(unittest.TestCase):

    def setUp(self):
        # 预置一条存量数据，便于断言「库大小不变」
        self.store = {"1": _seed_entry(1)}
        self._old_load = sticker._load_stickers
        self._old_save = sticker._save_stickers
        sticker._load_stickers = lambda: self.store
        sticker._save_stickers = lambda data: None

    def tearDown(self):
        sticker._load_stickers = self._old_load
        sticker._save_stickers = self._old_save

    def test_mface_message_skipped(self):
        res = sticker.maybe_collect_sticker(
            "[CQ:mface,summary=测试,url=https://x.example/mface]", 1, 123)
        self.assertIsNone(res)
        self.assertEqual(len(self.store), 1)  # 库大小不变

    def test_mface_without_url_skipped(self):
        res = sticker.maybe_collect_sticker("[CQ:mface,summary=测试]", 1, 123)
        self.assertIsNone(res)
        self.assertEqual(len(self.store), 1)

    def test_face_still_collected(self):
        res = sticker.maybe_collect_sticker("[CQ:face,id=14]", 1, 123)
        self.assertIsNotNone(res)
        self.assertEqual(len(self.store), 2)
        self.assertEqual(self.store[str(res)]["sticker_type"], "face")

    def test_image_still_collected(self):
        res = sticker.maybe_collect_sticker(
            "[CQ:image,file=abc.gif,url=https://x.example/a.gif]", 1, 123)
        self.assertIsNotNone(res)
        self.assertEqual(len(self.store), 2)
        self.assertEqual(self.store[str(res)]["sticker_type"], "image")


if __name__ == "__main__":
    unittest.main()
