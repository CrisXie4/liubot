"""合并转发可见性测试 — 没人 @ 的合并转发也能进决策/记忆。

转发可见性：决策引擎和记忆侧拿到的是「[合并转发:N条] 摘要」
而不是裸 [CQ:forward,id=..]（此前 AI 完全看不见转发内容）。
"""
import inspect
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler
from bot.handler import _forward_aware_text


class TestForwardAwareText(unittest.TestCase):

    def test_forward_replaced_with_summary(self):
        """含 forward CQ + forward_block 有 text → 替换为摘要"""
        raw = "看看这个[CQ:forward,id=abc123]"
        block = {"count": 3, "text": "第一条\n第二条\n第三条",
                 "images": [], "stickers": [], "nested": False}
        out = _forward_aware_text(raw, block)
        self.assertIn("[合并转发:3条]", out)
        self.assertNotIn("[CQ:forward", out)
        self.assertIn("第一条", out)

    def test_long_text_truncated_150(self):
        """正文超 150 字被截（防大包灌高决策相关性）"""
        raw = "[CQ:forward,id=x1]"
        block = {"count": 10, "text": "字" * 500}
        out = _forward_aware_text(raw, block)
        summary = out.split("] ", 1)[1] if "] " in out else ""
        self.assertLessEqual(len(summary), 150)
        self.assertNotIn("字" * 200, out)

    def test_no_block_returns_raw(self):
        """forward_block 为 None → 原样返回"""
        raw = "[CQ:forward,id=x2]"
        self.assertEqual(_forward_aware_text(raw, None), raw)

    def test_no_forward_cq_returns_raw(self):
        """raw 不含 forward CQ → 原样返回"""
        raw = "普通消息 [CQ:image,file=a.jpg]"
        block = {"count": 1, "text": "无关内容"}
        self.assertEqual(_forward_aware_text(raw, block), raw)

    def test_block_without_text_returns_raw(self):
        """展开失败（block 无 text）→ 原样返回，不产生空摘要"""
        raw = "[CQ:forward,id=x3]"
        self.assertEqual(_forward_aware_text(raw, {"count": 5, "text": ""}), raw)
        self.assertEqual(_forward_aware_text(raw, {}), raw)

    def test_empty_raw(self):
        self.assertEqual(_forward_aware_text("", {"count": 1, "text": "x"}), "")

    def test_count_zero(self):
        """count 缺失/为 0 → 摘要里显示 0 条（容错不崩）"""
        out = _forward_aware_text("[CQ:forward,id=x4]", {"text": "内容"})
        self.assertIn("[合并转发:0条]", out)

    def test_backslash_sequences_inserted_verbatim(self):
        """摘录含 \\1、\\g<1> 等序列 → 按字面插入，不抛 PatternError 也不吞字
        （回归：summary 曾被直接当 re.sub 替换串，含组引用序列时
        四个调用点全无 try 包裹，异常上抛炸掉整条消息的处理）"""
        for sample in (r"看这个路径 D:\1 试试", r"正则片段 \g<1> 演示",
                       r"\g<0> \2 \\ 结尾", r"普通\路径\n换行符"):
            raw = "[CQ:forward,id=bs1]"
            out = _forward_aware_text(raw, {"count": 1, "text": sample})
            self.assertIn(sample, out, f"摘录应字面保留: {sample!r}")
            self.assertNotIn("[CQ:forward", out)


class TestConsumptionPointsWired(unittest.TestCase):
    """handle_group_message 的 4 个消费点已改用 _forward_aware_text"""

    def setUp(self):
        self.src = inspect.getsource(handler.handle_group_message)

    def test_memory_point(self):
        self.assertIn("_forward_aware_text(raw_message, content.get(\"forward\"))", self.src)
        self.assertIn("fwd_visible", self.src)

    def test_add_context_point(self):
        self.assertIn("chat.add_context", self.src)
        self.assertIn("_forward_aware_text(raw_message, content.get('forward'))", self.src)

    def test_decision_engine_points(self):
        """followup 决策与主动决策两处的 message 实参都改用该函数"""
        self.assertEqual(
            self.src.count("_forward_aware_text(raw_message, content.get(\"forward\"))")
            + self.src.count("_forward_aware_text(raw_message, content.get('forward'))"),
            4,
            "记忆/上下文/会话决策/主动决策 4 个消费点都应使用 _forward_aware_text",
        )


if __name__ == "__main__":
    unittest.main()
