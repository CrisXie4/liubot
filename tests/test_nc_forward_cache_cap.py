"""content_reader._forward_cache 条数上限测试。

上一轮遗留：缓存只增不删（TTL 惰性过期，条目本身永不清除），长驻进程下
resId 无限累积。锁定：超过 FORWARD_CACHE_MAX 按写入时间清最旧。
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader


def _run(coro):
    return asyncio.run(coro)


def _make_bot(text="内容"):
    bot = mock.MagicMock()
    bot.get_forward_msg = mock.AsyncMock(return_value={
        "messages": [{"nickname": "甲", "message": [{"type": "text", "data": {"text": text}}]}],
    })
    return bot


class TestForwardCacheCap(unittest.TestCase):

    def setUp(self):
        content_reader._forward_cache.clear()
        self._old_max = content_reader.FORWARD_CACHE_MAX
        content_reader.FORWARD_CACHE_MAX = 5

    def tearDown(self):
        content_reader._forward_cache.clear()
        content_reader.FORWARD_CACHE_MAX = self._old_max

    def _fill(self, ids, ts):
        for fid in ids:
            content_reader._forward_cache[fid] = (fid, [], [], ts)

    def test_default_cap_is_300(self):
        """生产默认上限是 300（setUp 打补丁前记录的真实值）"""
        self.assertEqual(self._old_max, 300)

    def test_over_cap_trims_to_cap(self):
        """超上限裁到上限：5 条旧的 + 1 条新的 → 裁掉时间戳最旧的 old1"""
        base = time.time() - 1000
        # 时间戳逐条递增，保证淘汰顺序确定
        for i, fid in enumerate(["old1", "old2", "old3", "old4", "old5"]):
            content_reader._forward_cache[fid] = (fid, [], [], base + i)
        r = _run(content_reader._unfold_forward(_make_bot("新内容"), "new1", 1))
        text, *_ = r
        self.assertIn("新内容", text)
        cache = content_reader._forward_cache
        self.assertEqual(len(cache), 5)
        self.assertIn("new1", cache)
        self.assertNotIn("old1", cache)
        for kept in ("old2", "old3", "old4", "old5"):
            self.assertIn(kept, cache)

    def test_cap_does_not_grow_unbounded(self):
        """连续灌入远超上限的条目，缓存始终不超上限"""
        for i in range(30):
            _run(content_reader._unfold_forward(_make_bot(f"内容{i}"), f"f{i}", 1))
        self.assertEqual(len(content_reader._forward_cache), 5)

    def test_at_cap_no_eviction(self):
        """正好等于上限时不再清"""
        self._fill([f"k{i}" for i in range(5)], time.time())
        _run(content_reader._unfold_forward(_make_bot(), "fresh", 1))
        self.assertEqual(len(content_reader._forward_cache), 5)
        self.assertIn("fresh", content_reader._forward_cache)

    def test_cache_hit_within_ttl(self):
        """上限逻辑不影响原有 TTL 命中路径"""
        now = time.time()
        content_reader._forward_cache["hit1"] = ("缓存文本", [], [], now - 10)
        text, *_ = _run(content_reader._unfold_forward(_make_bot("不该出现"), "hit1", 1))
        self.assertEqual(text, "缓存文本")


if __name__ == "__main__":
    unittest.main()
