"""「发同款」链路测试 — 目录含义直配 + 最近收藏注入 + sticker_id 精准发送。

2026-10-10 实录：群友发了表情让 bot「来一个同款」，send_sticker 在收藏库
没匹配到（其实刚自动收藏为 #93），兜底成了小黄脸微笑——对用户等于没发。
三层修复：目录含义直配（吃瓜→:271:）、最近收藏注入上下文、sticker_id 精准发。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools, sticker
from bot.sticker import pick_emotion_face, recent_stickers_summary


class TestCatalogDirectMatch(unittest.TestCase):
    """目录含义直配：只接管情绪表没覆盖的词，已覆盖的保持原路径"""

    def test_chipaguais_in_catalog(self):
        """吃瓜：情绪表没有、目录有 271:吃瓜 → 直接发 271 而不是微笑兜底"""
        self.assertEqual(pick_emotion_face("吃瓜", group_id=0), "[CQ:face,id=271]")

    def test_doge_in_catalog(self):
        self.assertEqual(pick_emotion_face("doge", group_id=0), "[CQ:face,id=179]")

    def test_mapped_emotions_unchanged(self):
        """开心/尴尬这类情绪表已覆盖的词仍走原映射（目录不抢）"""
        import random
        random.seed(7)
        allowed = set(sticker.EMOTION_FACE_MAP["尴尬"])
        for _ in range(6):
            cq = pick_emotion_face("尴尬", group_id=0)
            self.assertTrue(cq.startswith("[CQ:face,id="), cq)
            self.assertIn(int(cq.split("id=")[1].rstrip("]")), allowed)


class TestRecentStickersSummary(unittest.TestCase):

    def _lib(self):
        return {
            "93": {"id": 93, "group_id": 555, "sticker_type": "image",
                   "tags": ["像素风", "可爱", "摸鱼"], "description": "像素猫打工",
                   "use_count": 0},
            "92": {"id": 92, "group_id": 555, "sticker_type": "mface",
                   "tags": ["商城"], "description": "", "use_count": 0},
            "91": {"id": 91, "group_id": 0, "sticker_type": "face",
                   "tags": ["生气"], "description": "", "use_count": 3},
        }

    def test_order_desc_and_mface_excluded(self):
        with mock.patch.object(sticker, "_load_stickers", return_value=self._lib()):
            out = recent_stickers_summary(group_id=555)
        self.assertIn("#93", out)
        self.assertNotIn("#92", out, "mface 链接会过期，不应列出")
        self.assertLess(out.index("#93"), out.index("#91") if "#91" in out else 1 << 30,
                        "id 越大越新，应排前面（#91 是全局表情，555 群可见）")

    def test_group_filter(self):
        """群 777 看不到 555 群的 #93，但全局表情 #91（group_id=0）可见"""
        with mock.patch.object(sticker, "_load_stickers", return_value=self._lib()):
            out = recent_stickers_summary(group_id=777)
        self.assertNotIn("#93", out)
        self.assertNotIn("#92", out)
        self.assertIn("#91", out)

    def test_empty_library_returns_empty(self):
        with mock.patch.object(sticker, "_load_stickers", return_value={}):
            self.assertEqual(recent_stickers_summary(group_id=1), "")


class TestSendStickerById(unittest.TestCase):

    def setUp(self):
        ai_tools._STICKER_RATE.clear()

    def tearDown(self):
        ai_tools._STICKER_RATE.clear()

    def _call(self, bot, tool_input, group_id=555):
        return asyncio.run(ai_tools.execute_tool(
            bot, group_id, 456, "send_sticker", tool_input))

    def test_sticker_id_sends_exact_sticker(self):
        bot = mock.MagicMock()
        bot.send_group_msg = mock.AsyncMock(return_value=999)
        bot.last_fail_msg = mock.MagicMock(return_value="")
        with mock.patch.object(sticker, "get_sticker",
                               return_value={"id": 93, "sticker_type": "image"}), \
             mock.patch.object(sticker, "_build_sticker_cq",
                               return_value="[CQ:image,file=93.gif]"), \
             mock.patch.object(sticker, "use_sticker") as used:
            r = self._call(bot, {"sticker_id": 93})
        self.assertIn("已直接发出", r)
        self.assertNotIn("[CQ:", r, "返回给模型的应是说明而非码")
        bot.send_group_msg.assert_called_once_with(555, "[CQ:image,file=93.gif]")
        used.assert_called_once_with(93)

    def test_missing_id_reports_failure(self):
        bot = mock.MagicMock()
        bot.send_group_msg = mock.AsyncMock(return_value=999)
        bot.last_fail_msg = mock.MagicMock(return_value="")
        with mock.patch.object(sticker, "get_sticker", return_value=None):
            r = self._call(bot, {"sticker_id": 999})
        self.assertIn("⨯", r)
        self.assertIn("#999", r)
        bot.send_group_msg.assert_not_called()

    def test_description_mentions_sticker_id(self):
        desc = next(t["description"] for t in ai_tools.ALL_TOOLS
                    if t["name"] == "send_sticker")
        self.assertIn("sticker_id", desc)
        schema_props = next(t for t in ai_tools.ALL_TOOLS
                            if t["name"] == "send_sticker")["input_schema"]["properties"]
        self.assertIn("sticker_id", schema_props)


class TestBuildStickerCqBadCache(unittest.TestCase):
    """存量坏记录：cq 指向本地路径但文件缺失 → URL 兜底/判死，不再必 ENOENT

    2026-10-11 实录：#89 的 cq_code 是 file=//www/.../89.gif 且文件不存在，
    NapCat ENOENT 发送失败，AI 把失败转述成"网络问题"。
    """

    def test_dead_local_path_without_url_dies(self):
        """纯本地路径且文件丢失、无 url → 返回空串判死（不发必败的码）"""
        s = {"id": 424242, "sticker_type": "image",
             "cq_code": "[CQ:image,file=/nonexistent/qqbot_cache/424242.gif]"}
        self.assertEqual(sticker._build_sticker_cq(s), "")

    def test_dead_local_path_with_url_falls_back(self):
        """本地文件丢失但有 url → 用 URL 直发兜底（同步上下文无循环，补缓存静默跳过）"""
        s = {"id": 424243, "sticker_type": "image",
             "cq_code": "[CQ:image,file=/nonexistent/x/424243.gif,url=https://x.example/a.gif]"}
        self.assertEqual(sticker._build_sticker_cq(s),
                         "[CQ:image,file=https://x.example/a.gif]")

    def test_qq_internal_filename_kept(self):
        """file= 是 QQ 内部文件名（非本地路径）→ 原样保留，NapCat 自己按 url 拉"""
        s = {"id": 424244, "sticker_type": "image",
             "cq_code": "[CQ:image,file=abc.gif,url=https://x.example/b.gif]"}
        self.assertEqual(sticker._build_sticker_cq(s),
                         "[CQ:image,file=abc.gif,url=https://x.example/b.gif]")

    def test_existing_local_file_used(self):
        """本地路径且文件真实存在 → 原样使用"""
        import os as _os
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".gif")
        try:
            _os.write(fd, b"x" * 200)
            _os.close(fd)
            s = {"id": 424245, "sticker_type": "image",
                 "cq_code": f"[CQ:image,file={p}]"}
            self.assertEqual(sticker._build_sticker_cq(s), f"[CQ:image,file={p}]")
        finally:
            _os.remove(p)


if __name__ == "__main__":
    unittest.main()
