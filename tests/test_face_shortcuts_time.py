"""表情快捷码数字边界测试 — 时分秒时间不再被误展开。

「12:30:45」里的 :30: 两侧都是数字，是时间不是表情；
数字语境下几乎必是时间/数值，宁可少展开不可误发。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.onebot_client import cq_text_to_segments, expand_face_shortcuts


class TestFaceShortcutDigitBoundary(unittest.TestCase):

    def test_time_not_expanded(self):
        """时分秒里的 :30: 前后都是数字 → 不产生 [CQ:face"""
        self.assertEqual(expand_face_shortcuts("我们 12:30:45 见"), "我们 12:30:45 见")
        self.assertNotIn("[CQ:face", expand_face_shortcuts("我们 12:30:45 见"))

    def test_ratio_not_expanded(self):
        """"3:2:1" 中 :2: 两侧都是数字 → 不展开"""
        self.assertEqual(expand_face_shortcuts("3:2:1"), "3:2:1")

    def test_shortcut_followed_by_digit_not_expanded(self):
        """【行为变化锁定】快捷码后紧跟数字（数字语境）→ 由旧版的展开变为不展开"""
        self.assertEqual(expand_face_shortcuts(":178:123"), ":178:123")
        self.assertNotIn("[CQ:face", expand_face_shortcuts(":178:123"))

    def test_normal_shortcuts_still_expand(self):
        """行首/汉字/标点相邻的快捷码不受影响"""
        self.assertEqual(expand_face_shortcuts("哈哈:178:"), "哈哈[CQ:face,id=178]")
        self.assertEqual(expand_face_shortcuts("看这个：96："), "看这个[CQ:face,id=96]")
        self.assertEqual(expand_face_shortcuts(":30: 开头"), "[CQ:face,id=30] 开头")

    def test_segments_path_consistent(self):
        """cq_text_to_segments 复用同一正则 → 全链一致"""
        segs = cq_text_to_segments("我们 12:30:45 见")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "我们 12:30:45 见"}}])

        segs2 = cq_text_to_segments("哈哈:178:")
        self.assertEqual(segs2[0], {"type": "text", "data": {"text": "哈哈"}})
        self.assertEqual(segs2[1], {"type": "face", "data": {"id": 178}})


if __name__ == "__main__":
    unittest.main()
