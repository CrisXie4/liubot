"""网站认证模式测试 — python tests/test_web_verify.py"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


class TestWebVerifyUtils(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._used_web_codes.clear()

    def test_normalize_code_strips_spaces_punct(self):
        """normalize 去空格、标点、lower"""
        self.assertEqual(self.verify._normalize_code(" 星 空 "), "星空")
        self.assertEqual(self.verify._normalize_code("星，空！"), "星空")
        self.assertEqual(self.verify._normalize_code("Star"), "star")

    def test_pick_web_mode_ratio_0(self):
        """ratio=0 → 永不命中"""
        with patch.object(self.verify, '_get_web_ratio', return_value=0):
            for _ in range(20):
                self.assertFalse(self.verify._pick_web_mode())

    def test_pick_web_mode_ratio_100(self):
        """ratio=100 → 必命中"""
        with patch.object(self.verify, '_get_web_ratio', return_value=100):
            for _ in range(20):
                self.assertTrue(self.verify._pick_web_mode())

    def test_pick_web_mode_ratio_distribution(self):
        """ratio=30 → 大致 30% 命中（统计检验）"""
        with patch.object(self.verify, '_get_web_ratio', return_value=30):
            hits = sum(1 for _ in range(1000) if self.verify._pick_web_mode())
        self.assertTrue(200 < hits < 400, f"hits={hits} 偏离 30% 太多")

    def test_generate_web_code_fallback_on_llm_fail(self):
        """LLM 失败 → 走内置兜底词库"""
        async def fake_call(*a, **kw):
            raise Exception("simulated LLM fail")
        with patch('bot.verify.llm_call.llm_create', new=fake_call):
            code = _run(self.verify._generate_web_code())
        self.assertIn(code, self.verify._WEB_CODE_FALLBACK)

    def test_generate_web_code_parses_llm_json(self):
        """LLM 返回合法 JSON → 取 code 字段"""
        resp = MagicMock()
        resp.content = [MagicMock(text='{"code": "星空"}')]
        async def fake_call(*a, **kw):
            return resp
        with patch('bot.verify.llm_call.llm_create', new=fake_call):
            code = _run(self.verify._generate_web_code())
        self.assertEqual(code, "星空")

    def test_generate_web_code_dedup_against_recent(self):
        """最近用过的词会避开"""
        self.verify._used_web_codes.add("星空")
        resp = MagicMock()
        resp.content = [MagicMock(text='{"code": "晚风"}')]
        async def fake_call(*a, **kw):
            return resp
        with patch('bot.verify.llm_call.llm_create', new=fake_call):
            code = _run(self.verify._generate_web_code())
        self.assertEqual(code, "晚风")
        self.assertIn("晚风", self.verify._used_web_codes)


class TestWebChallengeLifecycle(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._used_web_codes.clear()

    def test_create_returns_unique_token(self):
        """create 两次拿到不同 token"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            t1 = _run(self.verify.create_web_challenge(100, 1001, "alice"))
            t2 = _run(self.verify.create_web_challenge(100, 1002, "bob"))
        self.assertNotEqual(t1, t2)
        self.assertEqual(len(t1), 22)
        self.assertEqual(self.verify._web_pending[t1]["code"], "星空")
        self.assertEqual(self.verify._web_pending[t1]["user_id"], 1001)
        self.assertFalse(self.verify._web_pending[t1]["revealed"])

    def test_reveal_unknown_token(self):
        """未知 token → ok=False"""
        r = _run(self.verify.reveal_web_code("nonexistent", "cf"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "token_not_found")

    def test_reveal_expired_token(self):
        """过期 token → ok=False 且清理"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
        # 模拟过期
        self.verify._web_pending[token]["created_at"] = 0
        r = _run(self.verify.reveal_web_code(token, "cf"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "token_expired")
        self.assertNotIn(token, self.verify._web_pending)

    def test_reveal_success_without_secret(self):
        """secret 未配置 → 降级直接返回词"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch('bot.verify.config') as mock_cfg:
            mock_cfg.TURNSTILE_SECRET_KEY = ""
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
            r = _run(self.verify.reveal_web_code(token, "cf"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["code"], "星空")
        self.assertTrue(self.verify._web_pending[token]["revealed"])

    def test_reveal_turnstile_fail(self):
        """Turnstile 校验失败 → ok=False"""
        async def fake_gen():
            return "星空"
        async def fake_verify(*a, **kw):
            return False
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch('bot.verify.config') as mock_cfg, \
             patch.object(self.verify, '_verify_turnstile', new=fake_verify):
            mock_cfg.TURNSTILE_SECRET_KEY = "secret"
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
            r = _run(self.verify.reveal_web_code(token, "cf"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "captcha_failed")

    def test_consume_correct(self):
        """答对 → verdict=correct"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
        r = _run(self.verify.consume_web_challenge(100, 1001, "星空"))
        self.assertEqual(r["verdict"], "correct")
        # 通过后 token 被清理
        self.assertNotIn(token, self.verify._web_pending)

    def test_consume_correct_with_spaces(self):
        """normalize 后匹配"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        r = _run(self.verify.consume_web_challenge(100, 1001, " 星 空 "))
        self.assertEqual(r["verdict"], "correct")

    def test_consume_wrong_increments_attempts(self):
        """答错 → attempts+1，未达上限不踢"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        r1 = _run(self.verify.consume_web_challenge(100, 1001, "错词"))
        self.assertEqual(r1["verdict"], "wrong")
        self.assertEqual(r1["attempts"], 1)
        self.assertEqual(r1["max"], 3)
        r2 = _run(self.verify.consume_web_challenge(100, 1001, "再错"))
        self.assertEqual(r2["attempts"], 2)
        # 第 2 次错，还没到 3，token 还在
        self.assertEqual(len(self.verify._web_pending), 1)

    def test_consume_wrong_max_kicks(self):
        """错满 3 次 → verdict=wrong 且标记 final"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        for i in range(3):
            r = _run(self.verify.consume_web_challenge(100, 1001, "错"))
        self.assertEqual(r["verdict"], "wrong")
        self.assertEqual(r["attempts"], 3)
        self.assertTrue(r.get("final"))
        # token 已清理
        self.assertEqual(len(self.verify._web_pending), 0)

    def test_consume_smalltalk_no_attempt(self):
        """寒暄 → smalltalk，不消耗次数"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        r = _run(self.verify.consume_web_challenge(100, 1001, "你好"))
        self.assertEqual(r["verdict"], "smalltalk")
        # 没消耗次数
        self.assertEqual(self.verify._web_pending[list(self.verify._web_pending)[0]]["wrong_attempts"], 0)

    def test_consume_not_found(self):
        """没在 pending → not_found"""
        r = _run(self.verify.consume_web_challenge(999, 9999, "星空"))
        self.assertEqual(r["verdict"], "not_found")

    def test_cleanup_user_deletes_token(self):
        """cleanup 按 (group, user) 删 token"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
        self.verify._cleanup_web_user(100, 1001)
        self.assertNotIn(token, self.verify._web_pending)


class TestLogVerificationFields(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        import tempfile
        self._orig = verify._HISTORY_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._HISTORY_FILE = self.tmp.name

    def tearDown(self):
        import os
        self.verify._HISTORY_FILE = self._orig
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_log_with_method_attempts(self):
        """新参数写入历史 entry"""
        self.verify._log_verification(
            123, "alice", 456, "approved_web", "星空", "星空", "ok",
            method="web", attempts=1,
        )
        items = self.verify.get_verification_history()
        self.assertEqual(items[0]["method"], "web")
        self.assertEqual(items[0]["attempts"], 1)

    def test_log_backward_compat(self):
        """老调用（不传新参数）依然能写，字段带默认值"""
        self.verify._log_verification(
            123, "alice", 456, "approved", "1+1=?", "2", "ok",
        )
        items = self.verify.get_verification_history()
        self.assertEqual(items[0]["method"], "quiz")
        self.assertEqual(items[0]["attempts"], 1)


class TestMemberIncreaseDispatch(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._pending.clear()
        verify._used_web_codes.clear()
        verify._used_questions.clear()

    def _make_bot(self):
        bot = AsyncMock()
        bot.bot_qq = 999
        bot.get_group_member_info = AsyncMock(return_value={"card": "alice", "nickname": "A"})
        return bot

    def _patch_humanizer(self):
        """mock send_human_like，避免拟人延迟。返回 patcher。"""
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        return patch('bot.verify.send_human_like' if hasattr(self.verify, 'send_human_like')
                     else 'bot.humanizer.send_human_like', new=fake_send)

    def test_dispatch_to_web_mode(self):
        """ratio=100 → 进群走网站模式"""
        bot = self._make_bot()
        async def fake_create(g, u, n):
            import secrets
            t = secrets.token_urlsafe(16)
            self.verify._web_pending[t] = {
                "group_id": g, "user_id": u, "user_name": n,
                "code": "星空", "created_at": __import__('time').time(),
                "wrong_attempts": 0, "revealed": False,
            }
            return t
        # send_human_like 在函数内局部 import，patch bot.humanizer.send_human_like
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        with patch.object(self.verify, '_pick_web_mode', return_value=True), \
             patch.object(self.verify, 'create_web_challenge', new=fake_create), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            _run(self.verify.handle_member_increase(bot, {
                "group_id": 100, "user_id": 1001, "operator_id": 999,
            }))
        self.assertTrue(bot.send_group_msg.called)
        all_msgs = []
        for call in bot.send_group_msg.call_args_list:
            all_msgs.append(call.args[1] if len(call.args) > 1 else call.kwargs.get('message', ''))
        joined = '\n'.join(all_msgs)
        self.assertIn("/v/", joined)
        self.assertEqual(len(self.verify._web_pending), 1)

    def test_dispatch_to_quiz_mode(self):
        """ratio=0 → 进群走出题模式"""
        bot = self._make_bot()
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        with patch.object(self.verify, '_pick_web_mode', return_value=False), \
             patch.object(self.verify, '_generate_question', new=AsyncMock(return_value=("1+1=?", "2"))), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            _run(self.verify.handle_member_increase(bot, {
                "group_id": 100, "user_id": 1001, "operator_id": 999,
            }))
        self.assertIn(1001, self.verify._pending.get(100, {}))
        self.assertEqual(len(self.verify._web_pending), 0)


class TestHandleMemberIncreaseReturnContract(unittest.TestCase):
    """handle_member_increase 必须返回 True 当 verify 接管了入群流程，
    否则 handler.py:402-404 的 if handled: 死代码会导致重复欢迎。
    """

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._pending.clear()
        verify._used_web_codes.clear()

    def _make_bot(self, bot_qq=999):
        bot = MagicMock()
        bot.bot_qq = bot_qq
        bot.send_group_msg = AsyncMock()
        bot.get_group_member_info = AsyncMock(return_value={"card": "", "nickname": "T"})
        return bot

    def test_invalid_event_returns_falsy(self):
        """无效事件 (user_id=0 或 bot 自己) 应返回 falsy，让 handler 走回退欢迎"""
        bot = self._make_bot(bot_qq=1001)
        # user_id == bot_qq → 应早退
        result = _run(self.verify.handle_member_increase(bot, {
            "group_id": 100, "user_id": 1001, "operator_id": 999,
        }))
        self.assertFalse(result, "无效事件应返回 falsy，让 handler 接管")

    def test_quiz_success_returns_true(self):
        """出题模式成功发题应返回 True，阻止 handler 重复发欢迎"""
        bot = self._make_bot()
        async def fake_send(b, g, msg, **kw):
            await b.send_group_msg(g, msg)
        with patch.object(self.verify, '_pick_web_mode', return_value=False), \
             patch.object(self.verify, '_generate_question',
                          new=AsyncMock(return_value=("1+1=?", "2"))), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            result = _run(self.verify.handle_member_increase(bot, {
                "group_id": 100, "user_id": 1001, "operator_id": 999,
            }))
        self.assertTrue(result, "出题成功必须返回 True，否则 handler 会重复发欢迎")
        self.assertIn(1001, self.verify._pending.get(100, {}))

    def test_web_success_returns_true(self):
        """网站模式成功发链接应返回 True"""
        bot = self._make_bot()
        async def fake_create(g, u, n):
            import secrets, time
            t = secrets.token_urlsafe(16)
            self.verify._web_pending[t] = {
                "group_id": g, "user_id": u, "user_name": n,
                "code": "星空", "created_at": time.time(),
                "wrong_attempts": 0, "revealed": False,
            }
            return t
        async def fake_send(b, g, msg, **kw):
            await b.send_group_msg(g, msg)
        with patch.object(self.verify, '_pick_web_mode', return_value=True), \
             patch.object(self.verify, 'create_web_challenge', new=fake_create), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            result = _run(self.verify.handle_member_increase(bot, {
                "group_id": 100, "user_id": 1001, "operator_id": 999,
            }))
        self.assertTrue(result, "网站模式成功必须返回 True")

    def test_whitelist_returns_true(self):
        """白名单放行（已经发了欢迎）应返回 True，阻止 handler 再发一条"""
        bot = self._make_bot()
        async def fake_send(b, g, msg, **kw):
            await b.send_group_msg(g, msg)
        with patch.object(self.verify, 'is_whitelisted', return_value=True), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            result = _run(self.verify.handle_member_increase(bot, {
                "group_id": 100, "user_id": 1001, "operator_id": 999,
            }))
        self.assertTrue(result, "白名单放行必须返回 True，否则 handler 会重复发欢迎")


class TestGroupVerifyAnswerWebBranch(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._pending.clear()
        verify._used_web_codes.clear()
        import tempfile
        self._orig = verify._HISTORY_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._HISTORY_FILE = self.tmp.name

    def tearDown(self):
        import os
        self.verify._HISTORY_FILE = self._orig
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def _make_bot(self):
        bot = AsyncMock()
        bot.bot_qq = 999
        return bot

    def _patch_send(self):
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        return patch('bot.humanizer.send_human_like', new=fake_send)

    def test_web_answer_correct(self):
        """网站模式答对 → 通过，return True"""
        bot = self._make_bot()
        async def fake_gen(): return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             self._patch_send():
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        handled = _run(self.verify.handle_group_verify_answer(bot, {
            "group_id": 100, "user_id": 1001, "raw_message": "星空",
        }))
        self.assertTrue(handled)
        self.assertEqual(len(self.verify._web_pending), 0)

    def test_web_answer_wrong_stays(self):
        """网站模式答错 → 警告，return True，token 还在"""
        bot = self._make_bot()
        async def fake_gen(): return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             self._patch_send():
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
        handled = _run(self.verify.handle_group_verify_answer(bot, {
            "group_id": 100, "user_id": 1001, "raw_message": "错词",
        }))
        self.assertTrue(handled)
        self.assertEqual(len(self.verify._web_pending), 1)

    def test_quiz_mode_unchanged(self):
        """没在 web_pending → 走现有出题流程"""
        bot = self._make_bot()
        handled = _run(self.verify.handle_group_verify_answer(bot, {
            "group_id": 100, "user_id": 1001, "raw_message": "你好",
        }))
        self.assertFalse(handled)


class TestCleanupAndApprove(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._pending.clear()
        verify._used_web_codes.clear()
        import tempfile
        self._orig = verify._HISTORY_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._HISTORY_FILE = self.tmp.name

    def tearDown(self):
        import os
        self.verify._HISTORY_FILE = self._orig
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_cleanup_expired_clears_web_pending(self):
        """cleanup_expired 同时清 _web_pending"""
        async def fake_gen(): return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
        # 模拟过期
        self.verify._web_pending[token]["created_at"] = 0
        self.verify.cleanup_expired()
        self.assertEqual(len(self.verify._web_pending), 0)

    def test_approve_pending_clears_web(self):
        """主人放行 → 同时清 _web_pending"""
        bot = AsyncMock()
        async def fake_gen(): return "星空"
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
            ok = _run(self.verify.approve_pending(bot, 100, 1001))
        self.assertTrue(ok)
        self.assertEqual(len(self.verify._web_pending), 0)


class TestWebVerifyE2E(unittest.TestCase):
    """端到端：创建挑战 → reveal → consume 通过/失败"""

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._pending.clear()
        verify._used_web_codes.clear()
        verify._used_questions.clear()
        import tempfile
        self._orig = verify._HISTORY_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._HISTORY_FILE = self.tmp.name

    def tearDown(self):
        import os
        self.verify._HISTORY_FILE = self._orig
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_full_flow_web_pass(self):
        """完整通过流程：创建 → reveal → consume 答对"""
        async def fake_gen(): return "星空"
        async def fake_turnstile(*a, **kw): return True
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch.object(self.verify, '_verify_turnstile', new=fake_turnstile), \
             patch.object(self.verify, 'config') as mock_cfg:
            mock_cfg.TURNSTILE_SECRET_KEY = ""
            # 1. 创建挑战
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
            # 2. reveal
            r = _run(self.verify.reveal_web_code(token, "cf"))
            self.assertTrue(r["ok"])
            self.assertEqual(r["code"], "星空")
            # 3. consume 答对
            r = _run(self.verify.consume_web_challenge(100, 1001, "星空"))
            self.assertEqual(r["verdict"], "correct")
            # 4. token 已清
            self.assertNotIn(token, self.verify._web_pending)

    def test_full_flow_web_fail_3_times(self):
        """完整失败流程：错 3 次踢"""
        async def fake_gen(): return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch.object(self.verify, 'config') as mock_cfg:
            mock_cfg.TURNSTILE_SECRET_KEY = ""
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))
            for i in range(3):
                r = _run(self.verify.consume_web_challenge(100, 1001, "错"))
            self.assertTrue(r.get("final"))
            self.assertNotIn(token, self.verify._web_pending)

    def test_full_flow_handle_answer_pass(self):
        """完整流程：handle_group_verify_answer 处理通过"""
        bot = AsyncMock()
        bot.bot_qq = 999
        async def fake_gen(): return "星空"
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
            handled = _run(self.verify.handle_group_verify_answer(bot, {
                "group_id": 100, "user_id": 1001, "raw_message": "星空",
            }))
        self.assertTrue(handled)
        self.assertEqual(len(self.verify._web_pending), 0)
        # 历史已记 approved_web
        items = self.verify.get_verification_history()
        self.assertEqual(items[0]["result"], "approved_web")
        self.assertEqual(items[0]["method"], "web")

    def test_full_flow_handle_answer_3_fails_kicks(self):
        """完整流程：handle_group_verify_answer 错 3 次踢出"""
        bot = AsyncMock()
        bot.bot_qq = 999
        async def fake_gen(): return "星空"
        async def fake_send(bot, group_id, msg, **kw):
            await bot.send_group_msg(group_id, msg)
        with patch.object(self.verify, '_generate_web_code', new=fake_gen), \
             patch('bot.humanizer.send_human_like', new=fake_send):
            _run(self.verify.create_web_challenge(100, 1001, "alice"))
            for i in range(3):
                handled = _run(self.verify.handle_group_verify_answer(bot, {
                    "group_id": 100, "user_id": 1001, "raw_message": "错",
                }))
            self.assertTrue(handled)
            # bot.set_group_kick 被调
            bot._call.assert_called()
        # token 已清
        self.assertEqual(len(self.verify._web_pending), 0)
        items = self.verify.get_verification_history()
        self.assertEqual(items[0]["result"], "rejected_web")
        self.assertEqual(items[0]["attempts"], 3)


class TestLocalProof(unittest.TestCase):
    """自托管人机证明校验测试"""

    def setUp(self):
        from bot import verify
        self.verify = verify
        verify._web_pending.clear()
        verify._used_web_codes.clear()

    def _pow(self, token: str, bits: int):
        """枚举找出一个满足 bits 难度的 nonce"""
        import hashlib
        from bot.verify import _count_leading_zero_bits
        for n in range(5_000_000):
            if _count_leading_zero_bits(
                hashlib.sha256(f"{token}:{n}".encode()).digest()
            ) >= bits:
                return n
        raise RuntimeError("找不到 PoW 解")

    def _good_proof(self, token: str, elapsed_ms: int = 2000):
        """构造一个各项指标都正常的 proof"""
        now_ms = int(__import__("time").time() * 1000)
        return {
            "pow": {"nonce": self._pow(token, self.verify.LOCAL_POW_BITS),
                    "bits": self.verify.LOCAL_POW_BITS},
            "ts": now_ms,
            "loadedAt": now_ms - elapsed_ms,
            "events": {"mouseMoves": 15, "mousePath": 220.5,
                       "touchEvents": 0, "keysPressed": 0},
            "env": {"webdriver": False, "pluginCount": 3,
                    "langCount": 2, "screenW": 1920, "screenH": 1080,
                    "tz": "Asia/Shanghai"},
        }

    def test_pow_valid_proof(self):
        """完整合法的证明 → ok=True"""
        token = "validtoken_abc"
        ok, reason = self.verify._verify_local_proof(token, self._good_proof(token))
        self.assertTrue(ok, f"应该通过但失败: {reason}")

    def test_pow_missing(self):
        """缺 PoW → 拒"""
        proof = self._good_proof("validtoken_abc")
        proof["pow"] = {}
        ok, reason = self.verify._verify_local_proof("validtoken_abc", proof)
        self.assertFalse(ok)
        self.assertIn("工作量证明", reason)

    def test_pow_wrong_nonce(self):
        """错误的 nonce → 拒"""
        proof = self._good_proof("validtoken_abc")
        proof["pow"]["nonce"] = 0
        ok, reason = self.verify._verify_local_proof("validtoken_abc", proof)
        self.assertFalse(ok)
        self.assertIn("工作量证明", reason)

    def test_pow_wrong_token(self):
        """nonce 是为别的 token 算的 → 拒"""
        proof = self._good_proof("tokenA")
        ok, reason = self.verify._verify_local_proof("tokenB", proof)
        self.assertFalse(ok)
        self.assertIn("工作量证明", reason)

    def test_too_fast(self):
        """页面加载到提交 < 1.2s → 拒（脚本特征）"""
        token = "fasttoken"
        proof = self._good_proof(token, elapsed_ms=500)
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)
        self.assertIn("过快", reason)

    def test_request_expired(self):
        """ts 与服务器时间偏差过大 → 拒"""
        import time
        token = "oldtoken"
        proof = self._good_proof(token)
        proof["ts"] = int(time.time() * 1000) - 120_000   # 2 分钟前
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_webdriver_detected(self):
        """navigator.webdriver=true → 硬拒"""
        token = "bottoken"
        proof = self._good_proof(token)
        proof["env"]["webdriver"] = True
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)
        self.assertIn("自动化", reason)

    def test_headless_signals(self):
        """无插件 + 无语言 → headless 嫌疑"""
        token = "headless"
        proof = self._good_proof(token)
        proof["env"]["pluginCount"] = 0
        proof["env"]["langCount"] = 0
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)
        self.assertIn("行为", reason)

    def test_no_mouse_activity(self):
        """鼠标几乎没动 + 没触摸 → 拒"""
        token = "stillon"
        proof = self._good_proof(token)
        proof["events"]["mouseMoves"] = 1
        proof["events"]["mousePath"] = 5
        proof["events"]["touchEvents"] = 0
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)

    def test_touch_device_ok(self):
        """触摸设备（没鼠标但有触摸）→ 通过"""
        token = "phonetoken"
        proof = self._good_proof(token)
        proof["events"]["mouseMoves"] = 0
        proof["events"]["mousePath"] = 0
        proof["events"]["touchEvents"] = 5
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertTrue(ok)

    def test_proof_failure_accumulates(self):
        """失败累计，达到上限 token 被清"""
        async def fake_gen():
            return "星空"
        with patch.object(self.verify, '_generate_web_code', new=fake_gen):
            token = _run(self.verify.create_web_challenge(100, 1001, "alice"))

        # 失败 MAX_WRONG_ATTEMPTS_WEB 次
        for i in range(self.verify.MAX_WRONG_ATTEMPTS_WEB - 1):
            r = _run(self.verify._record_proof_failure(token))
            self.assertFalse(r["kicked"])
        r = _run(self.verify._record_proof_failure(token))
        self.assertTrue(r["kicked"])
        self.assertNotIn(token, self.verify._web_pending)

    def test_pow_bits_out_of_range(self):
        """难度 bits 异常（<8 或 >24）→ 拒"""
        token = "weirdtoken"
        proof = self._good_proof(token)
        proof["pow"]["bits"] = 2
        proof["pow"]["nonce"] = 0    # 任意值，因 bits 异常都拒
        ok, reason = self.verify._verify_local_proof(token, proof)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
