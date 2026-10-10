"""新闻配图图文相关性测试（2026-09-06 书封配 FBI 新闻事故回归）。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_brain, memory


class _Resp:
    def __init__(self, text):
        self.content = [type("T", (), {"text": text})()]


class TestRankImageCandidates(unittest.TestCase):
    """候选排序：重叠高在前、无元数据垫底、svg 剔除、上限 3"""

    def test_meta_overlap_ranks_first(self):
        results = [
            {"img_src": "https://a.example/no-meta.jpg"},                       # 无元数据
            {"img_src": "https://b.example/fbi.jpg",
             "title": "FBI调查驾照泄露事件", "source": "科技日报"},             # 高重叠
            {"img_src": "https://c.example/other.jpg",
             "title": "今天天气不错", "source": "生活频道"},                    # 零重叠
        ]
        ranked = ai_brain._rank_image_candidates(
            results, "FBI查出泄露1.53亿驾照的暗网服务", "个人信息泄露")
        self.assertEqual(ranked[0], "https://b.example/fbi.jpg")
        self.assertIn("https://a.example/no-meta.jpg", ranked)   # 保留但垫底
        self.assertLess(ranked.index("https://b.example/fbi.jpg"),
                        ranked.index("https://a.example/no-meta.jpg"))

    def test_svg_and_empty_excluded(self):
        results = [
            {"img_src": "https://a.example/logo.svg", "title": "FBI 驾照"},
            {"title": "FBI 驾照"},  # 没有 url
        ]
        self.assertEqual(
            ai_brain._rank_image_candidates(results, "FBI驾照泄露暗网", ""), [])

    def test_cap_three(self):
        results = [{"img_src": f"https://x.example/{i}.jpg",
                    "title": "FBI 驾照 泄露"} for i in range(6)]
        ranked = ai_brain._rank_image_candidates(results, "FBI驾照泄露暗网", "")
        self.assertEqual(len(ranked), 3)

    def test_no_meta_never_auto_wins(self):
        """旧版元凶：无元数据直接 break 采用。现在无元数据必须排在有重叠候选之后。"""
        results = [
            {"img_src": "https://book.example/cover.jpg"},  # 书封（无元数据）
            {"img_src": "https://news.example/fbi.jpg",
             "title": "暗网服务被查", "source": "新闻"},
        ]
        ranked = ai_brain._rank_image_candidates(
            results, "FBI查泄露驾照的暗网服务", "暗网")
        self.assertEqual(ranked[-1], "https://book.example/cover.jpg")
        self.assertEqual(ranked[0], "https://news.example/fbi.jpg")


class TestVerifyImageMatch(unittest.TestCase):
    """视觉校验：MATCH/NO/异常 三种走向"""

    def setUp(self):
        self._old_settings = memory.load_model_settings
        memory.load_model_settings = lambda: {"api_key": "test-key", "model": "m"}

    def tearDown(self):
        memory.load_model_settings = self._old_settings

    def _patch_llm(self, text=None, boom=False):
        import bot.llm_call as llm_mod
        self._old_create = llm_mod.llm_create

        async def fake(**kwargs):
            if boom:
                raise RuntimeError("llm down")
            return _Resp(text)
        llm_mod.llm_create = fake
        self.addCleanup(setattr, llm_mod, "llm_create", self._old_create)

    def test_match_passes(self):
        self._patch_llm("MATCH")
        self.assertTrue(self._run_verify())

    def test_no_rejected(self):
        self._patch_llm("NO")
        self.assertFalse(self._run_verify())

    def test_llm_down_rejected_ningquewulan(self):
        self._patch_llm(boom=True)
        self.assertFalse(self._run_verify())

    def test_no_api_key_rejected(self):
        memory.load_model_settings = lambda: {"api_key": ""}
        self.assertFalse(self._run_verify())

    def _run_verify(self):
        import asyncio
        return asyncio.run(
            ai_brain._verify_image_match("aGVsbG8=", "image/jpeg",
                                         "FBI查驾照泄露", "暗网"))


if __name__ == "__main__":
    unittest.main()
