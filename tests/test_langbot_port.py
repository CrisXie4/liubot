"""LangBot/Jianer 精髓移植测试：输入防抖聚合 + 长文本渲染成图片。"""
import sys
import os
import asyncio
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler, text_image


def _run(coro):
    return asyncio.run(coro)


class TestDebounceAggregate(unittest.TestCase):
    """连发消息合并为一次回答（LangBot aggregator 语义）"""

    def setUp(self):
        handler._debounce_windows.clear()

    def tearDown(self):
        handler._debounce_windows.clear()

    def test_first_message_waits_and_returns_merged(self):
        async def scenario():
            async def late_arrival():
                await asyncio.sleep(0.3)
                return await handler._collect_debounced_message(
                    1, 100, "第二条", "第二条")
            task = asyncio.create_task(late_arrival())
            merged = await handler._collect_debounced_message(
                1, 100, "第一条", "第一条")
            second = await task
            return merged, second

        merged, second = _run(scenario())
        self.assertEqual(merged, "第一条 / 第二条")
        self.assertIsNone(second)  # 第二条被收编，不单独回复

    def test_single_message_passes_through(self):
        merged = _run(handler._collect_debounced_message(
            2, 200, "就一条消息", "就一条消息"))
        self.assertEqual(merged, "就一条消息")

    def test_different_users_independent(self):
        async def scenario():
            t1 = asyncio.create_task(handler._collect_debounced_message(
                3, 300, "甲的话", "甲的话"))
            t2 = asyncio.create_task(handler._collect_debounced_message(
                3, 301, "乙的话", "乙的话"))
            return await t1, await t2

        r1, r2 = _run(scenario())
        self.assertEqual(r1, "甲的话")
        self.assertEqual(r2, "乙的话")

    def test_cq_codes_stripped_from_merged(self):
        merged = _run(handler._collect_debounced_message(
            4, 400, "[CQ:at,qq=123] 看这个", "看这个"))
        self.assertEqual(merged, "看这个")

    def test_window_expires_new_session(self):
        """窗口过期后同一用户新消息成为新的首条"""
        async def scenario():
            await handler._collect_debounced_message(5, 500, "旧消息", "旧消息")
            # 窗口已过 → 新窗口首条
            return await handler._collect_debounced_message(5, 500, "新消息", "新消息")
        merged = _run(scenario())
        self.assertEqual(merged, "新消息")


class TestTextImageRender(unittest.TestCase):
    """长文本渲染成图片（需 data/fonts 下有中文字体；无字体则跳过）"""

    @classmethod
    def setUpClass(cls):
        cls.font = text_image._has_font()

    def test_render_returns_file_path(self):
        if not self.font:
            self.skipTest("无中文字体")
        path = text_image.render_text_image("第一行内容\n\n第二行内容 " * 5, "测试")
        self.assertTrue(path and path.startswith("file:///"))
        self.assertTrue(os.path.exists(path[8:]))
        os.remove(path[8:])

    def test_empty_text_returns_none(self):
        self.assertIsNone(text_image.render_text_image("  "))

    def test_capacity_limit(self):
        if not self.font:
            self.skipTest("无中文字体")
        with self.assertRaises(text_image.TextImageCapacityError):
            text_image.render_text_image("超长" * (text_image.MAX_CHARS // 2 + 10))

    def test_line_wrap_no_split_ascii_token(self):
        """数字/单词不该被拆断到两行"""
        if not self.font:
            self.skipTest("无中文字体")
        font = text_image._load_font(text_image.FONT_SIZE)
        text = "价格是12345678901234567890元哦"
        lines = text_image._split_text_lines(text, font, font.getlength("价" * 12))
        joined = "".join(lines)
        self.assertIn("12345678901234567890", joined.replace("\n", ""))
        # 数字串必须完整出现在同一行
        self.assertTrue(any("12345678901234567890" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
