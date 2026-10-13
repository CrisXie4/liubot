"""表情收藏 URL 反转义回归测试（2026-10 表情链路 &amp; 修复）。

线上实况：收藏的 image 表情 URL 带 HTML 转义 &amp;，download_to_cache 全部
下载失败（sticker_cache 0 文件），无缓存回退发送的 CQ 码 NapCat 也拉不到。
本文件锁定四处反转义入口：download_to_cache、maybe_collect_sticker 的
image 分支、_build_sticker_cq 无缓存回退、maybe_collect_with_ai 入口。
测试全程使用临时目录，绝不读写真实 data/。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import sticker


def _run(coro):
    return asyncio.run(coro)


# 带转义的典型多媒体 URL（线上收藏的实际形态）
ESCAPED_URL = ("https://multimedia.nt.qq.com.cn/download"
               "?appid=1407&amp;fileid=abc123&amp;spec=0")
REAL_URL = ESCAPED_URL.replace("&amp;", "&")


class _FakeResp:
    """替身响应：200 + >100B 内容（download_to_cache 的落盘阈值）"""

    status_code = 200
    content = b"gif-data" * 128


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：记录实际请求的 URL，恒返回 200"""

    last_url = ""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.last_url = url
        return _FakeResp()


class _TempStickerStore:
    """临时表情库文件 + 临时缓存目录（不碰真实 data/）"""

    def __init__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._old_db = sticker.STICKER_DB_PATH
        self._old_cache = sticker.STICKER_CACHE_DIR
        sticker.STICKER_DB_PATH = os.path.join(self._tmpdir, "stickers.json")
        sticker.STICKER_CACHE_DIR = os.path.join(self._tmpdir, "sticker_cache")
        os.makedirs(sticker.STICKER_CACHE_DIR, exist_ok=True)

    def cleanup(self):
        sticker.STICKER_DB_PATH = self._old_db
        sticker.STICKER_CACHE_DIR = self._old_cache


class TestDownloadToCacheUnescape(unittest.TestCase):
    """(a) download_to_cache 入口反转义：&amp; URL 也能下载落盘"""

    def setUp(self):
        self.tmp = _TempStickerStore()

    def tearDown(self):
        self.tmp.cleanup()

    def test_escaped_url_downloads_with_real_amp(self):
        with patch("httpx.AsyncClient", _FakeAsyncClient):
            path = _run(sticker.download_to_cache(1, ESCAPED_URL))
        self.assertIsNotNone(path)
        self.assertTrue(path.startswith("file:///"))
        # 实际请求的是反转义后的真 &，不是 &amp;
        self.assertEqual(_FakeAsyncClient.last_url, REAL_URL)
        # 已落盘到临时缓存目录
        cached = os.listdir(sticker.STICKER_CACHE_DIR)
        self.assertEqual(cached, ["1.gif"])

    def test_real_url_still_works(self):
        with patch("httpx.AsyncClient", _FakeAsyncClient):
            path = _run(sticker.download_to_cache(2, REAL_URL))
        self.assertIsNotNone(path)
        self.assertEqual(_FakeAsyncClient.last_url, REAL_URL)


class TestBuildStickerCqUnescape(unittest.TestCase):
    """(c) _build_sticker_cq：无缓存回退反转义，有缓存优先 file:///"""

    def setUp(self):
        self.tmp = _TempStickerStore()
        self.sticker = {
            "id": 7,
            "sticker_type": "image",
            "cq_code": f"[CQ:image,file=abc.image,url={ESCAPED_URL}]",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_cache_fallback_unescaped(self):
        cq = sticker._build_sticker_cq(self.sticker)
        self.assertNotIn("&amp;", cq)
        self.assertIn(REAL_URL, cq)

    def test_with_cache_synced_to_napcat_container(self):
        # 本地缓存存在 → 同步进 NapCat 挂载目录，发容器侧路径
        # （NapCat 在 docker 容器里，宿主路径它看不见——2026-10-12 实录 ENOENT）
        import tempfile
        cache_file = os.path.join(sticker.STICKER_CACHE_DIR, "7.gif")
        with open(cache_file, "wb") as f:
            f.write(b"x" * 200)
        with tempfile.TemporaryDirectory() as sync_dir:
            old = sticker.NAPCAT_SYNC_DIR
            sticker.NAPCAT_SYNC_DIR = sync_dir
            try:
                cq = sticker._build_sticker_cq(self.sticker)
            finally:
                sticker.NAPCAT_SYNC_DIR = old
            self.assertEqual(cq, "[CQ:image,file=/app/napcat/plugins/sticker_cache/7.gif]")
            self.assertTrue(os.path.exists(os.path.join(sync_dir, "7.gif")))

    def test_face_type_untouched(self):
        s = {"id": 8, "sticker_type": "face", "cq_code": "[CQ:face,id=14]"}
        self.assertEqual(sticker._build_sticker_cq(s), "[CQ:face,id=14]")


class TestMaybeCollectUnescape(unittest.TestCase):
    """(b) maybe_collect_sticker 的 image 分支入库前反转义"""

    def setUp(self):
        self.tmp = _TempStickerStore()

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_cq_stored_unescaped(self):
        msg = f"[CQ:image,file=abc.image,subType=20,url={ESCAPED_URL}]"
        sid = sticker.maybe_collect_sticker(msg, group_id=1, user_qq=123)
        self.assertIsNotNone(sid)
        stored = sticker.get_sticker(sid)
        self.assertEqual(stored["sticker_type"], "image")
        self.assertNotIn("&amp;", stored["cq_code"])
        self.assertIn(REAL_URL, stored["cq_code"])


class TestMaybeCollectWithAiUnescape(unittest.TestCase):
    """(d) maybe_collect_with_ai 入口反转义（handler 传入的 c["raw"] 含 &amp;）"""

    def setUp(self):
        self.tmp = _TempStickerStore()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ai_collect_stores_unescaped_cq(self):
        raw_cq = f"[CQ:image,file=abc.image,url={ESCAPED_URL}]"
        with patch.object(sticker, "evaluate_sticker_with_ai",
                          AsyncMock(return_value={"keep": True, "score": 7,
                                                  "tags": ["搞笑"]})), \
             patch.object(sticker, "download_to_cache", AsyncMock(return_value=None)):
            sid = _run(sticker.maybe_collect_with_ai(
                "image", raw_cq, {"file": "abc.image", "url": ESCAPED_URL}, 1, 123))
        self.assertIsNotNone(sid)
        stored = sticker.get_sticker(sid)
        self.assertNotIn("&amp;", stored["cq_code"])
        self.assertIn(REAL_URL, stored["cq_code"])


if __name__ == "__main__":
    unittest.main()
