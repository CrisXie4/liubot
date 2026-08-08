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


class TestSyncOnce(unittest.TestCase):

    def test_all_sources_fail_returns_none(self):
        """所有源失败时返回 (None, 'all_sources_failed')"""
        async def fake_fetch(url, session):
            return None
        with patch.object(timeutil, "_fetch_source_time", fake_fetch):
            off, status = asyncio.run(timeutil.sync_once())
        self.assertIsNone(off)
        self.assertEqual(status, "all_sources_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
