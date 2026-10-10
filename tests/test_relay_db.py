"""中转站数据库（relay_db）工具测试 — 不连真实数据库"""
import sys
import os
import unittest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools, relay_db
from bot.config import config


def _run(coro):
    return asyncio.run(coro)


class TestReadSqlCheck(unittest.TestCase):
    """check_read_sql：只读语句分类"""

    def test_select_ok(self):
        ok, _ = relay_db.check_read_sql("SELECT 1")
        self.assertTrue(ok)

    def test_select_lowercase_parens_ok(self):
        ok, _ = relay_db.check_read_sql("(select username from users limit 5);")
        self.assertTrue(ok)

    def test_with_ok(self):
        ok, _ = relay_db.check_read_sql(
            "WITH t AS (SELECT user_id, SUM(quota) q FROM logs GROUP BY 1) "
            "SELECT * FROM t ORDER BY q DESC LIMIT 10"
        )
        self.assertTrue(ok)

    def test_show_explain_ok(self):
        for sql in ("SHOW statement_timeout", "EXPLAIN SELECT 1"):
            ok, _ = relay_db.check_read_sql(sql)
            self.assertTrue(ok, sql)

    def test_leading_comment_ok(self):
        ok, _ = relay_db.check_read_sql("-- 查Top10\nSELECT username FROM users")
        self.assertTrue(ok)

    def test_write_statements_rejected(self):
        for sql in (
            "UPDATE users SET quota = 0",
            "DELETE FROM logs",
            "INSERT INTO users VALUES (1)",
            "TRUNCATE TABLE logs",
            "DROP TABLE users",
            "CREATE TABLE x (id int)",
            "GRANT ALL ON users TO public",
            "/* 伪装 */ DELETE FROM tokens",
        ):
            ok, _ = relay_db.check_read_sql(sql)
            self.assertFalse(ok, sql)

    def test_multi_statement_rejected(self):
        ok, _ = relay_db.check_read_sql("SELECT 1; DROP TABLE users")
        self.assertFalse(ok)

    def test_empty_rejected(self):
        ok, _ = relay_db.check_read_sql("   ")
        self.assertFalse(ok)

    def test_too_long_rejected(self):
        ok, _ = relay_db.check_read_sql("SELECT " + "1" * 6000)
        self.assertFalse(ok)


class TestWriteSqlCheck(unittest.TestCase):
    """check_write_sql：主人写路径仍限制单条 + 长度"""

    def test_update_ok(self):
        ok, _ = relay_db.check_write_sql("UPDATE users SET quota = quota + 1000 WHERE id = 42")
        self.assertTrue(ok)

    def test_multi_statement_rejected(self):
        ok, _ = relay_db.check_write_sql("UPDATE users SET quota=1; DROP TABLE logs")
        self.assertFalse(ok)

    def test_empty_rejected(self):
        ok, _ = relay_db.check_write_sql("")
        self.assertFalse(ok)


class TestFormatResult(unittest.TestCase):
    """结果格式化：敏感列打码 + 截断"""

    def test_sensitive_columns_masked(self):
        cols = ["username", "password", "key", "token_name", "access_token", "secret"]
        rows = [("alice", "p@ss", "sk-abc", "my-token", "at123", "s3cr3t")]
        out = relay_db._format_result(cols, rows)
        self.assertIn("alice", out)
        self.assertIn("my-token", out)  # token_name 只是名字，不打码
        self.assertNotIn("p@ss", out)
        self.assertNotIn("sk-abc", out)
        self.assertNotIn("at123", out)
        self.assertNotIn("s3cr3t", out)

    def test_row_cap_note(self):
        cols = ["n"]
        rows = [(i,) for i in range(relay_db.MAX_ROWS + 1)]
        out = relay_db._format_result(cols, rows)
        self.assertIn("只显示前", out)

    def test_cell_truncated(self):
        out = relay_db._format_result(["v"], [("x" * 500,)])
        self.assertIn("…", out)


class TestToolRegistration(unittest.TestCase):
    """工具注册与按配置显隐"""

    def test_tools_registered(self):
        names = [t["name"] for t in ai_tools.ALL_TOOLS]
        for n in ("relay_db_schema", "relay_db_query", "relay_db_execute"):
            self.assertIn(n, names)
        self.assertIn("relay_db_execute", ai_tools.DANGEROUS_TOOLS)

    def test_hidden_when_not_configured(self):
        old = getattr(config, "RELAY_DB_URL", "")
        try:
            config.RELAY_DB_URL = ""
            names = [t["name"] for t in ai_tools.get_tools_for_group(True, True)]
            for n in ("relay_db_schema", "relay_db_query", "relay_db_execute"):
                self.assertNotIn(n, names)
        finally:
            config.RELAY_DB_URL = old

    def test_visible_when_configured(self):
        old = getattr(config, "RELAY_DB_URL", "")
        try:
            config.RELAY_DB_URL = "postgresql://u:p@h/db"
            names = [t["name"] for t in ai_tools.get_tools_for_group(True, True)]
            self.assertIn("relay_db_query", names)
            self.assertIn("relay_db_execute", names)
        finally:
            config.RELAY_DB_URL = old


class TestExecuteToolGating(unittest.TestCase):
    """execute_tool 权限闸门：写仅主人、读校验只读、冷却"""

    def setUp(self):
        self._old_url = getattr(config, "RELAY_DB_URL", "")
        self._old_owner = getattr(config, "OWNER_QQ", "")
        config.RELAY_DB_URL = "postgresql://u:p@h/db"
        config.OWNER_QQ = "424242"
        relay_db._query_last.clear()

    def tearDown(self):
        config.RELAY_DB_URL = self._old_url
        config.OWNER_QQ = self._old_owner
        relay_db._query_last.clear()

    def test_execute_rejected_for_non_master(self):
        calls = []

        async def fake_write(sql):
            calls.append(sql)
            return "已执行"

        old = relay_db.run_write
        relay_db.run_write = fake_write
        try:
            result = _run(ai_tools.execute_tool(
                None, 123, 999, "relay_db_execute",
                {"sql": "UPDATE users SET quota=1"}, caller_qq=999,
            ))
        finally:
            relay_db.run_write = old
        self.assertIn("主人", result)
        self.assertEqual(calls, [])

    def test_execute_allowed_for_master(self):
        calls = []

        async def fake_write(sql):
            calls.append(sql)
            return "已执行，影响 1 行"

        old = relay_db.run_write
        relay_db.run_write = fake_write
        try:
            result = _run(ai_tools.execute_tool(
                None, 123, 424242, "relay_db_execute",
                {"sql": "UPDATE users SET quota=1"},
                caller_qq=424242, master_instructed=True,
            ))
        finally:
            relay_db.run_write = old
        self.assertIn("影响 1 行", result)
        self.assertEqual(calls, ["UPDATE users SET quota=1"])

    def test_query_rejects_write_sql_before_db(self):
        calls = []

        async def fake_read(sql):
            calls.append(sql)
            return "ok"

        old = relay_db.run_read
        relay_db.run_read = fake_read
        try:
            result = _run(ai_tools.execute_tool(
                None, 123, 999, "relay_db_query",
                {"sql": "DELETE FROM logs"}, caller_qq=999,
            ))
        finally:
            relay_db.run_read = old
        self.assertIn("⨯", result)
        self.assertEqual(calls, [])

    def test_query_passes_select_to_read(self):
        calls = []

        async def fake_read(sql):
            calls.append(sql)
            return "username | used\nalice | 123"

        old = relay_db.run_read
        relay_db.run_read = fake_read
        try:
            result = _run(ai_tools.execute_tool(
                None, 123, 999, "relay_db_query",
                {"sql": "SELECT username FROM users LIMIT 5"}, caller_qq=999,
            ))
        finally:
            relay_db.run_read = old
        self.assertIn("alice", result)
        self.assertEqual(calls, ["SELECT username FROM users LIMIT 5"])

    def test_query_cooldown(self):
        async def fake_read(sql):
            return "ok"

        old = relay_db.run_read
        relay_db.run_read = fake_read
        try:
            r1 = _run(ai_tools.execute_tool(
                None, 555, 999, "relay_db_query", {"sql": "SELECT 1"}, caller_qq=999))
            r2 = _run(ai_tools.execute_tool(
                None, 555, 999, "relay_db_query", {"sql": "SELECT 2"}, caller_qq=999))
        finally:
            relay_db.run_read = old
        self.assertEqual(r1, "ok")
        self.assertIn("频繁", r2)


if __name__ == "__main__":
    unittest.main()
