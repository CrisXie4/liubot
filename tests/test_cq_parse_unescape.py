"""CQ 参数解析重构测试 — url 不被切碎 + 值反转义（收藏动图链路回归）。

旧实现全局找 key=，URL 里的 appid=/fileid= 被误判成新参数，
url 在 appid= 处截断且 &amp; 不反转义 → 表情包动图发不出去、
过期表情删不掉。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.onebot_client import (
    _cq_contains_url,
    _parse_cq_params,
    cq_text_to_segments,
)


class TestParseCqParams(unittest.TestCase):

    def test_url_not_truncated_and_unescaped(self):
        """完整图片 URL（含 appid=/fileid= 与 &amp; 转义）不被切碎且反转为真 &"""
        params = _parse_cq_params(
            "file=a.jpg,subType=0,"
            "url=https://multimedia.nt.qq.com.cn/download?appid=1&amp;fileid=2&amp;spec=0"
        )
        self.assertEqual(params["file"], "a.jpg")
        self.assertEqual(params["subType"], "0")
        self.assertEqual(
            params["url"],
            "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=2&spec=0",
        )

    def test_space_after_comma_split(self):
        """逗号后带空格的参数照常切分，值里的空格保留（评审回归用例）"""
        params = _parse_cq_params("id=14, text=hi there")
        self.assertEqual(params, {"id": "14", "text": "hi there"})

    def test_simple_codes_unchanged(self):
        """face / at / reply 简单码解析不变"""
        self.assertEqual(_parse_cq_params("id=14"), {"id": "14"})
        self.assertEqual(_parse_cq_params("qq=10086"), {"qq": "10086"})
        self.assertEqual(_parse_cq_params("id=42"), {"id": "42"})


class TestCqTextToSegments(unittest.TestCase):

    def test_image_url_full_with_real_amp(self):
        """image 码经 cq_text_to_segments 后 url 完整且为真 &"""
        cq = ("[CQ:image,file=a.jpg,subType=0,"
              "url=https://multimedia.nt.qq.com.cn/download?appid=1&amp;fileid=2&amp;spec=0]")
        segs = cq_text_to_segments(cq)
        self.assertEqual(segs[0]["type"], "image")
        self.assertEqual(
            segs[0]["data"]["url"],
            "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=2&spec=0",
        )

    def test_face_id_int(self):
        segs = cq_text_to_segments("[CQ:face,id=14]")
        self.assertEqual(segs, [{"type": "face", "data": {"id": 14}}])

    def test_at_non_numeric_qq_stays_str(self):
        segs = cq_text_to_segments("[CQ:at,qq=x]")
        self.assertEqual(segs, [{"type": "at", "data": {"qq": "x"}}])

    def test_reply_id_int(self):
        segs = cq_text_to_segments("[CQ:reply,id=999]")
        self.assertEqual(segs, [{"type": "reply", "data": {"id": 999}}])

    def test_text_around_cq_kept(self):
        segs = cq_text_to_segments("哈哈[CQ:face,id=96]")
        self.assertEqual(segs[0], {"type": "text", "data": {"text": "哈哈"}})
        self.assertEqual(segs[1], {"type": "face", "data": {"id": 96}})


class TestCqContainsUrl(unittest.TestCase):

    URL = "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=2&spec=0"

    def test_escaped_cq_matches(self):
        """cq_code 内是 &amp; 转义时也能命中（过期表情可删）"""
        cq = f"[CQ:image,file=a.jpg,url={self.URL.replace('&', '&amp;')}]"
        self.assertTrue(_cq_contains_url(cq, self.URL))

    def test_plain_cq_matches(self):
        """cq_code 内已是明文 & 也命中（html.unescape 幂等）"""
        cq = f"[CQ:image,file=a.jpg,url={self.URL}]"
        self.assertTrue(_cq_contains_url(cq, self.URL))

    def test_unrelated_url(self):
        cq = "[CQ:image,file=a.jpg,url=https://example.com/a.jpg]"
        self.assertFalse(_cq_contains_url(cq, self.URL))

    def test_empty_inputs(self):
        self.assertFalse(_cq_contains_url("", self.URL))
        self.assertFalse(_cq_contains_url("[CQ:image]", ""))


if __name__ == "__main__":
    unittest.main()
