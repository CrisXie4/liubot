"""send_sticker 工具结果纯净性测试。

背景：旧实现返回「{cq}（把上面的 CQ 码直接放进你的回复文本里发出去）」，
normalize_face_tags 只删裸 [表情] 占位，AI 照抄整串会把中文括号说明发进群。
锁定：返回值是纯 CQ 码、不含任何括号说明；使用说明固定在 ALL_TOOLS 的
description 里而不是工具结果里。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools


class TestSendStickerToolResult(unittest.TestCase):

    def setUp(self):
        ai_tools._STICKER_RATE.clear()

    def tearDown(self):
        ai_tools._STICKER_RATE.clear()

    def _call(self, emotion: str = "开心") -> str:
        return asyncio.run(ai_tools.execute_tool(
            mock.MagicMock(), 0, 123, "send_sticker", {"emotion": emotion}))

    def test_result_is_pure_cq(self):
        """返回值以 [CQ: 开头，且不再携带中文括号注释"""
        for emotion in ("开心", "无奈", ""):
            r = self._call(emotion)
            self.assertTrue(r.startswith("[CQ:"), f"emotion={emotion!r} → {r!r}")
            self.assertNotIn("（", r, f"emotion={emotion!r} 返回值带了括号说明: {r!r}")
            self.assertNotIn("把上面的", r)

    def test_description_carries_usage_note(self):
        """使用说明固定在 ALL_TOOLS 的 send_sticker description 里"""
        desc = next(t["description"] for t in ai_tools.ALL_TOOLS
                    if t["name"] == "send_sticker")
        self.assertIn("放进回复", desc)
        self.assertIn("不要复述", desc)


if __name__ == "__main__":
    unittest.main()
