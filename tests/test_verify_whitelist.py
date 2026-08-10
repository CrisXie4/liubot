"""verify 白名单 + 放行业务函数测试 — python tests/test_verify_whitelist.py"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


class TestVerifyWhitelist(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        # 用临时文件隔离测试
        self._orig_file = verify._WHITELIST_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._WHITELIST_FILE = self.tmp.name
        # 清内存缓存
        verify._whitelist_cache = None

    def tearDown(self):
        self.verify._WHITELIST_FILE = self._orig_file
        self.verify._whitelist_cache = None
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_global_whitelist_match(self):
        """加 global 白名单 → is_whitelisted 命中"""
        self.assertTrue(self.verify.add_whitelist(123456))
        self.assertTrue(self.verify.is_whitelisted(123456, 999))

    def test_by_group_whitelist_match(self):
        """加 by_group 白名单 → 仅该群命中"""
        self.assertTrue(self.verify.add_whitelist(111, group_id=888))
        self.assertTrue(self.verify.is_whitelisted(111, 888))
        self.assertFalse(self.verify.is_whitelisted(111, 999))

    def test_remove_whitelist(self):
        """移除后不再命中"""
        self.verify.add_whitelist(222)
        self.assertTrue(self.verify.is_whitelisted(222, 1))
        self.verify.remove_whitelist(222)
        self.assertFalse(self.verify.is_whitelisted(222, 1))

    def test_list_whitelist_format(self):
        """list_whitelist 返回正确 schema"""
        self.verify.add_whitelist(100)
        self.verify.add_whitelist(200, group_id=555)
        result = self.verify.list_whitelist()
        self.assertIn("global", result)
        self.assertIn("by_group", result)
        self.assertIn(100, result["global"])
        self.assertIn(200, result["by_group"].get("555", []))

    def test_persistence_after_reload(self):
        """写盘后重新加载，白名单仍生效"""
        self.verify.add_whitelist(333, group_id=777)
        # 模拟重启：清内存缓存
        self.verify._whitelist_cache = None
        self.assertTrue(self.verify.is_whitelisted(333, 777))


class TestApprovePending(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        self.verify._pending.clear()

    def tearDown(self):
        self.verify._pending.clear()

    def test_approve_pending_user_in_queue(self):
        """待验证中的用户被放行"""
        import time
        self.verify._pending[111] = {222: {
            "question": "1+1", "answer": "2", "user_name": "Newbie",
            "created_at": time.time(), "wrong_attempts": 0,
        }}
        bot = MagicMock()
        bot.send_group_msg = AsyncMock(return_value=1)
        ok = _run(self.verify.approve_pending(bot, 111, 222))
        self.assertTrue(ok)
        # _pending 已清理
        self.assertNotIn(222, self.verify._pending.get(111, {}))

    def test_approve_pending_user_not_in_queue(self):
        """不在待验证 → 返回 False"""
        bot = MagicMock()
        ok = _run(self.verify.approve_pending(bot, 999, 888))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
