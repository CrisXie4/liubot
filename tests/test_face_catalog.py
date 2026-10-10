"""表情菜单（face_catalog）测试。"""
import sys
import os
import re
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import face_catalog


class TestFaceCatalog(unittest.TestCase):

    def test_catalog_size_and_types(self):
        self.assertGreater(len(face_catalog.FACE_CATALOG), 100)
        for fid, name in face_catalog.FACE_CATALOG.items():
            self.assertIsInstance(fid, int)
            self.assertTrue(1 <= len(name) <= 6)

    def test_menu_format_valid_shortcuts(self):
        menu = face_catalog.render_face_menu()
        shortcuts = re.findall(r":(\d{1,4}):", menu)
        self.assertEqual(len(shortcuts), len(face_catalog.FACE_CATALOG))
        # 所有快捷码都能被发送端展开（数字 id 合法）
        for s in shortcuts:
            self.assertTrue(s.isdigit())

    def test_known_entries_present(self):
        menu = face_catalog.render_face_menu()
        for token in (":179:doge", ":96:冷汗", ":285:摸鱼", ":271:吃瓜", ":319:比心"):
            self.assertIn(token, menu)

    def test_no_mislabelled_legacy_entries(self):
        """旧 prompt 的错位标注不得出现（178 是笑哭不是斜眼、96 是冷汗不是开心）"""
        menu = face_catalog.render_face_menu()
        self.assertNotIn(":178:斜眼", menu)
        self.assertNotIn(":96:开心", menu)


if __name__ == "__main__":
    unittest.main()
