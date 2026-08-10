"""punish 单元测试 —— 用 python tests/test_punish.py 直接跑"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import punish


class TestMuteLadder(unittest.TestCase):

    def test_first_offense_30min(self):
        """第1次：30 分钟"""
        sec, desc = punish.compute_mute_seconds(1)
        self.assertEqual(sec, 1800)
        self.assertEqual(desc, "30分钟")

    def test_second_offense_1hour(self):
        """第2次：1 小时"""
        sec, desc = punish.compute_mute_seconds(2)
        self.assertEqual(sec, 3600)
        self.assertEqual(desc, "1小时")

    def test_third_offense_3hour(self):
        """第3次：3 小时"""
        sec, desc = punish.compute_mute_seconds(3)
        self.assertEqual(sec, 10800)
        self.assertEqual(desc, "3小时")

    def test_fourth_offense_24hour(self):
        """第4次：24 小时"""
        sec, desc = punish.compute_mute_seconds(4)
        self.assertEqual(sec, 86400)
        self.assertEqual(desc, "24小时")

    def test_fifth_offense_kick_approval(self):
        """第5次+：踢人审批（不再禁言）"""
        sec, desc = punish.compute_mute_seconds(5)
        self.assertEqual(sec, 0)
        self.assertEqual(desc, "踢人审批")
        # 第 10 次也走踢人
        sec, desc = punish.compute_mute_seconds(10)
        self.assertEqual(sec, 0)

    def test_ladder_is_monotonic(self):
        """阶梯时长单调递增"""
        secs = [punish.compute_mute_seconds(n)[0] for n in range(1, 5)]
        self.assertEqual(secs, sorted(secs))
        self.assertEqual(len(set(secs)), 4)  # 互不相同


if __name__ == "__main__":
    unittest.main(verbosity=2)
