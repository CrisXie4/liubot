"""主动消息软限速测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import guardian


class TestProactiveRate(unittest.TestCase):

    def setUp(self):
        guardian._rate_state.clear()

    def test_allows_first_8(self):
        for i in range(8):
            self.assertTrue(guardian.check_proactive_rate(123456),
                            f"第 {i+1} 次应允许")

    def test_blocks_9th(self):
        for _ in range(8):
            guardian.check_proactive_rate(123456)
        self.assertFalse(guardian.check_proactive_rate(123456),
                         "第 9 次应拒绝")

    def test_different_groups_independent(self):
        for _ in range(8):
            guardian.check_proactive_rate(111)
        self.assertTrue(guardian.check_proactive_rate(222),
                        "群 222 不应受群 111 影响")


if __name__ == "__main__":
    unittest.main()
