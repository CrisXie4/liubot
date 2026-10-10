"""表情快捷码 :178: / ：178： 展开测试（字面码漏进群事故回归）。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.onebot_client import cq_text_to_segments, expand_face_shortcuts, has_cq_codes


class TestFaceShortcuts(unittest.TestCase):

    def test_half_width(self):
        self.assertEqual(expand_face_shortcuts("哈哈:178:"), "哈哈[CQ:face,id=178]")

    def test_full_width(self):
        """DeepSeek 爱输出的全角冒号变体必须同样展开"""
        self.assertEqual(expand_face_shortcuts("哈哈：178："), "哈哈[CQ:face,id=178]")

    def test_segments_path_expands(self):
        """此前快捷码只在字符串路径展开，段路径（转发节点等）会漏字面码"""
        segs = cq_text_to_segments("你看长文章的表情:178:")
        self.assertEqual(segs[0]["type"], "text")
        self.assertEqual(segs[1]["type"], "face")
        self.assertEqual(segs[1]["data"]["id"], 178)

    def test_segments_path_full_width(self):
        segs = cq_text_to_segments("看这个：96：")
        self.assertEqual(segs[0]["type"], "text")
        self.assertEqual(segs[1]["type"], "face")
        self.assertEqual(segs[1]["data"]["id"], 96)

    def test_plain_text_untouched(self):
        segs = cq_text_to_segments("普通一句话")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "普通一句话"}}])

    def test_has_cq_codes_detects_shortcut(self):
        self.assertTrue(has_cq_codes(":178:"))
        self.assertTrue(has_cq_codes("：178："))
        self.assertFalse(has_cq_codes("没有表情"))


if __name__ == "__main__":
    unittest.main()
