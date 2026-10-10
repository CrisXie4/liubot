"""长消息 → 合并转发（学 Jianer 3段规则 + LangBot 降级链）测试。"""
import sys
import os
import time
import asyncio
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import humanizer


class _FakeBot:
    def __init__(self, forward_ok=True):
        self.bot_qq = "3685789505"
        self.forward_ok = forward_ok
        self.forward_calls = []
        self.msg_calls = []

    async def send_group_forward_msg(self, group_id, messages):
        self.forward_calls.append((group_id, messages))
        return 12345 if self.forward_ok else None

    async def send_group_msg(self, group_id, message):
        self.msg_calls.append((group_id, message))
        return 6789

    async def delete_msg(self, message_id):
        return True

    async def send_private_msg(self, uid, message):
        self.msg_calls.append((uid, message))
        return 6789


def _run(coro):
    return asyncio.run(coro)


class TestForwardNodeSplit(unittest.TestCase):

    def test_short_bubbles_pass_through(self):
        out = humanizer._split_for_forward_nodes(["第一段", "第二段"])
        self.assertEqual(out, ["第一段", "第二段"])

    def test_long_bubble_split_by_sentence(self):
        long_text = "这是第一句话。这是第二句话！" * 60  # ~720 字
        out = humanizer._split_for_forward_nodes([long_text])
        self.assertGreater(len(out), 1)
        for piece in out:
            self.assertLessEqual(
                humanizer._visible_len(piece),
                humanizer.FORWARD_NODE_MAX_LEN + 20)  # 句子边界允许小超
        self.assertIn("。", out[0])  # 按句子边界切，不撕半句

    def test_no_half_sentence(self):
        long_text = ("甲" * 30 + "。") * 30
        out = humanizer._split_for_forward_nodes([long_text])
        for piece in out[:-1]:
            self.assertTrue(piece.endswith("。"))


class TestSendAsForward(unittest.TestCase):

    def test_success_builds_nodes(self):
        bot = _FakeBot(forward_ok=True)
        bubbles = ["第一段内容", "第二段内容"]
        ok = _run(humanizer._send_as_forward(bot, 123, bubbles))
        self.assertTrue(ok)
        self.assertEqual(len(bot.forward_calls), 1)
        gid, nodes = bot.forward_calls[0]
        self.assertEqual(gid, 123)
        self.assertEqual(len(nodes), 2)
        node = nodes[0]
        self.assertEqual(node["type"], "node")
        self.assertEqual(node["data"]["user_id"], 3685789505)
        self.assertTrue(node["data"]["nickname"])
        self.assertIsInstance(node["data"]["content"], list)

    def test_failure_returns_false(self):
        bot = _FakeBot(forward_ok=False)
        ok = _run(humanizer._send_as_forward(bot, 123, ["内容"]))
        self.assertFalse(ok)


class TestHumanLikeLongReply(unittest.TestCase):

    def setUp(self):
        humanizer._last_send_time.clear()

    def tearDown(self):
        humanizer._last_send_time.clear()

    def _long_reply(self, chars=900):
        return ("今天聊聊这个话题的来龙去脉。" * 100)[:chars]

    def test_long_reply_goes_forward(self):
        bot = _FakeBot(forward_ok=True)
        out = _run(humanizer.send_human_like(
            bot, 321, self._long_reply(), started_at=time.time() - 100))
        self.assertTrue(out)
        self.assertEqual(len(bot.forward_calls), 1)
        self.assertEqual(bot.msg_calls, [])  # 不该逐条刷屏

    def test_forward_failure_falls_back_to_bubbles(self):
        bot = _FakeBot(forward_ok=False)
        # started_at 设为很久前 → 拟人延迟为 0，测试快
        out = _run(humanizer.send_human_like(
            bot, 322, self._long_reply(600), started_at=time.time() - 1000))
        self.assertTrue(out)
        self.assertGreater(len(bot.msg_calls), 0)  # 转发失败 → 降级逐条发

    def test_short_reply_not_forwarded(self):
        bot = _FakeBot(forward_ok=True)
        _run(humanizer.send_human_like(
            bot, 323, "哈哈哈 这句话有点意思", started_at=time.time() - 100))
        self.assertEqual(bot.forward_calls, [])
        self.assertEqual(len(bot.msg_calls), 1)

    def test_private_never_forwarded(self):
        bot = _FakeBot(forward_ok=True)
        _run(humanizer.send_human_like(
            bot, 0, self._long_reply(), private_user_id=10001,
            started_at=time.time() - 100))
        self.assertEqual(bot.forward_calls, [])
        self.assertGreater(len(bot.msg_calls), 0)


if __name__ == "__main__":
    unittest.main()
