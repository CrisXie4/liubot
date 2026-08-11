"""新闻新鲜度校验测试"""
import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.ai_brain import _is_news_too_old


class TestNewsFreshness(unittest.TestCase):

    def test_old_news_rejected(self):
        old = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        self.assertTrue(_is_news_too_old(old, max_age_days=30))

    def test_recent_news_accepted(self):
        recent = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertFalse(_is_news_too_old(recent, max_age_days=30))

    def test_empty_date_accepted(self):
        self.assertFalse(_is_news_too_old("", max_age_days=30))

    def test_just_at_boundary(self):
        boundary = (datetime.utcnow() - timedelta(days=29)).strftime("%Y-%m-%d")
        self.assertFalse(_is_news_too_old(boundary, max_age_days=30))


if __name__ == "__main__":
    unittest.main()
