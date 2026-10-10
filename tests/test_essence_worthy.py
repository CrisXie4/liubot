"""is_essence_worthy 单元测试 —— 用 python tests/test_essence_worthy.py 直接跑"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.ai_brain import is_essence_worthy


class TestEssenceWorthy(unittest.TestCase):

    def test_trivial_greetings_rejected(self):
        """打招呼/探活类必须被拦"""
        for t in ["你好", "您好", "在吗", "哈喽", "hi", "hello", "晚上好", "晚安",
                  "早上好", "早", "嗨", "你是机器人吗", "你是活的吗", "活着吗",
                  "测试", "test"]:
            self.assertFalse(is_essence_worthy(t), f"应被拦: «{t}»")

    def test_trivial_answers_rejected(self):
        """应答/水/感谢类必须被拦"""
        for t in ["好的", "嗯", "哦", "收到", "了解", "ok", "谢谢", "辛苦了",
                  "是的", "对的", "明白", "知道啦"]:
            self.assertFalse(is_essence_worthy(t), f"应被拦: «{t}»")

    def test_pure_laughter_and_emoji_rejected(self):
        """纯笑声/纯标点/纯表情必须被拦"""
        for t in ["哈哈", "哈哈哈", "哈哈哈哈哈哈", "呵呵", "嘻嘻", "666",
                  "！！！！", "？？？", "？？？！！！", "嗯嗯嗯",
                  "[CQ:face,id=178]", "🎉🎉🎉", "哈哈哈哈[CQ:face,id=178]"]:
            self.assertFalse(is_essence_worthy(t), f"应被拦: «{t}»")

    def test_too_short_rejected(self):
        """≤4 个硬字符必须被拦"""
        for t in ["", "   ", "好的呢", "来啦", "去吧", "12", "abc", "你好啊"]:
            self.assertFalse(is_essence_worthy(t), f"应被拦: «{t}»")

    def test_with_cq_codes_stripped(self):
        """带 @ / 引用 CQ 码的水内容仍应被拦"""
        self.assertFalse(is_essence_worthy("[CQ:at,qq=12345] 你好"))
        self.assertFalse(is_essence_worthy("[CQ:reply,id=99] 嗯"))
        self.assertFalse(is_essence_worthy("你好 [CQ:face,id=178]"))

    def test_meaningful_content_accepted(self):
        """真正有信息量的内容应放行（够不够精华交给 AI 判断）"""
        for t in [
            "今天下午三点半在二号会议室开会，记得带笔记本电脑",
            "这个 bug 的根因是并发写同一个文件没有加锁",
            "我查了文档，这个参数默认是 false，改成 true 之后就好了",
            "推荐一本好书《深入理解计算机系统》，讲底层原理特别清楚",
            "你说的这个观点我很认同，因为从统计学角度看相关性确实不等于因果性",
        ]:
            self.assertTrue(is_essence_worthy(t), f"应放行: «{t[:20]}»")

    def test_image_only_accepted(self):
        """纯图片/合并转发没文字，可能值得精华，放行"""
        self.assertTrue(is_essence_worthy("[CQ:image,file=abc.jpg]"))
        self.assertTrue(is_essence_worthy("[CQ:forward,id=xxx]"))

    def test_image_plus_trivial_text_rejected(self):
        """图片配一句水文字（如纯「哈」）应被拦"""
        self.assertFalse(is_essence_worthy("[CQ:image,file=abc.jpg] 哈"))
        self.assertFalse(is_essence_worthy("你好 [CQ:image,file=abc.jpg]"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
