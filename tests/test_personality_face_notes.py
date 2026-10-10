"""system prompt 表情标注与 face_catalog 目录一致性测试。

背景：personality 里有两套含义表（旧硬编码标注 vs face_catalog 菜单），
:178: 曾被标成"斜眼"实际是笑哭、:96: 曾被标成"开心"实际是冷汗，
AI 照旧标注挑表情会名不副实。本测试锁定：get_system_prompt() 两条路径
（默认人设 / custom_prompt+机制附录）里所有「:id:紧跟标注」的紧凑对，
只要 id 在 FACE_CATALOG 里，标注必须与目录一致。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import personality
from bot.face_catalog import FACE_CATALOG

# 紧凑形态「:id:紧跟标注」（第二个冒号后无空格）；散文形态（如「:182: 是笑哭」）
# 冒号后有空格，不会被此正则捕获，由下方专门断言锁定
COMPACT_PAIR_RE = re.compile(r"[：:](\d{1,4})[：:]([一-鿿a-zA-Z0-9]+)")


def _check_pairs(self, prompt: str):
    """id ∈ FACE_CATALOG 的标注必须等于目录含义"""
    for m in COMPACT_PAIR_RE.finditer(prompt):
        fid, note = int(m.group(1)), m.group(2)
        if fid in FACE_CATALOG:
            self.assertEqual(
                note, FACE_CATALOG[fid],
                f":{fid}: 在 prompt 里标注为「{note}」，目录里是「{FACE_CATALOG[fid]}」",
            )


class TestPersonalityFaceNotes(unittest.TestCase):

    def _prompt(self, monkeypatch_cfg: dict | None = None) -> str:
        cfg = dict(personality.DEFAULT)
        if monkeypatch_cfg:
            cfg.update(monkeypatch_cfg)
        original = personality.load
        personality.load = lambda: cfg
        try:
            return personality.get_system_prompt()
        finally:
            personality.load = original

    def test_default_path_matches_catalog(self):
        """默认人设路径：所有目录内表情的标注与 FACE_CATALOG 一致"""
        _check_pairs(self, self._prompt())

    def test_custom_prompt_path_matches_catalog(self):
        """custom_prompt 路径：机制附录恒定追加，其表情标注同样必须对齐"""
        prompt = self._prompt({"custom_prompt": "你是一个测试人设，只说文言文"})
        self.assertTrue(prompt.startswith("你是一个测试人设"))
        _check_pairs(self, prompt)

    def test_prose_form_updated(self):
        """散文形态（冒号后带空格，正则捕获不到）单独锁定：
        旧「:178: 是斜眼」已改为「:182: 是笑哭、:96: 是冷汗」，
        且全 prompt 不再残留任何 :178: 标注"""
        prompt = self._prompt()
        self.assertNotIn(":178:", prompt)
        self.assertIn(":96:冷汗", prompt)
        self.assertIn(":182: 是笑哭", prompt)
        self.assertIn(":96: 是冷汗", prompt)

    def test_known_mislabels_absent(self):
        """旧错位标注不得再出现"""
        for path in (self._prompt(), self._prompt({"custom_prompt": "自定义"})):
            self.assertNotIn(":178:斜眼", path)
            self.assertNotIn(":96:开心", path)
            self.assertNotIn(":28:哈哈", path)
            self.assertNotIn(":4:哭", path)
            self.assertNotIn(":24:无奈", path)


if __name__ == "__main__":
    unittest.main()
