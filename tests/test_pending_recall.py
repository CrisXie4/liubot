"""待验证成员消息追踪/离场撤回测试 — python tests/test_pending_recall.py

回归背景（2026-10-09 反馈）：广告机器人进群刷广告 → 不答验证被踢/自己退群，
发过的广告永远留在群里，广告目的照样达成。现在待验证期间的消息全部记下
message_id，离场（答错踢/超时踢/被管理员踢/主动退群）时统一撤回。
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


GID, UID = 777001, 777002  # 独立群号/用户号，避免和其他测试串台


class TestPendingRecall(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._pending.clear()
        verify._web_pending.clear()
        verify._pending_msgs.clear()
        self._orig_timeout = verify.VERIFY_TIMEOUT
        verify.VERIFY_TIMEOUT = 0.01  # 超时任务直接秒触发
        import tempfile
        self._orig_hist = verify._HISTORY_FILE
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.close()
        verify._HISTORY_FILE = tmp.name
        self.tmp_path = tmp.name

    def tearDown(self):
        import os
        self.verify.VERIFY_TIMEOUT = self._orig_timeout
        self.verify._HISTORY_FILE = self._orig_hist
        try:
            os.unlink(self.tmp_path)
        except Exception:
            pass

    def _patch_send(self):
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        return patch('bot.humanizer.send_human_like', new=fake_send)

    def _make_pending(self, wrong=0):
        self.verify._pending.setdefault(GID, {})[UID] = {
            "question": "1 + 1 等于几？", "answer": "2",
            "user_name": "tester", "created_at": time.time(),
            "wrong_attempts": wrong,
        }

    def _make_bot(self):
        bot = AsyncMock()
        bot.bot_qq = 999
        bot._call = AsyncMock(return_value={"status": "ok"})
        return bot

    # ── 追踪器本体 ──

    def test_track_cap(self):
        """追踪列表有上限，超出丢最旧的"""
        for i in range(self.verify._PENDING_MSG_KEEP + 10):
            self.verify._track_pending_message(GID, UID, i)
        ids = self.verify._pending_msgs[GID][UID]
        self.assertEqual(len(ids), self.verify._PENDING_MSG_KEEP)
        self.assertNotIn(0, ids)
        self.assertIn(self.verify._PENDING_MSG_KEEP + 9, ids)

    def test_recall_empty_noop(self):
        """没追踪过任何消息 → 撤回是空操作"""
        bot = self._make_bot()
        n = _run(self.verify.recall_pending_messages(bot, GID, 883311))
        self.assertEqual(n, 0)
        bot.delete_msg.assert_not_awaited()

    def test_recall_deletes_all_and_forgets(self):
        """撤回删掉全部追踪消息并停止追踪"""
        self.verify._track_pending_message(GID, UID, 111)
        self.verify._track_pending_message(GID, UID, 222)
        bot = self._make_bot()
        n = _run(self.verify.recall_pending_messages(bot, GID, UID, reason="测试"))
        self.assertEqual(n, 2)
        deleted = [c.args[0] for c in bot.delete_msg.await_args_list]
        self.assertIn(111, deleted)
        self.assertIn(222, deleted)
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))

    def test_recall_swallows_delete_failure(self):
        """单条撤回失败不中断，能撤几条撤几条"""
        self.verify._track_pending_message(GID, UID, 111)
        self.verify._track_pending_message(GID, UID, 222)
        bot = self._make_bot()

        async def flaky(mid):
            if mid == 111:
                raise RuntimeError("already gone")
            return True

        bot.delete_msg = AsyncMock(side_effect=flaky)
        n = _run(self.verify.recall_pending_messages(bot, GID, UID))
        self.assertEqual(n, 1)
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))

    # ── 出题模式 ──

    def test_answer_message_gets_tracked(self):
        """待验证成员发的每条消息（含被判'非答题'的）都自动记下 id"""
        self._make_pending()
        bot = self._make_bot()

        async def fake_judge(q, e, a):
            return ("invalid", "不是答题")

        with patch.object(self.verify, '_judge_answer', new=fake_judge), \
             self._patch_send():
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "朋友们看过来 http://ad.example.com",
                "message_id": 777,
            }))
        self.assertTrue(handled)
        self.assertIn(777, self.verify._pending_msgs[GID][UID])
        bot.delete_msg.assert_not_awaited()  # 没离场，不撤

    def test_final_wrong_recalls_and_kicks(self):
        """答错满次数 → 先撤回全部消息再踢人"""
        self._make_pending(wrong=self.verify.MAX_WRONG_ATTEMPTS - 1)
        self.verify._track_pending_message(GID, UID, 444)
        bot = self._make_bot()

        async def fake_judge(q, e, a):
            return ("wrong", "答错了")

        with patch.object(self.verify, '_judge_answer', new=fake_judge), \
             self._patch_send():
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "瞎答", "message_id": 445,
            }))
        self.assertTrue(handled)
        deleted = [c.args[0] for c in bot.delete_msg.await_args_list]
        self.assertIn(444, deleted)
        self.assertIn(445, deleted)  # 本次踢人前的最后一条也被撤
        kicks = [c for c in bot._call.await_args_list if c.args[0] == "set_group_kick"]
        self.assertEqual(len(kicks), 1)
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))

    def test_pass_keeps_messages(self):
        """答对通过 → 之前的消息不撤，停止追踪"""
        self._make_pending()
        self.verify._track_pending_message(GID, UID, 333)
        bot = self._make_bot()

        async def fake_judge(q, e, a):
            return ("correct", "对的")

        with patch.object(self.verify, '_judge_answer', new=fake_judge), \
             self._patch_send(), \
             patch('bot.welcome.get_welcome_message', return_value=None), \
             patch('bot.welcome.add_points', return_value=None):
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "2", "message_id": 334,
            }))
        self.assertTrue(handled)
        bot.delete_msg.assert_not_awaited()
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))

    def test_timeout_task_recalls(self):
        """超时任务：撤回 + 踢人"""
        self._make_pending()
        self.verify._track_pending_message(GID, UID, 555)
        bot = self._make_bot()
        with self._patch_send():
            _run(self.verify._timeout_kick(bot, GID, UID, "tester"))
        deleted = [c.args[0] for c in bot.delete_msg.await_args_list]
        self.assertIn(555, deleted)
        kicks = [c for c in bot._call.await_args_list if c.args[0] == "set_group_kick"]
        self.assertEqual(len(kicks), 1)

    # ── 网站模式 ──

    def _make_web_pending(self, wrong):
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(GID, UID, "alice"))
        self.verify._web_pending[token]["wrong_attempts"] = wrong
        return token

    def test_web_final_wrong_recalls_and_kicks(self):
        """网站模式答错满 3 次 → 撤回 + 踢人"""
        self._make_web_pending(wrong=self.verify.MAX_WRONG_ATTEMPTS_WEB - 1)
        self.verify._track_pending_message(GID, UID, 666)
        bot = self._make_bot()
        with self._patch_send():
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "又一个错词", "message_id": 667,
            }))
        self.assertTrue(handled)
        deleted = [c.args[0] for c in bot.delete_msg.await_args_list]
        self.assertIn(666, deleted)
        self.assertIn(667, deleted)
        kicks = [c for c in bot._call.await_args_list if c.args[0] == "set_group_kick"]
        self.assertEqual(len(kicks), 1)

    def test_web_pass_keeps_messages(self):
        """网站模式答对 → 消息保留，停止追踪"""
        self._make_web_pending(wrong=0)
        self.verify._track_pending_message(GID, UID, 777)
        bot = self._make_bot()
        with self._patch_send(), \
             patch('bot.welcome.get_welcome_message', return_value=None), \
             patch('bot.welcome.add_points', return_value=None):
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "星空", "message_id": 778,
            }))
        self.assertTrue(handled)
        bot.delete_msg.assert_not_awaited()
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))

    def test_web_expiry_fallback_recalls(self):
        """网站模式过期兜底（超时任务没接住）→ 撤回 + 踢人"""
        token = self._make_web_pending(wrong=0)
        self.verify._web_pending[token]["created_at"] = time.time() - 9999
        self.verify._track_pending_message(GID, UID, 888)
        bot = self._make_bot()
        with self._patch_send():
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": GID, "user_id": UID,
                "raw_message": "迟到的话", "message_id": 889,
            }))
        self.assertTrue(handled)
        deleted = [c.args[0] for c in bot.delete_msg.await_args_list]
        self.assertIn(888, deleted)
        kicks = [c for c in bot._call.await_args_list if c.args[0] == "set_group_kick"]
        self.assertEqual(len(kicks), 1)

    # ── 主人放行 ──

    def test_manual_approve_keeps_messages(self):
        """主人放行 → 消息保留，停止追踪"""
        self._make_pending()
        self.verify._track_pending_message(GID, UID, 999)
        bot = self._make_bot()
        with self._patch_send(), \
             patch('bot.welcome.get_welcome_message', return_value=None), \
             patch('bot.welcome.add_points', return_value=None):
            ok = _run(self.verify.approve_pending(bot, GID, UID))
        self.assertTrue(ok)
        bot.delete_msg.assert_not_awaited()
        self.assertNotIn(UID, self.verify._pending_msgs.get(GID, {}))


if __name__ == "__main__":
    unittest.main()
