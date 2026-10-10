"""get_forward_msg 客户端适配测试 — 关键字 id / 昵称归一化 / 内联嵌套拍平。

审计原始 bug：content_reader 以 get_forward_msg(id=…) 调用，
旧签名只有 message_id → 每次必抛 TypeError，转发永远解析成
「[合并转发-无法解析]」。
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader
from bot.onebot_client import OneBotClient, _flatten_inline_forward


def _run(coro):
    return asyncio.run(coro)


class TestGetForwardMsgAdapter(unittest.TestCase):

    def setUp(self):
        # 清掉 content_reader 的模块级缓存，避免命中缓存让 mock 失效
        content_reader._forward_cache.clear()
        self.client = OneBotClient()
        self.calls: list[dict] = []

        async def fake_call(action, params):
            self.calls.append({"action": action, "params": params})
            return self._resp

        self.client._call = fake_call

    # ─── gap-9：签名适配 ───

    def test_id_keyword_no_typeerror(self):
        """get_forward_msg(id="resId") 不抛 TypeError 且 _call 收到 {"id": ...}"""
        self._resp = {"status": "ok", "data": {"messages": []}}
        data = _run(self.client.get_forward_msg(id="resId-abc"))
        self.assertIsNotNone(data)
        self.assertEqual(self.calls[0]["params"], {"id": "resId-abc"})

    def test_positional_message_id_still_works(self):
        """旧的 message_id 位置参数调用保持兼容"""
        self._resp = {"status": "ok", "data": {"messages": []}}
        data = _run(self.client.get_forward_msg("pos-id"))
        self.assertIsNotNone(data)
        self.assertEqual(self.calls[0]["params"], {"id": "pos-id"})

    def test_missing_id_returns_none_without_call(self):
        """id/message_id 均缺省 → 返回 None 且不发 API 请求（不发 {"id": None}）"""
        self._resp = {"status": "ok", "data": {"messages": []}}
        data = _run(self.client.get_forward_msg())
        self.assertIsNone(data)
        self.assertEqual(self.calls, [])

    def test_empty_string_id_returns_none(self):
        self._resp = {"status": "ok", "data": {"messages": []}}
        data = _run(self.client.get_forward_msg(id=""))
        self.assertIsNone(data)
        self.assertEqual(self.calls, [])

    def test_api_failure_returns_none(self):
        self._resp = {"status": "failed", "retcode": -1, "msg": "x"}
        data = _run(self.client.get_forward_msg(id="whatever"))
        self.assertIsNone(data)

    # ─── gap-10：昵称归一化 ───

    def test_napcat_shape_nickname_from_sender(self):
        """只给 sender.nickname（NapCat 形状）→ 展开文本每行带 [昵称] 前缀"""
        self._resp = {"status": "ok", "data": {"messages": [
            {"sender": {"nickname": "Alice", "user_id": 1},
             "message": [{"type": "text", "data": {"text": "你好"}}]},
            {"sender": {"nickname": "Bob", "user_id": 2},
             "message": [{"type": "text", "data": {"text": "在吗"}}]},
        ]}}
        result = _run(content_reader.extract_full_content(
            self.client, "[CQ:forward,id=nick1]", []))
        self.assertIn("[Alice] 你好", result["forward"]["text"])
        self.assertIn("[Bob] 在吗", result["forward"]["text"])

    def test_gocqhttp_shape_top_level_nickname(self):
        """顶层 nickname 形状（go-cqhttp）不受影响"""
        self._resp = {"status": "ok", "data": {"messages": [
            {"nickname": "老王", "message": [{"type": "text", "data": {"text": "嗯"}}]},
        ]}}
        result = _run(content_reader.extract_full_content(
            self.client, "[CQ:forward,id=nick2]", []))
        self.assertIn("[老王] 嗯", result["forward"]["text"])

    # ─── gap-11：内联嵌套转发拍平 ───

    def test_inline_nested_forward_flattened(self):
        """嵌套段 data.content 已内联 → 直接拍平，不按 id 重查、不再「无法解析」"""
        self._resp = {"status": "ok", "data": {"messages": [
            {"nickname": "外层",
             "message": [
                 {"type": "text", "data": {"text": "看这个"}},
                 {"type": "forward", "data": {
                     "id": "inner-1",
                     "content": [
                         {"nickname": "小张", "message": [{"type": "text", "data": {"text": "内层一句话"}}]},
                     ],
                 }},
             ]},
        ]}}
        result = _run(content_reader.extract_full_content(
            self.client, "[CQ:forward,id=outer1]", []))
        text = result["forward"]["text"]
        self.assertIn("内层一句话", text)
        self.assertIn("[小张] 内层一句话", text)
        self.assertIn("[内嵌转发]", text)
        self.assertNotIn("[合并转发-无法解析]", text)

    def test_nested_forward_without_content_still_refetch_by_id(self):
        """无 content 的 forward 段保持原样（content_reader 仍可按 id 递归）"""
        self._resp = {"status": "ok", "data": {"messages": [
            {"nickname": "外层",
             "message": [{"type": "forward", "data": {"id": "inner-2"}}]},
        ]}}
        inner_resp = {"status": "ok", "data": {"messages": [
            {"nickname": "小李", "message": [{"type": "text", "data": {"text": "递归内容"}}]},
        ]}}
        self._resp_seq = [self._resp, inner_resp]

        async def fake_call_seq(action, params):
            self.calls.append({"action": action, "params": params})
            return self._resp_seq.pop(0)

        self.client._call = fake_call_seq
        result = _run(content_reader.extract_full_content(
            self.client, "[CQ:forward,id=outer2]", []))
        self.assertIn("递归内容", result["forward"]["text"])

    def test_deep_inline_nesting_no_crash(self):
        """深度 > 3 的内联嵌套停止展开，不崩"""
        # 构造 5 层内联嵌套：每层 forward 段都带 data.content
        def make_level(n: int) -> dict:
            inner = {
                "nickname": f"user{n}",
                "message": [
                    {"type": "text", "data": {"text": f"L{n}"}},
                    {"type": "forward", "data": {"id": f"deep-{n}", "content": [
                        make_level(n + 1)] if n < 5 else []}},
                ],
            }
            return inner

        flat = _flatten_inline_forward([make_level(1)], depth=1)
        # 深度 1..3 会展开（L1/L2/L3），更深的内容不再出现；不抛异常即可
        self.assertIn("[内嵌转发]", flat)
        self.assertIn("L1", flat)
        self.assertNotIn("L4", flat)


if __name__ == "__main__":
    unittest.main()
