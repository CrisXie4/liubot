"""LLM 手滑 CQ 变体归一化测试 — :CQ:face,id=317: 不再裸发（2026-10-10 线上实录回归）。

模型把「快捷码 :178:」和「CQ 码 [CQ:face,id=178]」两种格式揉在一起，
产出 :CQ:face,id=317: 这类四不像，解析不出来被当纯文字发进群。
发送层 sanitize_cq_codes 现在会先把常见变体归一化成合法码。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.onebot_client import (
    _fix_cq_typos,
    cq_text_to_segments,
    sanitize_cq_codes,
)


class TestCqTypos(unittest.TestCase):

    def test_incident_hybrid_colon_wrapped(self):
        """线上实录：:CQ:face,id=317: 得咧 → 归一化成合法 face 码"""
        text = ":CQ:face,id=317: 得咧 六哥 这个够不够味"
        fixed = _fix_cq_typos(text)
        self.assertIn("[CQ:face,id=317]", fixed)
        self.assertIn("得咧 六哥", fixed)
        self.assertNotIn(":CQ:", fixed)

    def test_hybrid_without_leading_colon(self):
        """漏写前括号的变体：CQ:face,id=182: → 补成合法码"""
        fixed = _fix_cq_typos("来一个 CQ:face,id=182: 看看")
        self.assertIn("[CQ:face,id=182]", fixed)

    def test_fullwidth_brackets_and_punctuation(self):
        """全角括号+全角标点：【CQ：face，id=182】 → 合法码"""
        fixed = _fix_cq_typos("笑死【CQ：face，id=182】")
        self.assertIn("[CQ:face,id=182]", fixed)
        self.assertNotIn("【", fixed)

    def test_fullwidth_brackets_halfwidth_inside(self):
        """全角括号、码内半角：【CQ:face,id=182】 → 合法码"""
        self.assertIn("[CQ:face,id=182]", _fix_cq_typos("【CQ:face,id=182】"))

    def test_wellformed_code_idempotent(self):
        """本来合法的码原样保留"""
        text = "哈哈[CQ:face,id=182]笑死"
        self.assertEqual(_fix_cq_typos(text), text)

    def test_time_not_touched(self):
        """时间 12:30:45 不受影响（无 CQ 字面量不触发）"""
        text = "我们 12:30:45 见"
        self.assertEqual(_fix_cq_typos(text), text)

    def test_plain_sentence_not_touched(self):
        """普通带冒号的句子不误伤"""
        text = "提示：记得带伞:明天有雨"
        self.assertEqual(_fix_cq_typos(text), text)

    def test_sanitize_integrates_typo_fix(self):
        """发送层 sanitize 链路：变体先归一化、残码补全都还在"""
        self.assertIn("[CQ:face,id=317]", sanitize_cq_codes(":CQ:face,id=317: 得咧"))
        self.assertIn("[CQ:face,id=14]", sanitize_cq_codes("[CQ:face,id=14"))

    def test_end_to_end_segments(self):
        """整条发送链：变体文本最终产出 face 消息段（id 转整数）而非裸文字"""
        segs = cq_text_to_segments(":CQ:face,id=317: 得咧 六哥 这个够不够味")
        face_segs = [s for s in segs if s.get("type") == "face"]
        self.assertTrue(face_segs, "应产出 face 段而不是纯文本")
        self.assertEqual(face_segs[0]["data"]["id"], 317)
        texts = "".join(s.get("data", {}).get("text", "") for s in segs if s.get("type") == "text")
        self.assertIn("得咧 六哥", texts)


if __name__ == "__main__":
    unittest.main()
