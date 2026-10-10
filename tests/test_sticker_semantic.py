"""表情语义选择 + 中文标签归一化 + GIF 抽帧测试（2026-09-06 表情牛头不对马嘴回归）。"""
import sys
import os
import io
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import sticker, media_reader


class _TempStickerDB:
    """临时表情库文件"""

    def __init__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._old = sticker.STICKER_DB_PATH
        sticker.STICKER_DB_PATH = os.path.join(self._tmpdir, "stickers.json")

    def cleanup(self):
        sticker.STICKER_DB_PATH = self._old


class TestEmotionAlias(unittest.TestCase):

    def test_basic_and_suffix(self):
        self.assertEqual(sticker._match_emotion_key("尴尬"), "尴尬")
        self.assertEqual(sticker._match_emotion_key("尴尬表情"), "尴尬")
        self.assertEqual(sticker._match_emotion_key("饥饿"), "吃货")
        self.assertEqual(sticker._match_emotion_key("哈哈"), "大笑")
        self.assertEqual(sticker._match_emotion_key("狗头"), "调皮")

    def test_unknown_returns_empty(self):
        self.assertEqual(sticker._match_emotion_key("图片"), "")
        self.assertEqual(sticker._match_emotion_key(""), "")


class TestNormalizeFaceTags(unittest.TestCase):

    def test_bracket_name_becomes_cq(self):
        out = sticker.normalize_face_tags("下午好呀 [尴尬] 今天周末")
        self.assertIn("[CQ:face,id=", out)
        self.assertNotIn("[尴尬]", out)

    def test_suffix_and_colon_forms(self):
        self.assertIn("[CQ:face,id=",
                      sticker.normalize_face_tags("[饥饿表情]干饭去"))
        self.assertIn("[CQ:face,id=",
                      sticker.normalize_face_tags("笑死:狗头:"))

    def test_bare_placeholder_removed(self):
        out = sticker.normalize_face_tags("有啥事吩咐 [表情]")
        self.assertNotIn("[表情]", out)

    def test_unknown_bracket_untouched(self):
        self.assertEqual(sticker.normalize_face_tags("看这个[链接]"),
                         "看这个[链接]")

    def test_existing_cq_untouched(self):
        src = "哈哈 [CQ:face,id=14] 走起"
        self.assertEqual(sticker.normalize_face_tags(src), src)


class TestSemanticPick(unittest.TestCase):
    """pick_emotion_face：语义命中收藏库，不命中绝不随机"""

    def setUp(self):
        self.tmp = _TempStickerDB()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_library_falls_back_to_emotion_face(self):
        # 空库：直接内置映射，"尴尬"必出尴尬系 id，绝不出饥饿系
        for _ in range(10):
            cq = sticker.pick_emotion_face("尴尬", group_id=1)
            fid = int(cq.split("id=")[1].rstrip("]"))
            self.assertIn(fid, sticker.EMOTION_FACE_MAP["尴尬"])

    def test_library_semantic_match_wins(self):
        # 收藏库里有带"尴尬"标签的表情 → 情绪=尴尬 时应命中它而非内置
        sticker.add_sticker(
            sticker_type="face", cq_code="[CQ:face,id=25]",
            tags=["尴尬", "无语"], group_id=9, description="尴尬到脚趾抠地")
        cq = sticker.pick_emotion_face("尴尬", group_id=9)
        self.assertEqual(cq, "[CQ:face,id=25]")

    def test_library_mismatch_not_picked(self):
        # 收藏库只有"庆祝"表情 → 情绪=生气 时不能随机抽它，走内置映射
        sticker.add_sticker(
            sticker_type="face", cq_code="[CQ:face,id=74]",
            tags=["庆祝"], group_id=8, description="撒花庆祝")
        for _ in range(10):
            cq = sticker.pick_emotion_face("生气", group_id=8)
            fid = int(cq.split("id=")[1].rstrip("]"))
            self.assertIn(fid, sticker.EMOTION_FACE_MAP["生气"])

    def test_gif_sticker_semantic_match(self):
        # GIF 收藏：语义命中 → 直接返回它的 image CQ（本地缓存优先）
        sid = sticker.add_sticker(
            sticker_type="image", cq_code="[CQ:image,file=abc.gif,url=https://x.example/a.gif]",
            tags=["吃", "干饭", "吃货"], group_id=7, description="炫饭动图")
        cq = sticker.pick_emotion_face("饥饿", group_id=7)
        # 无本地缓存文件 → 回退原始 CQ 码
        self.assertEqual(cq, "[CQ:image,file=abc.gif,url=https://x.example/a.gif]")
        self.assertEqual(sticker.get_sticker(sid)["use_count"], 1)


class TestGifFrames(unittest.TestCase):
    """GIF 抽帧拼图"""

    @staticmethod
    def _make_gif(frames=4, size=(120, 90)):
        from PIL import Image
        imgs = []
        for i in range(frames):
            img = Image.new("RGB", size, (i * 60 % 255, 100, 150))
            imgs.append(img)
        buf = io.BytesIO()
        imgs[0].save(buf, format="GIF", save_all=True,
                     append_images=imgs[1:], duration=200, loop=0)
        return buf.getvalue()

    def test_is_gif_url(self):
        self.assertTrue(media_reader._is_gif_url("https://x.example/a.gif"))
        self.assertTrue(media_reader._is_gif_url(
            "https://x.example/a?url=q.gif", "image/gif"))
        self.assertFalse(media_reader._is_gif_url("https://x.example/a.jpg"))

    def test_gif_to_frames_png(self):
        png = media_reader._gif_to_frames_image(self._make_gif(4), max_frames=3)
        self.assertIsNotNone(png)
        from PIL import Image
        im = Image.open(io.BytesIO(png))
        self.assertEqual(im.format, "PNG")
        # 3 帧横向拼接 → 宽度明显大于单帧目标宽
        self.assertGreater(im.width, 480)

    def test_gif_two_frames(self):
        png = media_reader._gif_to_frames_image(self._make_gif(2), max_frames=3)
        self.assertIsNotNone(png)

    def test_invalid_bytes_returns_none(self):
        self.assertIsNone(media_reader._gif_to_frames_image(b"not-a-gif"))


if __name__ == "__main__":
    unittest.main()
