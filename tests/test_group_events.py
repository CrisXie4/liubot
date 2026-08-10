"""group_events 单元测试 —— 用 python tests/test_group_events.py 直接跑"""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import group_events


class TestGroupEvents(unittest.TestCase):

    def setUp(self):
        # 每个测试清空，避免互相干扰
        group_events.clear_group(999)

    def test_record_and_build_block(self):
        """记录事件后 build_recent_events_block 返回非空、含事件信息"""
        group_events.record(999, "mute", 123456, "嘟嘟", "600s")
        block = group_events.build_recent_events_block(999)
        self.assertIn("禁言", block)
        self.assertIn("嘟嘟", block)

    def test_empty_group_returns_empty_string(self):
        """没记录过事件的群返回空串"""
        self.assertEqual(group_events.build_recent_events_block(888), "")

    def test_limit_caps_output(self):
        """limit 限制返回的事件数"""
        for i in range(10):
            group_events.record(999, "recall", 100 + i, f"用户{i}")
        block = group_events.build_recent_events_block(999, limit=3)
        # 最新的 3 条（用户9/8/7）
        self.assertIn("用户9", block)
        self.assertIn("用户7", block)
        self.assertNotIn("用户6", block)

    def test_ttl_expired_events_excluded(self):
        """超过 TTL 的事件不返回"""
        group_events.record(999, "recall", 123, "老人")
        # 篡改时间戳模拟过期
        events = group_events._events.get(999)
        if events:
            old_ts = time.time() - 3600  # 1 小时前
            events[0] = dict(events[0])
            events[0]["ts"] = old_ts
        block = group_events.build_recent_events_block(999)
        self.assertNotIn("老人", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
