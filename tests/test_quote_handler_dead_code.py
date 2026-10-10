"""quote_handler 死代码清理测试 — 四个全仓库无调用点的辅助函数已删除。

extract_reply_text 依赖的「reply 段带 text」假设在 NapCat 本就不成立。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.quote_handler as qh


class TestDeadCodeRemoved(unittest.TestCase):
    def test_dead_functions_gone(self):
        """四个死函数不再存在于模块命名空间"""
        for name in ("build_reply_cq", "build_quote_reply",
                     "extract_reply_id", "extract_reply_text"):
            self.assertFalse(hasattr(qh, name), f"{name} 应已删除")

    def test_live_functions_kept(self):
        """在用的两个函数仍在"""
        self.assertTrue(hasattr(qh, "get_quoted_context"))
        self.assertTrue(hasattr(qh, "format_quoted_context_for_ai"))


if __name__ == "__main__":
    unittest.main()
