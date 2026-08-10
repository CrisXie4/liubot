"""find_members_by_name 单元测试 —— 用 python tests/test_identity_find.py 直接跑"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import identity


def _fake_bot(members):
    """构造一个假 bot，get_group_member_list 返回 members"""
    bot = MagicMock()
    bot.get_group_member_list = AsyncMock(return_value=members)
    return bot


MEMBERS = [
    {"user_id": 111, "card": "嘟嘟", "nickname": "dd", "role": "member"},
    {"user_id": 222, "card": "小嘟嘟", "nickname": "嘟嘟大人", "role": "member"},
    {"user_id": 333, "card": "", "nickname": "嘟嘟", "role": "admin"},
    {"user_id": 444, "card": "阿强", "nickname": "强哥", "role": "owner"},
    {"user_id": 555, "card": "DU", "nickname": "DuDu", "role": "member"},
]


class TestFindMembers(unittest.TestCase):

    def test_exact_match_card(self):
        """精确匹配群名片"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res if r["match_type"] == "exact"]
        self.assertIn(111, qqs)  # card 完全等于"嘟嘟"

    def test_exact_match_nickname(self):
        """精确匹配 QQ 昵称"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res if r["match_type"] == "exact"]
        self.assertIn(333, qqs)  # nickname 完全等于"嘟嘟"

    def test_substring_match(self):
        """子串匹配：'嘟嘟' 命中 '小嘟嘟' 的 card 和 '嘟嘟大人' 的 nickname"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res]
        self.assertIn(222, qqs)

    def test_exact_sorted_before_substring(self):
        """exact 排在 substring 前面"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        # 第一个应该是 exact
        self.assertEqual(res[0]["match_type"], "exact")

    def test_zero_match(self):
        """查不到返回空列表"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "不存在的名字"))
        self.assertEqual(res, [])

    def test_case_insensitive(self):
        """英文大小写无关：'dudu' 命中 'DuDu'"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "dudu"))
        qqs = [r["qq"] for r in res]
        self.assertIn(555, qqs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
