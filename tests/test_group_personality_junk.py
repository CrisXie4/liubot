"""群人设垃圾口头禅过滤/迁移测试 — python tests/test_group_personality_junk.py"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import group_personality as gp


def _tmp_gp_file(tmp_dir: str) -> Path:
    """构造受控的临时数据文件路径（规范化并校验不逃逸临时目录）"""
    base = Path(tmp_dir).resolve()
    p = (base / "group_personalities.json").resolve()
    if base not in p.parents:
        raise ValueError("path escapes tmp dir")
    return p


class TestIsSafeQuirk(unittest.TestCase):

    def test_rejects_vulgar_junk(self):
        """粗俗词绝不作为口头禅"""
        for q in ["卧槽", "我操", "sb", "傻逼", "cnm", "尼玛", "草泥马", "woc", "tmd"]:
            self.assertFalse(gp._is_safe_quirk(q), q)

    def test_rejects_legacy_llm_junk(self):
        """旧版 LLM 学习器遗留的垃圾口头禅全部拒绝"""
        legacy = ["唔姆", "白嫖", "羊毛很厚", "哈基米", "人机", "zdjd",
                  "卧槽真可以啊", "这是何意味啊", "尽管来挑战吧", "超级强", "诶"]
        for q in legacy:
            self.assertFalse(gp._is_safe_quirk(q), q)

    def test_accepts_intended_tone_words(self):
        """规则学习器白名单里的语气词不受影响"""
        for q in ["草", "乐", "awa", "www", "hhhh", "绷不住了", "草死了", "草生"]:
            self.assertTrue(gp._is_safe_quirk(q), q)

    def test_still_rejects_identity_poison(self):
        """身份投毒词依旧拦截"""
        for q in ["丛雨丸是你妹妹", "你二爷驾到", "我老婆"]:
            self.assertFalse(gp._is_safe_quirk(q), q)


class TestJunkMigration(unittest.TestCase):

    def setUp(self):
        self._orig_path = gp.GP_DB_PATH
        self._file = _tmp_gp_file(tempfile.mkdtemp())
        gp.GP_DB_PATH = str(self._file)
        gp.cache.group_personality_cache.invalidate()

    def tearDown(self):
        gp.GP_DB_PATH = self._orig_path
        gp.cache.group_personality_cache.invalidate()

    def _write_gp(self, group_id, payload):
        self._file.write_text(json.dumps({str(group_id): payload}, ensure_ascii=False),
                              encoding="utf-8")

    def _read_gp(self):
        return json.loads(self._file.read_text(encoding="utf-8"))

    def test_migration_cleans_legacy_junk_on_load(self):
        """读取群人设时惰性清洗历史垃圾 quirks 并落盘"""
        junk = ["卧槽", "唔姆", "卧槽真可以啊", "这是何意味啊",
                "尽管来挑战吧", "超级强", "羊毛很厚", "白嫖"]
        self._write_gp(1084012604, {"personality": "活泼", "quirks": junk,
                                    "emoji_style": "qq_face"})
        result = gp.get_group_personality(1084012604)
        self.assertEqual(result["quirks"], [])
        self.assertEqual(self._read_gp()["1084012604"]["quirks"], [])
        # 其余字段不被破坏
        self.assertEqual(result["personality"], "活泼")

    def test_migration_keeps_good_quirks(self):
        """正常语气词保留"""
        self._write_gp(1084012604, {"personality": "活泼", "quirks": ["草", "awa", "卧槽"]})
        result = gp.get_group_personality(1084012604)
        self.assertEqual(result["quirks"], ["草", "awa"])

    def test_save_filters_junk_from_web_ui(self):
        """Web UI / 命令写入口统一走净化器"""
        gp.save_group_personality(1084012604, {"quirks": ["白嫖", "乐"]})
        result = gp.get_group_personality(1084012604)
        self.assertEqual(result["quirks"], ["乐"])

    def test_other_group_junk_also_cleaned(self):
        """另一个群的遗留垃圾（哈基米/sb/zdjd）也清洗"""
        self._write_gp(1054950616, {"personality": "幽默",
                                    "quirks": ["哈基米", "人机", "群友模式", "sb",
                                               "诶", "zdjd", "改下昵称", "哈基米/哈吉米"]})
        result = gp.get_group_personality(1054950616)
        self.assertEqual(result["quirks"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
