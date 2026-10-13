"""分享卡片（json/xml 段）解析测试 — python tests/test_card_parse.py"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader


def _run(coro):
    return asyncio.run(coro)


# B站分享卡片（json 段真实结构，中文以 \uXXXX 转义形态出现——正是
# 裸扫 raw_message 永远漏掉它的原因）
BILI_JSON = (
    '{"config":{"forward":true,"type":"normal"},"meta":{"news":{'
    '"action":"","app_type":1,"appid":100951776,'
    '"desc":"\\u624b\\u673a\\u6e38\\u620f\\u4ee3\\u7ec3 \\u65e5\\u7ed3 \\u514d\\u8d39\\u9886\\u53d6",'
    '"jumpUrl":"https://b23.tv/abc123",'
    '"preview":"https://i0.hdslb.com/bfs/archive/x.jpg",'
    '"title":"\\u9707\\u60ca!\\u5168\\u670d\\u9996\\u53f7"},'
    '"appinfo":{"appName":"QQ小程序"}},"prompt":"[分享]震惊!全服首号"}'
)


class TestParseJsonCard(unittest.TestCase):

    def test_news_card_decoded(self):
        """\\uXXXX 转义的中文被解码成可读文本"""
        card = content_reader._parse_json_card(BILI_JSON)
        self.assertIsNotNone(card)
        self.assertEqual(card["title"], "震惊!全服首号")
        self.assertIn("免费领取", card["desc"])
        self.assertEqual(card["url"], "https://b23.tv/abc123")

    def test_miniapp_card(self):
        obj = ('{"meta":{"miniapp":{"title":"拼好饭","desc":"1分钱吃大餐",'
               '"jumpUrl":"https://pdd.example/x"}},"appname":"拼多多"}')
        card = content_reader._parse_json_card(obj)
        self.assertEqual(card["title"], "拼好饭")
        self.assertEqual(card["source"], "拼多多")

    def test_no_meta_falls_back_to_top_level(self):
        obj = '{"title":"裸卡片","jumpUrl":"https://x.example/","desc":"d"}'
        card = content_reader._parse_json_card(obj)
        self.assertEqual(card["title"], "裸卡片")
        self.assertEqual(card["url"], "https://x.example/")

    def test_invalid_returns_none(self):
        self.assertIsNone(content_reader._parse_json_card("not json {"))
        self.assertIsNone(content_reader._parse_json_card(""))
        self.assertIsNone(content_reader._parse_json_card('{"foo":1}'))


class TestParseXmlCard(unittest.TestCase):

    def test_attrs_extracted(self):
        xml = ('<?xml version="1.0"?><msg appmsg="6" '
               'title="活动推广" des="加群 免费领取" '
               'url="https://spam.example/page?action=1&amp;x=2"/>')
        card = content_reader._parse_xml_card(xml)
        self.assertEqual(card["title"], "活动推广")
        self.assertIn("加群", card["desc"])
        self.assertEqual(card["url"], "https://spam.example/page?action=1&x=2")

    def test_empty_returns_none(self):
        self.assertIsNone(content_reader._parse_xml_card("<msg/>"))
        self.assertIsNone(content_reader._parse_xml_card(""))


class TestFormatCardsText(unittest.TestCase):

    def test_full_shape(self):
        text = content_reader.format_cards_text(
            [{"title": "标题", "desc": "简介", "url": "https://x.example/",
              "source": "B站"}])
        self.assertIn("[分享卡片]", text)
        self.assertIn("《标题》", text)
        self.assertIn("简介", text)
        self.assertIn("链接:https://x.example/", text)
        self.assertIn("(来源:B站)", text)

    def test_empty_list(self):
        self.assertEqual(content_reader.format_cards_text([]), "")
        self.assertEqual(content_reader.format_cards_text(None), "")

    def test_caps_at_three(self):
        cards = [{"title": f"卡{i}"} for i in range(5)]
        text = content_reader.format_cards_text(cards)
        self.assertIn("卡2", text)
        self.assertNotIn("卡3", text)


class TestExtractFullContentCards(unittest.TestCase):

    def test_json_segment_parsed(self):
        segs = [{"type": "text", "data": {"text": "看看这个"}},
                {"type": "json", "data": {"data": BILI_JSON}}]
        content = _run(content_reader.extract_full_content(None, "", segs))
        self.assertEqual(len(content["cards"]), 1)
        self.assertEqual(content["cards"][0]["title"], "震惊!全服首号")

    def test_raw_message_fallback(self):
        """segments 为空时从 raw_message 抠卡片（CQ 转义形态）"""
        raw = "[CQ:json,data=" + BILI_JSON.replace("&", "&amp;") + "]"
        content = _run(content_reader.extract_full_content(None, raw, []))
        self.assertEqual(len(content["cards"]), 1)
        self.assertEqual(content["cards"][0]["title"], "震惊!全服首号")

    def test_card_inside_forward(self):
        """转发包里的卡片进转发文本"""
        class _Bot:
            async def get_forward_msg(self, id=0):
                return {"messages": [
                    {"nickname": "甲", "message": [
                        {"type": "json", "data": {"data": BILI_JSON}}]},
                ]}
        bot = _Bot()
        raw = "[CQ:forward,id=abc]"
        content = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertTrue(content["has_forward"])
        self.assertIn("[分享卡片]", content["forward"]["text"])
        self.assertIn("震惊!全服首号", content["forward"]["text"])


class TestVisibleMessageText(unittest.TestCase):

    def test_card_appended(self):
        from bot import handler
        txt = handler._visible_message_text(
            "看看这个 [CQ:json,data=xxx]", None,
            "[分享卡片]《震惊》链接:https://x.example/")
        self.assertIn("看看这个", txt)
        self.assertIn("[分享卡片]《震惊》", txt)

    def test_no_cards_unchanged(self):
        from bot import handler
        self.assertEqual(handler._visible_message_text("plain", None, ""), "plain")


class TestQuotedCardContext(unittest.TestCase):

    def test_quoted_json_card_exposed_to_ai(self):
        """引用一条卡片消息 → card_text 出现在 AI 可读上下文里"""
        from bot import quote_handler

        class _Bot:
            async def get_msg(self, mid):
                return {
                    "raw_message": "[CQ:json,data=" + BILI_JSON.replace("&", "&amp;") + "]",
                    "sender": {"user_id": 42, "card": "张三"},
                }

        ctx = _run(quote_handler.get_quoted_context(_Bot(), 1001))
        self.assertIsNotNone(ctx)
        self.assertIn("震惊!全服首号", ctx.get("card_text", ""))
        formatted = quote_handler.format_quoted_context_for_ai(ctx)
        self.assertIn("[分享卡片]", formatted)
        self.assertIn("震惊!全服首号", formatted)


if __name__ == "__main__":
    unittest.main()
