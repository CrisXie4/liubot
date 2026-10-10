"""验证题等系统关键消息不允许被群级发送冷却静默丢弃。

回归背景（2026-09-13 事故）：同群内一条普通消息（打算跟进/自主回复）
发出后 5 秒内新人进群，verify 的验证题被 humanizer 冷却静默吞掉，
verify 又不检查返回值照记"已出题"，5 分钟后无题踢人，连踢两个。
"""
import sys
import os
import time
import asyncio
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import humanizer
from bot import verify as verify_mod

GID = 1084012604
QUESTION_MSG = (
    "[CQ:at,qq=999] 欢迎入群~\n"
    "请回答验证题（5分钟内）：\n\n"
    "📝 6+7等于多少？\n\n"
    "答对留下 :96:\n"
    "答错踢出 :178:"
)


class _FakeBot:
    def __init__(self, send_ok=True):
        self.bot_qq = 3685789505
        self.send_ok = send_ok
        self.msg_calls = []
        self.kick_calls = []

    async def send_group_msg(self, group_id, message):
        if not self.send_ok:
            return None
        self.msg_calls.append((group_id, str(message)))
        return 6789

    async def send_private_msg(self, user_id, message):
        self.msg_calls.append((user_id, str(message)[:30]))
        return 6789

    async def delete_msg(self, message_id):
        return True

    async def send_group_forward_msg(self, group_id, messages):
        return None

    async def get_group_member_info(self, group_id, user_id):
        return {"card": "新人", "nickname": "新人"}

    async def _call(self, action, params):
        self.kick_calls.append(action)
        return {}


def _no_delay():
    """把拟人打字延迟全部归零，测试瞬时完成且确定性。"""
    return [
        patch.object(humanizer, "TYPING_CPS", (1000.0, 1000.0)),
        patch.object(humanizer, "FIRST_REACT", (0.0, 0.0)),
        patch.object(humanizer, "BUBBLE_GAP", (0.0, 0.0)),
        patch.object(humanizer, "HESITATE_CHANCE", 0.0),
        patch.object(humanizer, "LONG_HESITATE_CHANCE", 0.0),
        patch.object(humanizer, "TYPO_CHANCE", 0.0),
    ]


class TestCooldownDrop(unittest.TestCase):
    def setUp(self):
        humanizer._last_send_time.clear()
        self._patches = _no_delay()
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_normal_message_dropped_within_cooldown(self):
        """事故原样复刻：4 秒前发过消息，普通消息整条被吞。"""
        bot = _FakeBot()
        humanizer._last_send_time[GID] = time.time() - 4.0
        r = asyncio.run(humanizer.send_human_like(bot, GID, "Cris谢，昨天那编程问题解决没？"))
        self.assertEqual(r, [])
        self.assertEqual(bot.msg_calls, [])
        # 丢弃必须对 handler 可见（原来只记 debug 日志）
        self.assertIsNotNone(humanizer.last_send_failure)
        self.assertEqual(humanizer.last_send_failure["total"], 1)

    def test_priority_message_bypasses_cooldown(self):
        """priority=True（验证题）：同窗口内不丢弃、照常发出。"""
        bot = _FakeBot()
        humanizer._last_send_time[GID] = time.time() - 4.0
        r = asyncio.run(humanizer.send_human_like(bot, GID, QUESTION_MSG, priority=True))
        self.assertTrue(r)
        self.assertTrue(bot.msg_calls)

    def test_last_send_failure_reset_on_success(self):
        """成功的调用要清掉上一轮的失败记录，防 handler 误归因。"""
        bot = _FakeBot()
        humanizer._last_send_time[GID] = time.time() - 4.0
        asyncio.run(humanizer.send_human_like(bot, GID, QUESTION_MSG, priority=True))
        self.assertIsNone(humanizer.last_send_failure)


class TestVerifyQuestionFallback(unittest.TestCase):
    """verify 层兜底：拟人发送整条没出去时裸发，绝不无声放弃。"""

    def setUp(self):
        verify_mod._pending.clear()
        verify_mod._web_pending.clear()
        self._patches = _no_delay()
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _event(self):
        return {"group_id": GID, "user_id": 999, "sub_type": "approve"}

    def _patches_for_verify(self, humanizer_send):
        async def _gen_question():
            return ("6+7等于多少？", "13")
        return [
            patch("bot.humanizer.send_human_like", new=humanizer_send),
            patch.object(verify_mod, "is_whitelisted", return_value=False),
            patch.object(verify_mod, "_pick_web_mode", return_value=False),
            patch.object(verify_mod, "_generate_question", new=_gen_question),
        ]

    def test_question_bare_send_fallback_when_dropped(self):
        """拟人发送被冷却吞掉（返回空）→ verify 裸发兜底，题一定要出去。"""
        async def _dropped(*a, **k):
            return []
        bot = _FakeBot()
        patches = self._patches_for_verify(_dropped)
        for p in patches:
            p.start()
        try:
            took_over = asyncio.run(verify_mod.handle_member_increase(bot, self._event()))
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(took_over)
        self.assertTrue(bot.msg_calls, "验证题必须经裸发兜底发出去")
        self.assertEqual(bot.kick_calls, [])

    def test_no_kick_task_when_all_sends_fail(self):
        """拟人+裸发全失败 → 不启动超时踢人（宁可放过不可错杀）。"""
        async def _dropped(*a, **k):
            return []
        bot = _FakeBot(send_ok=False)
        patches = self._patches_for_verify(_dropped)
        for p in patches:
            p.start()
        try:
            took_over = asyncio.run(verify_mod.handle_member_increase(bot, self._event()))
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(took_over)
        self.assertEqual(bot.msg_calls, [])
        self.assertEqual(bot.kick_calls, [])
        self.assertNotIn(999, verify_mod._pending.get(GID, {}), "全失败必须清 pending，不留超时踢人钩子")

    def test_no_double_send_when_humanizer_succeeds(self):
        """拟人发送正常时不得再裸发一遍（防双重出题）。"""
        async def _ok(bot, group_id, text, **k):
            return ["bubble1"]
        bot = _FakeBot()
        patches = self._patches_for_verify(_ok)
        for p in patches:
            p.start()
        try:
            asyncio.run(verify_mod.handle_member_increase(bot, self._event()))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(bot.msg_calls, [], "拟人发送成功就不再裸发")
        self.assertIn(999, verify_mod._pending.get(GID, {}), "正常出题要挂上待验证")


class TestSingleLineQuestion(unittest.TestCase):
    """09-25 反馈：多行出题文案被 humanizer 拆成 4 条连发=刷屏。

    进群只该一行过去：@新人 欢迎入群~ 请回答验证题（5分钟内）：xxx
    """

    def setUp(self):
        verify_mod._pending.clear()
        verify_mod._web_pending.clear()
        verify_mod._recent_joins.clear()
        self._patches = _no_delay()
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _event(self):
        return {"group_id": GID, "user_id": 999, "sub_type": "approve"}

    def _run_quiz(self, bot):
        async def _gen_question():
            return ("6+7等于多少？", "13")
        patches = [
            patch.object(verify_mod, "is_whitelisted", return_value=False),
            patch.object(verify_mod, "_pick_web_mode", return_value=False),
            patch.object(verify_mod, "_generate_question", new=_gen_question),
        ]
        for p in patches:
            p.start()
        try:
            return asyncio.run(verify_mod.handle_member_increase(bot, self._event()))
        finally:
            for p in patches:
                p.stop()

    def test_question_is_one_bubble_one_line(self):
        """出题消息必须只发一条气泡，且整条不含换行（换行=会被拆条）。"""
        bot = _FakeBot()
        self.assertTrue(self._run_quiz(bot))
        self.assertEqual(len(bot.msg_calls), 1, f"出题只该发一条，实际{len(bot.msg_calls)}条")
        _, text = bot.msg_calls[0]
        self.assertNotIn("\n", text, "出题消息不允许换行")
        self.assertIn("6+7等于多少？", text)
        self.assertIn("欢迎入群", text)
        self.assertIn("请回答验证题（5分钟内）", text)

    def test_recent_join_registered_for_avoidance(self):
        """出题后要登记最近入群，供决策引擎避让 AI 插话欢迎。"""
        bot = _FakeBot()
        self.assertTrue(self._run_quiz(bot))
        joins = verify_mod.get_recent_joins(GID)
        self.assertEqual([(999, "新人")], joins)


if __name__ == "__main__":
    unittest.main()
