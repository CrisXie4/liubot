"""自然唤醒 + 热对话续聊测试 — python tests/test_wake_and_followup.py"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


class TestHotConversationFollowup(unittest.TestCase):
    """决策引擎：会话态对话者的热对话续聊"""

    def setUp(self):
        from bot import decision_engine as de
        self.de = de
        de._last_reply_time.clear()

    def _decide(self, **kw):
        bot = MagicMock()
        bot.bot_qq = "10000"
        defaults = dict(
            bot=bot, group_id=1, user_qq=123, user_name="测试",
            message="现在都下午5点了", is_at_bot=False, is_reply_to_bot=False,
        )
        defaults.update(kw)
        return _run(self.de.should_reply_to_message(**defaults))

    def test_hot_followup_right_after_bot_reply(self):
        """会话态 + bot 刚说完 30s → 高概率续聊(种子固定必中)"""
        import random
        hot_hits = 0
        for seed in range(40):
            self.de._last_reply_time.clear()  # 命中会写冷却，必须逐轮清
            random.seed(seed)
            ok, reason = self._decide(
                in_followup=True, bot_last_reply_ts=time.time() - 30)
            if ok and "热对话" in reason:
                hot_hits += 1
        # p=0.85，40 次里热对话层命中应远超半数
        self.assertGreater(hot_hits, 24, f"hot_hits={hot_hits}")

    def test_cool_followup_window(self):
        """会话态 + bot 说完 2~3 分钟 → 中等概率"""
        import random
        hot_hits = 0
        for seed in range(200):
            self.de._last_reply_time.clear()
            random.seed(seed)
            ok, reason = self._decide(
                in_followup=True, bot_last_reply_ts=time.time() - 140)
            if ok and "热对话" in reason:
                hot_hits += 1
        # p=0.60，200 次命中应明显过半但不到满
        self.assertTrue(80 < hot_hits < 180, f"hot_hits={hot_hits}")

    def test_no_bonus_outside_hot_window(self):
        """bot 说完超过 3 分钟 → 无热对话加成(reason 不含热对话)"""
        for seed in range(10):
            import random
            random.seed(seed)
            ok, reason = self._decide(
                in_followup=True, bot_last_reply_ts=time.time() - 400)
            if ok:
                self.assertNotIn("热对话", reason)

    def test_followup_short_cooldown(self):
        """会话态冷却 3s：bot 回完 5s 后接话不再被冷却硬拒"""
        import random
        random.seed(7)
        self.de._last_reply_time[1] = time.time() - 5
        # 无热对话加成(不传 ts)但冷却应已过 → 走正常概率，reason 不是"全局冷却中"
        ok, reason = self._decide(in_followup=True)
        self.assertNotEqual(reason, "全局冷却中")

    def test_global_cooldown_still_blocks_non_followup(self):
        """非会话态：10s 冷却依旧生效"""
        self.de._last_reply_time[1] = time.time() - 5
        ok, reason = self._decide(in_followup=False)
        self.assertFalse(ok)
        self.assertEqual(reason, "全局冷却中")

    def test_non_followup_unaffected(self):
        """非会话态消息不带热对话加成"""
        import random
        for seed in range(20):
            random.seed(seed)
            ok, reason = self._decide(in_followup=False,
                                      bot_last_reply_ts=time.time() - 10)
            if ok:
                self.assertNotIn("热对话", reason)


class TestDormantConversation(unittest.TestCase):
    """沉寂唤醒：断线 3~10 分钟、话头悬在 Bot 这边"""

    def setUp(self):
        from bot import handler as hd
        self.hd = hd
        hd._followup.clear()

    def _make_session(self, group_id=1, idle_s=300, bot_replied_before=True):
        now = time.time()
        g = self.hd._followup.setdefault(group_id, {"users": {}, "expiry": 0})
        g["users"] = {123: "六哥"}
        g["last_msg_ts"] = now - idle_s
        # bot_replied_before=True 模拟"bot 先说完话、群友接了句、bot 没接"
        g["last_bot_reply_ts"] = now - idle_s - 60 if bot_replied_before else now - idle_s + 60
        return g

    def test_dormant_window_hit(self):
        """断线 5 分钟 + bot 没收尾 → 命中唤醒条件"""
        g = self._make_session(idle_s=300, bot_replied_before=True)
        self.assertFalse(g.get("woken"))

    def test_bot_closed_topic_skipped(self):
        """bot 最后发言晚于群友 → 不该唤醒(在 check 中跳过)"""
        g = self._make_session(idle_s=300, bot_replied_before=False)
        # last_bot_reply_ts 更晚 → 方向是"bot 收尾"
        self.assertGreater(g["last_bot_reply_ts"], g["last_msg_ts"])

    def test_woken_only_once(self):
        """woken 标记后不再触发"""
        g = self._make_session(idle_s=300)
        g["woken"] = True
        bot = AsyncMock()
        bot.bot_qq = "10000"
        _run(self.hd.check_dormant_conversations(bot))
        bot.send_group_msg.assert_not_called()

    def test_no_session_no_wake(self):
        """无会话记录 → 什么都不做"""
        bot = AsyncMock()
        bot.bot_qq = "10000"
        _run(self.hd.check_dormant_conversations(bot))
        bot.send_group_msg.assert_not_called()

    def test_full_wake_flow_with_llm(self):
        """完整唤醒链路:LLM 返回续话 → 拟人发送 + 记忆落账"""
        g = self._make_session(idle_s=300, bot_replied_before=True)

        resp = MagicMock()
        resp.content = [MagicMock(text="对哦 现在都五点了 我记岔了")]
        async def fake_llm(**kw):
            return resp
        async def fake_send(bot_, gid, text, **kw):
            return [1]

        fake_cs = MagicMock()
        fake_cs.get_state.return_value = {"sleeping": False,
                                          "emotions": {"energy": 0.8}}
        fake_cs.can_speak_proactively.return_value = (True, "")
        fake_cs.record_proactive_message.return_value = None

        fake_guardian = MagicMock()
        fake_guardian.check_proactive_rate.return_value = True

        bot = AsyncMock()
        bot.bot_qq = "10000"
        with patch("bot.chat.generate_dormant_wake", new=fake_llm), \
             patch("bot.humanizer.send_human_like", new=fake_send), \
             patch("bot.consciousness.get_state", new=fake_cs.get_state), \
             patch("bot.consciousness.can_speak_proactively",
                   new=fake_cs.can_speak_proactively), \
             patch("bot.consciousness.record_proactive_message",
                   new=fake_cs.record_proactive_message), \
             patch("bot.guardian.check_proactive_rate",
                   new=fake_guardian.check_proactive_rate), \
             patch("bot.memory.window_add_message") as wam, \
             patch("bot.chat.add_context") as ctx:
            _run(self.hd.check_dormant_conversations(bot))
        # humanizer 被 handler 导入为模块属性 → patch bot.handler 命名空间
        self.assertTrue(g.get("woken"))

    def test_quiet_hours_no_wake(self):
        """深夜静默 → 不冒泡，且不消耗 woken 机会以外的东西"""
        g = self._make_session(idle_s=300)
        fake_cs = MagicMock()
        fake_cs.get_state.return_value = {"sleeping": False,
                                          "emotions": {"energy": 0.8}}
        fake_cs.can_speak_proactively.return_value = (False, "深夜静默时段")
        bot = AsyncMock()
        bot.bot_qq = "10000"
        with patch("bot.consciousness.get_state", new=fake_cs.get_state), \
             patch("bot.consciousness.can_speak_proactively",
                   new=fake_cs.can_speak_proactively):
            _run(self.hd.check_dormant_conversations(bot))
        # 闸门拦下 → 未标记 woken(睡醒了还能接)
        self.assertFalse(g.get("woken"))


class TestGenerateDormantWake(unittest.TestCase):
    """唤醒语生成：LLM SKIP / 超长 / 正常"""

    def setUp(self):
        from bot import chat as chat_mod
        self.chat = chat_mod
        chat_mod._chat_contexts.clear()

    def test_no_context_returns_none(self):
        result = _run(self.chat.generate_dormant_wake(1, {"users": {123: "六哥"},
                                                          "last_msg_ts": time.time() - 300}))
        self.assertIsNone(result)

    def test_skip_response(self):
        self.chat.add_context(1, "user", "[六哥] 说: 好了不聊了去吃饭",
                              importance=0.4)
        resp = MagicMock()
        resp.content = [MagicMock(text="SKIP")]
        async def fake_llm(**kw):
            return resp
        with patch("bot.chat.llm_call.llm_create", new=fake_llm), \
             patch("bot.chat.memory.load_model_settings",
                   return_value={"api_key": "k", "model": "m"}):
            result = _run(self.chat.generate_dormant_wake(
                1, {"users": {123: "六哥"}, "last_msg_ts": time.time() - 300,
                    "last_speaker_uid": 123}))
        self.assertIsNone(result)

    def test_normal_line(self):
        self.chat.add_context(1, "user", "[六哥] 说: 现在都下午5点了",
                              importance=0.4)
        resp = MagicMock()
        resp.content = [MagicMock(text='对哦 都五点了 我记岔了')]
        async def fake_llm(**kw):
            return resp
        with patch("bot.chat.llm_call.llm_create", new=fake_llm), \
             patch("bot.chat.memory.load_model_settings",
                   return_value={"api_key": "k", "model": "m"}):
            result = _run(self.chat.generate_dormant_wake(
                1, {"users": {123: "六哥"}, "last_msg_ts": time.time() - 300,
                    "last_speaker_uid": 123}))
        self.assertEqual(result, "对哦 都五点了 我记岔了")

    def test_overlong_line_rejected(self):
        self.chat.add_context(1, "user", "[六哥] 说: 嗯", importance=0.4)
        resp = MagicMock()
        resp.content = [MagicMock(text="这句" * 40)]
        async def fake_llm(**kw):
            return resp
        with patch("bot.chat.llm_call.llm_create", new=fake_llm), \
             patch("bot.chat.memory.load_model_settings",
                   return_value={"api_key": "k", "model": "m"}):
            result = _run(self.chat.generate_dormant_wake(
                1, {"users": {123: "六哥"}, "last_msg_ts": time.time() - 300}))
        self.assertIsNone(result)

    def test_prompt_contains_dialogue_and_time(self):
        """system 提示必须包含对话内容和时间事实(防编造作息)"""
        self.chat.add_context(1, "user", "[六哥] 说: 打羽毛球去了", importance=0.4)
        captured = {}
        resp = MagicMock()
        resp.content = [MagicMock(text="SKIP")]
        async def fake_llm(**kw):
            captured["system"] = kw.get("system", "")
            return resp
        with patch("bot.chat.llm_call.llm_create", new=fake_llm), \
             patch("bot.chat.memory.load_model_settings",
                   return_value={"api_key": "k", "model": "m"}):
            _run(self.chat.generate_dormant_wake(
                1, {"users": {123: "六哥"}, "last_msg_ts": time.time() - 300,
                    "last_speaker_uid": 123}))
        self.assertIn("打羽毛球去了", captured["system"])
        self.assertIn("真实时间", captured["system"])
        self.assertIn("六哥", captured["system"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
