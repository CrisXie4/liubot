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


if __name__ == "__main__":
    unittest.main()
