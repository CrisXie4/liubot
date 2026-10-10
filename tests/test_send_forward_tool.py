"""send_forward 工具：node 结构教学、入参校验、每群限速测试。

背景：工具原来没告诉 AI node 的 JSON 结构（大概率发失败），dispatch 也
不校验入参、不限速。锁定三件事：
1. description 带 node 最小 JSON 示例（含 "type": "node" 与 nickname）；
2. 非 node 数组/坏 JSON 返回格式错误提示且不调 NapCat API；
3. 每群 24h 滚动窗口限速（默认 3 次），过期时间戳回填后恢复。
"""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools

# 合法单节点（参照 humanizer._send_as_forward 的构造）
_VALID_NODES = [
    {"type": "node",
     "data": {"user_id": 123, "nickname": "小Q",
              "content": [{"type": "text", "data": {"text": "聊天记录整理"}}]}},
]


def _make_bot():
    bot = mock.MagicMock()
    bot.send_group_forward_msg = mock.AsyncMock(return_value=98765)
    return bot


def _call_forward(bot, messages_str: str) -> str:
    return asyncio.run(ai_tools.execute_tool(
        bot, 0, 123, "send_forward", {"messages": messages_str}))


class TestSendForwardDescription(unittest.TestCase):

    def test_description_has_node_example(self):
        desc = next(t["description"] for t in ai_tools.ALL_TOOLS
                    if t["name"] == "send_forward")
        self.assertIn('"type": "node"', desc)
        self.assertIn("nickname", desc)
        self.assertIn("node", next(
            t["input_schema"]["properties"]["messages"]["description"]
            for t in ai_tools.ALL_TOOLS if t["name"] == "send_forward"))


class TestSendForwardValidation(unittest.TestCase):

    def setUp(self):
        ai_tools._FORWARD_RATE.clear()

    def tearDown(self):
        ai_tools._FORWARD_RATE.clear()

    def test_bad_json_rejected_without_api_call(self):
        bot = _make_bot()
        r = _call_forward(bot, "这不是JSON")
        self.assertIn("JSON", r)
        bot.send_group_forward_msg.assert_not_called()

    def test_non_node_array_rejected_without_api_call(self):
        bot = _make_bot()
        bad = json.dumps([{"type": "text", "data": {"text": "混入非node"}}])
        r = _call_forward(bot, bad)
        self.assertIn("格式错误", r)
        self.assertIn("node", r)
        bot.send_group_forward_msg.assert_not_called()

    def test_non_list_and_empty_rejected(self):
        bot = _make_bot()
        r1 = _call_forward(bot, json.dumps({"type": "node"}))  # 对象不是数组
        r2 = _call_forward(bot, "[]")                          # 空数组
        self.assertIn("格式错误", r1)
        self.assertIn("格式错误", r2)
        bot.send_group_forward_msg.assert_not_called()

    def test_valid_nodes_pass_through(self):
        bot = _make_bot()
        r = _call_forward(bot, json.dumps(_VALID_NODES))
        self.assertIn("已发送", r)
        bot.send_group_forward_msg.assert_awaited_once_with(0, _VALID_NODES)


class TestSendForwardRate(unittest.TestCase):

    def setUp(self):
        ai_tools._FORWARD_RATE.clear()
        self._old_limit = ai_tools._FORWARD_RATE_LIMIT
        ai_tools._FORWARD_RATE_LIMIT = 3

    def tearDown(self):
        ai_tools._FORWARD_RATE.clear()
        ai_tools._FORWARD_RATE_LIMIT = self._old_limit

    def test_fourth_call_within_24h_blocked(self):
        bot = _make_bot()
        payload = json.dumps(_VALID_NODES)
        for _ in range(3):
            self.assertIn("已发送", _call_forward(bot, payload))
        r4 = _call_forward(bot, payload)
        self.assertIn("太频繁", r4)
        self.assertEqual(bot.send_group_forward_msg.await_count, 3)

    def test_expired_timestamps_restore_quota(self):
        bot = _make_bot()
        payload = json.dumps(_VALID_NODES)
        for _ in range(3):
            _call_forward(bot, payload)
        # 回填 25 小时前的时间戳（超出 86400s 窗口）→ 滚动窗口淘汰 → 恢复
        old = time.time() - 90000
        ai_tools._FORWARD_RATE[0] = [old, old + 1, old + 2]
        self.assertIn("已发送", _call_forward(bot, payload))

    def test_different_groups_independent(self):
        bot = _make_bot()
        payload = json.dumps(_VALID_NODES)
        for _ in range(3):
            _call_forward(bot, payload)  # 群 0 用满
        self.assertIn("太频繁", _call_forward(bot, payload))
        self.assertIn("已发送", asyncio.run(ai_tools.execute_tool(
            bot, 777, 123, "send_forward", {"messages": payload})),
            "群 777 不应受群 0 影响")


if __name__ == "__main__":
    unittest.main()
