"""send_sticker 每群滑动窗口限速测试。

口径：monkeypatch _STICKER_RATE_LIMIT=2，同群（group_id=0，绕开表情库读写）
连调 3 次——前两次返回 CQ 码、第三次返回限速提示；回填 2 小时前的时间戳
（超出 3600s 窗口）后恢复可发，验证滚动窗口；不同群互不影响。
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools, sticker


def _call_send_sticker(group_id: int, emotion: str = "开心") -> str:
    bot = mock.MagicMock()
    return asyncio.run(ai_tools.execute_tool(
        bot, group_id, 123, "send_sticker", {"emotion": emotion}))


class TestSendStickerRate(unittest.TestCase):

    def setUp(self):
        ai_tools._STICKER_RATE.clear()
        ai_tools._FORWARD_RATE.clear()
        self._old_limit = ai_tools._STICKER_RATE_LIMIT
        ai_tools._STICKER_RATE_LIMIT = 2

    def tearDown(self):
        ai_tools._STICKER_RATE.clear()
        ai_tools._FORWARD_RATE.clear()
        ai_tools._STICKER_RATE_LIMIT = self._old_limit

    def test_third_call_blocked_with_actionable_hint(self):
        r1 = _call_send_sticker(0)
        r2 = _call_send_sticker(0)
        self.assertTrue(r1.startswith("[CQ:"), r1)
        self.assertTrue(r2.startswith("[CQ:"), r2)
        r3 = _call_send_sticker(0)
        self.assertNotIn("[CQ:", r3, "第三次应被限速，不得再返回 CQ 码")
        self.assertIn("太频繁", r3)

    def test_rolling_window_expires_old_timestamps(self):
        _call_send_sticker(0)
        _call_send_sticker(0)
        self.assertNotIn("[CQ:", _call_send_sticker(0))  # 已达限额
        # 回填 2 小时前的时间戳（远超 3600s 窗口）→ 滚动窗口淘汰 → 恢复可发
        old = time.time() - 7200
        ai_tools._STICKER_RATE[0] = [old, old + 1]
        self.assertTrue(_call_send_sticker(0).startswith("[CQ:"),
                        "过期时间戳不计入窗口，应恢复可发")

    def test_different_groups_independent(self):
        # 非 0 群会走表情库，patch 掉选脸函数避免读写 data/ 下的表情库
        with mock.patch.object(sticker, "pick_emotion_face",
                               return_value="[CQ:face,id=14]"):
            self.assertTrue(_call_send_sticker(555).startswith("[CQ:"))
            self.assertTrue(_call_send_sticker(555).startswith("[CQ:"))
            self.assertIn("太频繁", _call_send_sticker(555))  # 群 555 超限
        self.assertTrue(_call_send_sticker(0).startswith("[CQ:"),
                        "群 0 不应受群 555 影响")

    def test_blocked_call_does_not_consume_quota(self):
        _call_send_sticker(0)
        _call_send_sticker(0)
        self.assertEqual(len(ai_tools._STICKER_RATE[0]), 2)
        _call_send_sticker(0)  # 被拒的这次不记账
        self.assertEqual(len(ai_tools._STICKER_RATE[0]), 2,
                         "超限被拒的调用不应追加时间戳")


if __name__ == "__main__":
    unittest.main()
