"""reply_policy 核心判定直接单测（纯新增，不改源码）。

覆盖此前无直接单测的三块核心：
- 流水账 note_message / messages_after
- is_still_current（话头新旧判定）
- should_quote / sanitize_targeting（引用去留与清洗）
"""
import sys
import os
import time
import unittest
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import reply_policy as rp

GID = 9527


class ReplyPolicyBase(unittest.TestCase):

    def setUp(self):
        rp._log.clear()


class TestLedger(ReplyPolicyBase):
    """note_message / messages_after 流水账"""

    def test_note_and_count_after(self):
        rp.note_message(GID, 1, 100)
        rp.note_message(GID, 2, 101)
        rp.note_message(GID, 3, 102)
        self.assertEqual(rp.messages_after(GID, 1), 2)
        self.assertEqual(rp.messages_after(GID, 3), 0)

    def test_messages_after_missing(self):
        self.assertEqual(rp.messages_after(GID, 999), -1)
        self.assertEqual(rp.messages_after(GID, 0), -1)

    def test_note_ignores_invalid(self):
        """group_id/message_id 缺失不记、不崩"""
        rp.note_message(0, 1, 100)
        rp.note_message(GID, 0, 100)
        self.assertNotIn(GID, rp._log)

    def test_log_capped(self):
        """流水账上限 LOG_KEEP=60，FIFO 淘汰"""
        for i in range(1, 70):
            rp.note_message(GID, i, 100)
        dq = rp._log[GID]
        self.assertEqual(len(dq), rp.LOG_KEEP)
        self.assertEqual(dq[0]["mid"], 10)   # 前 9 条被挤掉
        self.assertEqual(rp.messages_after(GID, 1), -1)  # 旧消息已不在账上


class TestIsStillCurrent(ReplyPolicyBase):

    def test_fresh_message_current(self):
        rp.note_message(GID, 1, 100)
        self.assertTrue(rp.is_still_current(GID, 1))

    def test_brushed_past_by_msgs(self):
        """后面隔 ≥3 条 → 不在话头"""
        rp.note_message(GID, 1, 100)
        for mid in (2, 3, 4):
            rp.note_message(GID, mid, 100 + mid)
        self.assertFalse(rp.is_still_current(GID, 1))
        self.assertTrue(rp.is_still_current(GID, 4))  # 最新的还在话头

    def test_too_old_by_seconds(self):
        """过了 ≥180s → 不在话头（直接注入旧 ts，不真实等待）"""
        rp._log[GID] = deque([{"mid": 1, "qq": 100,
                               "ts": time.time() - 200}],
                             maxlen=rp.LOG_KEEP)
        self.assertFalse(rp.is_still_current(GID, 1))

    def test_not_in_ledger(self):
        """账上没有 → 当作已经过去"""
        rp.note_message(GID, 1, 100)
        self.assertFalse(rp.is_still_current(GID, 999))


class TestShouldQuote(ReplyPolicyBase):

    def test_current_topic_no_quote(self):
        """目标=正在回的那条且还在话头 → 不引用"""
        rp.note_message(GID, 10, 100)
        self.assertFalse(rp.should_quote(GID, 10, trigger_mid=10))

    def test_brushed_past_quote(self):
        """被刷过去 → 引用"""
        rp.note_message(GID, 10, 100)
        for mid in (11, 12, 13):
            rp.note_message(GID, mid, 100 + mid)
        self.assertTrue(rp.should_quote(GID, 10, trigger_mid=10))
        # 不带 trigger 的旧消息同理
        self.assertTrue(rp.should_quote(GID, 10))

    def test_zero_target(self):
        self.assertFalse(rp.should_quote(GID, 0))


class TestSanitizeTargeting(ReplyPolicyBase):

    def test_fresh_quote_stripped(self):
        """引用还在话头上的新消息 → 剥掉"""
        rp.note_message(GID, 10, 100)
        out = rp.sanitize_targeting(
            "[CQ:reply,id=10]好的 就这么办",
            group_id=GID, trigger_message_id=10, trigger_user_qq=100,
        )
        self.assertNotIn("[CQ:reply", out)
        self.assertIn("好的 就这么办", out)

    def test_old_quote_kept_and_moved_to_front(self):
        """旧消息引用保留，且归到整段开头（QQ 只认开头的 reply 码）"""
        rp.note_message(GID, 1, 100)
        for mid in (2, 3, 4):
            rp.note_message(GID, mid, 100 + mid)
        out = rp.sanitize_targeting(
            "我看还是这个对[CQ:reply,id=1]",
            group_id=GID, trigger_message_id=4, trigger_user_qq=104,
        )
        self.assertTrue(out.startswith("[CQ:reply,id=1]"))
        self.assertEqual(out.count("[CQ:reply"), 1)
        self.assertIn("我看还是这个对", out)

    def test_two_quotes_only_one_kept(self):
        """两个引用只留 max_quotes=1 个"""
        rp.note_message(GID, 1, 100)
        rp.note_message(GID, 2, 101)
        for mid in (3, 4, 5):
            rp.note_message(GID, mid, 100 + mid)
        out = rp.sanitize_targeting(
            "[CQ:reply,id=1][CQ:reply,id=2]这两条都说得对",
            group_id=GID, trigger_message_id=5, trigger_user_qq=105,
        )
        self.assertEqual(out.count("[CQ:reply"), 1)
        self.assertTrue(out.startswith("[CQ:reply,id=1]"))

    def test_no_double_space_or_blank_lines_left(self):
        """剥离后不残留空行/双空格"""
        rp.note_message(GID, 10, 100)
        out = rp.sanitize_targeting(
            "[CQ:reply,id=10]   多   空格\n\n\n后面一段",
            group_id=GID, trigger_message_id=10, trigger_user_qq=100,
        )
        self.assertNotIn("  ", out)
        self.assertFalse(out.startswith((" ", "\n")))
        self.assertFalse(out.endswith((" ", "\n")))
        self.assertNotIn("\n\n\n", out)
        self.assertIn("多 空格", out)

    def test_garbage_reply_id_not_matched(self):
        """id 不是数字的引用码不在 _REPLY_RE 匹配范围内 → 原样保留、不崩"""
        out = rp.sanitize_targeting(
            "[CQ:reply,id=abc]回复",
            group_id=GID, trigger_message_id=0,
        )
        self.assertEqual(out, "[CQ:reply,id=abc]回复")


if __name__ == "__main__":
    unittest.main()
