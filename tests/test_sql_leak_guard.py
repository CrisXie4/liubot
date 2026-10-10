"""SQL 泄漏防御测试 — python tests/test_sql_leak_guard.py

场景：AI 调中转站数据库工具后，最终回复把 SQL 命令原样带进群聊。
覆盖：检测（围栏/裸语句/行内片段）与剥离（不误伤正常聊天文本）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.chat import _looks_like_sql, _strip_sql_text


class TestLooksLikeSql(unittest.TestCase):

    def test_bare_multiline_select(self):
        """裸多行 SELECT...FROM 检出"""
        text = ("查了下，最近两天的情况：\n"
                "SELECT model_name, COUNT(*) cnt, SUM(quota) 用量\n"
                "FROM logs\n"
                "WHERE created_at >= extract(epoch from now()) - 172800\n"
                "GROUP BY model_name")
        self.assertTrue(_looks_like_sql(text))

    def test_single_line_select(self):
        text = "我用 SELECT username FROM users ORDER BY used_quota DESC 查的"
        self.assertTrue(_looks_like_sql(text))

    def test_fenced_sql_block(self):
        text = ("结论：codex 没人跑\n"
                "```\n"
                "SELECT model_name, status FROM models WHERE model_name ILIKE '%codex%'\n"
                "```")
        self.assertTrue(_looks_like_sql(text))

    def test_cte_and_dml(self):
        self.assertTrue(_looks_like_sql("WITH t AS (SELECT 1) SELECT * FROM t"))
        self.assertTrue(_looks_like_sql("INSERT INTO users (username) VALUES ('x')"))
        self.assertTrue(_looks_like_sql("DELETE FROM logs WHERE id=1"))
        self.assertTrue(_looks_like_sql("UPDATE users SET used_quota=0"))

    def test_normal_chat_not_flagged(self):
        """正常聊天不误伤"""
        for text in (
            "查到了，最近两天 gemini-3.5-flash 跑了39次",
            "没干嘛 你问codex没人跑嘛 我查了下 没上架",
            "我更新了一下心情， update 完了",   # 裸 update 不带 set 不算
            "今天 selector 抽卡运气不错",        # select 词根但非语句
            "",
        ):
            self.assertFalse(_looks_like_sql(text), text)


class TestStripSqlText(unittest.TestCase):

    def test_strip_multiline_keeps_conclusion(self):
        text = ("查了下，最近两天的情况：\n"
                "SELECT model_name, COUNT(*) cnt, SUM(quota) 用量\n"
                "FROM logs\n"
                "GROUP BY model_name\n"
                "结果就 gemini-3.5-flash 跑了39次")
        out = _strip_sql_text(text)
        self.assertNotIn("SELECT", out)
        self.assertNotIn("FROM logs", out)
        self.assertIn("查了下，最近两天的情况", out)
        self.assertIn("gemini-3.5-flash 跑了39次", out)

    def test_strip_fenced_block(self):
        text = ("结论：codex 没人跑\n"
                "```\n"
                "SELECT model_name, status FROM models\n"
                "```\n"
                "就这样")
        out = _strip_sql_text(text)
        self.assertNotIn("SELECT", out)
        self.assertIn("结论：codex 没人跑", out)
        self.assertIn("就这样", out)

    def test_strip_inline_span(self):
        text = "我用 SELECT username FROM users 查的，第一名用量最多"
        out = _strip_sql_text(text)
        self.assertNotIn("SELECT", out)
        self.assertNotIn("FROM users", out)

    def test_strip_leadin_line(self):
        text = "查到了\n查询语句：\nSELECT 1 FROM users"
        out = _strip_sql_text(text)
        self.assertNotIn("查询语句", out)
        self.assertNotIn("SELECT", out)

    def test_strip_all_sql_leaves_empty(self):
        """整条都是 SQL → 剥完为空（调用方走兜底文案）"""
        text = ("SELECT model_name, COUNT(*) cnt\n"
                "FROM logs\n"
                "GROUP BY model_name")
        out = _strip_sql_text(text)
        self.assertEqual(out, "")

    def test_keeps_cq_codes(self):
        """CQ 码不受影响"""
        text = "查到了 [CQ:face,id=14]\nSELECT x FROM users"
        out = _strip_sql_text(text)
        self.assertIn("[CQ:face,id=14]", out)
        self.assertNotIn("SELECT", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
