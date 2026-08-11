"""自动表情表态限速测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import guardian


class TestAutoEmojiRate(unittest.TestCase):

    def setUp(self):
        guardian._rate_state.clear()

    def test_allows_first_8(self):
        for _ in range(8):
            self.assertTrue(guardian.check_auto_emoji_rate(123))

    def test_blocks_9th(self):
        for _ in range(8):
            guardian.check_auto_emoji_rate(123)
        self.assertFalse(guardian.check_auto_emoji_rate(123))


if __name__ == "__main__":
    unittest.main()
