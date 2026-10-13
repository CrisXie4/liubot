"""网页截图模块测试 — python tests/test_web_shot.py"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import web_shot


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, content=b"", content_type="image/jpeg", status=200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """按 URL 关键字路由响应的假 httpx 客户端"""

    def __init__(self, routes: dict, calls: list):
        self.routes = routes   # 关键字 → _FakeResp 或 异常
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        return _FakeResp(b"", "text/html", 404)


class TestCapture(unittest.TestCase):

    def setUp(self):
        web_shot._rate.clear()

    def tearDown(self):
        web_shot._rate.clear()

    def test_thum_success_first_try(self):
        calls = []
        img = b"x" * 8000
        routes = {"thum.io": _FakeResp(img)}
        with patch.object(web_shot.httpx, "AsyncClient",
                          lambda **kw: _FakeClient(routes, calls)):
            data = _run(web_shot.capture("https://example.com/page"))
        self.assertEqual(data, img)
        self.assertEqual(len(calls), 1)  # thum 一次成功，mShots 不再试

    def test_falls_back_to_mshots(self):
        calls = []
        img = b"y" * 9000
        routes = {
            "thum.io": _FakeResp(b"tiny", "image/png"),   # 占位小图
            "mshots": _FakeResp(img),
        }
        with patch.object(web_shot.httpx, "AsyncClient",
                          lambda **kw: _FakeClient(routes, calls)), \
             patch.object(web_shot.asyncio, "sleep", new=AsyncMock()):
            data = _run(web_shot.capture("https://example.com/page"))
        self.assertEqual(data, img)

    def test_mshots_placeholder_retries(self):
        """mShots 未就绪返回小占位图 → 按间隔重试拿到真图"""
        calls = []
        img = b"z" * 9000
        holder = {"n": 0}

        class _Flaky(_FakeResp):
            pass

        def routes_for(_url):
            return None

        # mshots 前两次返回占位、第三次返回真图
        client = _FakeClient({}, calls)
        client.routes = {"thum.io": RuntimeError("boom")}

        async def fake_get(url):
            calls.append(url)
            if "mshots" in url:
                holder["n"] += 1
                return _FakeResp(img if holder["n"] >= 3 else b"placeholder")
            raise RuntimeError("thum down")

        client.get = fake_get
        with patch.object(web_shot.httpx, "AsyncClient", lambda **kw: client), \
             patch.object(web_shot.asyncio, "sleep", new=AsyncMock()) as ms:
            data = _run(web_shot.capture("https://example.com/"))
        self.assertEqual(data, img)
        self.assertEqual(ms.await_count, 2)

    def test_all_fail_returns_none(self):
        calls = []
        routes = {"thum.io": RuntimeError("x"), "mshots": RuntimeError("y")}
        with patch.object(web_shot.httpx, "AsyncClient",
                          lambda **kw: _FakeClient(routes, calls)), \
             patch.object(web_shot.asyncio, "sleep", new=AsyncMock()):
            self.assertIsNone(_run(web_shot.capture("https://example.com/")))

    def test_non_http_rejected(self):
        self.assertIsNone(_run(web_shot.capture("ftp://example.com/x")))
        self.assertIsNone(_run(web_shot.capture("")))

    def test_internal_url_rejected(self):
        """SSRF 口径与 web_browse 一致：内网地址不截"""
        self.assertIsNone(_run(web_shot.capture("http://127.0.0.1:4000/")))
        self.assertIsNone(_run(web_shot.capture("http://192.168.1.1/")))


class TestRateLimit(unittest.TestCase):

    def setUp(self):
        web_shot._rate.clear()

    def tearDown(self):
        web_shot._rate.clear()

    def test_three_allowed_then_blocked(self):
        for i in range(3):
            self.assertEqual(web_shot.check_rate(123), "")
        self.assertIn("太频繁", web_shot.check_rate(123))

    def test_groups_independent(self):
        web_shot.check_rate(1)
        web_shot.check_rate(1)
        web_shot.check_rate(1)
        self.assertIn("太频繁", web_shot.check_rate(1))
        self.assertEqual(web_shot.check_rate(2), "")

    def test_window_expiry(self):
        for _ in range(3):
            web_shot.check_rate(5)
        # 把窗口内第一条的时间戳拨到 6 分钟前
        web_shot._rate[5][0] = time.time() - 360
        self.assertEqual(web_shot.check_rate(5), "")


class TestWebScreenshotTool(unittest.TestCase):

    def setUp(self):
        web_shot._rate.clear()

    def tearDown(self):
        web_shot._rate.clear()

    def _bot(self):
        bot = MagicMock()
        bot.bot_qq = "10000"
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.send_private_msg = AsyncMock(return_value=2)
        return bot

    def test_capture_and_send_group(self):
        from bot import ai_tools
        bot = self._bot()
        img = b"i" * 5000
        with patch.object(web_shot, "capture", AsyncMock(return_value=img)):
            out = _run(ai_tools.execute_tool(
                bot, 123, 42, "web_screenshot", {"url": "https://example.com/"}))
        self.assertIn("已直接发出", out)
        cq = bot.send_group_msg.call_args[0][1]
        self.assertTrue(cq.startswith("[CQ:image,file=base64://"))

    def test_no_url_no_session(self):
        from bot import ai_tools
        bot = self._bot()
        out = _run(ai_tools.execute_tool(
            bot, 123, 42, "web_screenshot", {}))
        self.assertIn("没有正在浏览的页面", out)
        bot.send_group_msg.assert_not_called()

    def test_uses_current_browse_page(self):
        from bot import ai_tools, web_browse
        bot = self._bot()
        with patch.object(web_browse, "current_url",
                          return_value="https://example.com/open"), \
             patch.object(web_shot, "capture",
                          AsyncMock(return_value=b"i" * 5000)) as mc:
            out = _run(ai_tools.execute_tool(bot, 123, 42, "web_screenshot", {}))
        self.assertIn("已直接发出", out)
        self.assertIn("https://example.com/open", mc.call_args[0][0])

    def test_capture_fail_message(self):
        from bot import ai_tools
        bot = self._bot()
        with patch.object(web_shot, "capture", AsyncMock(return_value=None)):
            out = _run(ai_tools.execute_tool(
                bot, 123, 42, "web_screenshot", {"url": "https://x.example/"}))
        self.assertIn("没出图", out)
        bot.send_group_msg.assert_not_called()

    def test_rate_blocked_before_capture(self):
        from bot import ai_tools
        bot = self._bot()
        for _ in range(3):
            web_shot.check_rate(123)
        with patch.object(web_shot, "capture", AsyncMock(return_value=b"i" * 100)) as mc:
            out = _run(ai_tools.execute_tool(
                bot, 123, 42, "web_screenshot", {"url": "https://x.example/"}))
        self.assertIn("太频繁", out)
        mc.assert_not_called()

    def test_tool_registered_and_web_gated(self):
        from bot import ai_tools
        names = {t["name"] for t in ai_tools.ALL_TOOLS}
        self.assertIn("web_screenshot", names)
        enabled = {t["name"] for t in ai_tools.get_tools_for_group(False, web_search_enabled=True)}
        disabled = {t["name"] for t in ai_tools.get_tools_for_group(False, web_search_enabled=False)}
        self.assertIn("web_screenshot", enabled)
        self.assertNotIn("web_screenshot", disabled)


if __name__ == "__main__":
    unittest.main()
