"""timeutil 单元测试 —— 用 python tests/test_timeutil.py 直接跑"""
import asyncio
import sys
import os
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import timeutil
from datetime import datetime, timezone


class TestTimeUtil(unittest.TestCase):

    def test_now_shanghai_returns_tz_aware(self):
        """now_shanghai 返回的时间必须带 Asia/Shanghai tz"""
        from zoneinfo import ZoneInfo
        t = timeutil.now_shanghai()
        self.assertIsNotNone(t.tzinfo)
        self.assertEqual(t.tzinfo, ZoneInfo("Asia/Shanghai"))

    def test_offset_applied(self):
        """设了 _offset 后，now_shanghai = 系统时钟 + offset"""
        before = timeutil.now_shanghai()
        fake_offset = timedelta(seconds=42)
        with patch.object(timeutil, "_offset", fake_offset):
            after = timeutil.now_shanghai()
        # 应该比无 offset 时大约多 42 秒（允许 5 秒执行抖动）
        diff = (after - before).total_seconds()
        self.assertGreater(diff, 37, f"offset 未生效，diff={diff}")
        self.assertLess(diff, 47, f"offset 偏差过大，diff={diff}")

    def test_offset_none_uses_system_clock(self):
        """_offset=None 时不崩，退回系统时钟"""
        with patch.object(timeutil, "_offset", None):
            t = timeutil.now_shanghai()
        self.assertIsNotNone(t)

    def test_today_shanghai(self):
        d = timeutil.today_shanghai()
        self.assertIsNotNone(d)

    def test_sync_status_shape(self):
        s = timeutil.get_sync_status()
        self.assertIn("synced", s)
        self.assertIn("offset_seconds", s)
        self.assertIn("last_sources", s)
        self.assertIsInstance(s["sync_count"], int)

    def test_source_list_has_10(self):
        self.assertEqual(len(timeutil._TIME_SOURCES), 10)

    def test_sample_size_within_bounds(self):
        self.assertLessEqual(timeutil._SAMPLE_SIZE, len(timeutil._TIME_SOURCES))

    def test_no_known_stale_sources(self):
        """taobao / apple 的 Date 头长期落后真实时间，不得出现在源列表里"""
        for bad in ("taobao.com", "apple.com"):
            self.assertFalse(
                any(bad in u for u in timeutil._TIME_SOURCES),
                f"源列表不应包含已知时钟漂移源 {bad}",
            )


class TestReachConsensus(unittest.TestCase):
    """_reach_consensus: 多数一致判定"""

    def test_majority_two_of_three_wins(self):
        """2 个一致、1 个离群 → 取多数"""
        off_good = timedelta(seconds=0)
        off_bad = timedelta(seconds=-100)
        offsets = [
            ("https://a", off_good),
            ("https://b", off_good),
            ("https://c", off_bad),
        ]
        res = timeutil._reach_consensus(offsets)
        self.assertIsNotNone(res)
        off, winning = res
        self.assertEqual(off, off_good)
        self.assertIn("https://a", winning)
        self.assertIn("https://b", winning)
        self.assertNotIn("https://c", winning)

    def test_all_disagree_returns_none(self):
        """三者全不一致且超容差 → None（交由上层重试）"""
        offsets = [
            ("https://a", timedelta(seconds=0)),
            ("https://b", timedelta(seconds=-100)),
            ("https://c", timedelta(seconds=-200)),
        ]
        self.assertIsNone(timeutil._reach_consensus(offsets))

    def test_single_source_accepted(self):
        """只有 1 个有效源 → 采纳（聊胜于无）"""
        offsets = [("https://a", timedelta(seconds=5))]
        res = timeutil._reach_consensus(offsets)
        self.assertIsNotNone(res)
        off, winning = res
        self.assertEqual(off, timedelta(seconds=5))


class TestSyncOnce(unittest.TestCase):

    def test_all_sources_fail_returns_none(self):
        """所有源失败时返回 (None, 'all_sources_failed')"""
        async def fake_fetch(url, session):
            return None
        with patch.object(timeutil, "_fetch_source_time", fake_fetch):
            off, status = asyncio.run(timeutil.sync_once())
        self.assertIsNone(off)
        self.assertEqual(status, "all_sources_failed")

    def test_retry_on_disagreement_then_success(self):
        """第 1 轮 3 个源全不一致 → 重试；第 2 轮 2 个一致 → 成功"""
        sys_base = datetime.now(timezone.utc)

        def src_time(sec_off):
            return sys_base + timedelta(seconds=sec_off)

        call = {"n": 0}

        async def fake_fetch(url, session):
            call["n"] += 1
            # 前 3 次（第 1 轮）：全不一致
            if call["n"] <= 3:
                return src_time([-100, 0, -200][call["n"] - 1])
            # 后 3 次（第 2 轮）：两个一致
            return src_time([0, 0, -5][call["n"] - 4])

        with patch.object(timeutil, "_fetch_source_time", fake_fetch):
            off, status = asyncio.run(timeutil.sync_once())
        self.assertEqual(status, "ok")
        self.assertIsNotNone(off)
        # 偏移应在 0 附近（第 2 轮多数值）
        self.assertLess(abs(off.total_seconds()), 5)
        self.assertEqual(call["n"], 6)  # 两轮各 3 次


if __name__ == "__main__":
    unittest.main(verbosity=2)
