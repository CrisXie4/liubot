"""同一条消息只触发一次表情收集（源码级回归锁）。

handle_group_message 里曾有两次 maybe_collect_sticker 调用：主扫描已覆盖
同一 raw_message，第二次只会靠 unique_key 去重虚增 favorite_count + 重复
文件 IO。handler 全函数体量大、仓库现有测试不端到端跑它，故用源码断言锁。
"""
import inspect
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import handler


class TestStickerCollectOnce(unittest.TestCase):

    def test_maybe_collect_sticker_called_once(self):
        """maybe_collect_sticker( 在 handle_group_message 源中恰出现 1 次"""
        src = inspect.getsource(handler.handle_group_message)
        self.assertEqual(src.count("maybe_collect_sticker("), 1,
                         "同一条消息只应有一个表情收集入口")

    def test_no_double_pass_leftover(self):
        """except 后连续两个 pass 的遗留已清理"""
        src = inspect.getsource(handler.handle_group_message)
        self.assertNotIn("pass\n            pass", src)

    def test_collect_entry_inside_main_scan(self):
        """唯一入口在主扫描（raw_message 含 [CQ: 的分支）里"""
        src = inspect.getsource(handler.handle_group_message)
        self.assertIn('sticker.maybe_collect_sticker(raw_message, group_id, user_id)', src)


if __name__ == "__main__":
    unittest.main()
