"""dice/rps/music 消息段接入测试。

背景：NapCat 支持 {"type":"dice"}/{"type":"rps"}（发送侧忽略参数、结果随机）
和 {"type":"music",...}（平台点歌 + custom 自定义卡，custom 型 url/image 必填）。
锁定三件事：
1. CQ 码路径能透传无参码 [CQ:dice]/[CQ:rps] 与 [CQ:music,...]（不被当纯文本剥掉）；
2. play_dice/play_rps 直接发送（2026-10-10 改版：不再返回码等模型拼接）；
3. send_music 的平台白名单 / song_id 必填 / custom 型 url+image 必填且 http 开头。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools
from bot.onebot_client import (
    OneBotClient,
    cq_text_to_segments,
    has_cq_codes,
    sanitize_cq_codes,
)


def _run(coro):
    return asyncio.run(coro)


def _call_tool(bot, name, tool_input, group_id=123):
    return _run(ai_tools.execute_tool(bot, group_id, 456, name, tool_input))


def _make_bot():
    bot = mock.MagicMock()
    bot.send_group_msg = mock.AsyncMock(return_value=12345)
    bot.send_private_msg = mock.AsyncMock(return_value=12345)
    bot.last_fail_msg = mock.MagicMock(return_value="")
    bot.send_group_music = mock.AsyncMock(return_value=True)
    bot.last_fail_msg = mock.MagicMock(return_value="")
    return bot


class TestCqPassthrough(unittest.TestCase):
    """dice/rps/music 的 CQ 码不被剥离、正确转成消息段"""

    def test_dice_no_params_to_segment(self):
        self.assertTrue(has_cq_codes("[CQ:dice]"))
        self.assertEqual(cq_text_to_segments("[CQ:dice]"),
                         [{"type": "dice", "data": {}}])

    def test_rps_no_params_to_segment(self):
        self.assertEqual(cq_text_to_segments("[CQ:rps]"),
                         [{"type": "rps", "data": {}}])

    def test_dice_mixed_with_text(self):
        segs = cq_text_to_segments("我掷个骰子[CQ:dice]比比看")
        self.assertEqual(segs[0], {"type": "text", "data": {"text": "我掷个骰子"}})
        self.assertEqual(segs[1]["type"], "dice")
        self.assertEqual(segs[2], {"type": "text", "data": {"text": "比比看"}})

    def test_music_platform_to_segment(self):
        segs = cq_text_to_segments("[CQ:music,type=qq,id=112211]")
        self.assertEqual(segs, [{"type": "music", "data": {"type": "qq", "id": "112211"}}])

    def test_sanitize_keeps_dice(self):
        """残码修复逻辑不误伤完整的无参码"""
        self.assertEqual(sanitize_cq_codes("[CQ:dice]"), "[CQ:dice]")
        self.assertEqual(sanitize_cq_codes("哈[CQ:rps]"), "哈[CQ:rps]")

    def test_old_param_codes_unchanged(self):
        """原有带参码解析不受正则放宽影响（at 的纯数字 qq 走既有 int 化）"""
        self.assertEqual(cq_text_to_segments("[CQ:face,id=14]"),
                         [{"type": "face", "data": {"id": 14}}])
        self.assertEqual(cq_text_to_segments("[CQ:at,qq=10086]"),
                         [{"type": "at", "data": {"qq": 10086}}])


class TestDiceRpsTools(unittest.TestCase):
    """play_dice / play_rps：直接发送（返回给模型的是「已发出」说明）"""

    def test_play_dice_sent_directly(self):
        bot = _make_bot()
        r = _call_tool(bot, "play_dice", {})
        self.assertIn("骰子已直接发出", r)
        self.assertNotIn("[CQ:", r)
        bot.send_group_msg.assert_called_once_with(123, "[CQ:dice]")

    def test_play_rps_sent_directly(self):
        bot = _make_bot()
        r = _call_tool(bot, "play_rps", {})
        self.assertIn("猜拳已直接发出", r)
        self.assertNotIn("[CQ:", r)
        bot.send_group_msg.assert_called_once_with(123, "[CQ:rps]")

    def test_play_dice_send_failure_feedback(self):
        """发送失败时把原因回传给模型，而不是假装成功"""
        bot = _make_bot()
        bot.send_group_msg = mock.AsyncMock(return_value=None)
        r = _call_tool(bot, "play_dice", {})
        self.assertIn("⨯", r)
        self.assertIn("失败", r)

    def test_dice_tool_registered(self):
        names = {t["name"] for t in ai_tools.ALL_TOOLS}
        self.assertIn("play_dice", names)
        self.assertIn("play_rps", names)


class TestSendMusicTool(unittest.TestCase):
    """send_music：平台点歌 + custom 自定义卡校验"""

    def test_platform_song_ok(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {"platform": "qq", "song_id": "112211"})
        self.assertIn("✓", r)
        bot.send_group_music.assert_awaited_once_with(
            123, {"type": "qq", "id": "112211"})

    def test_platform_whitelist(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {"platform": "spotify", "song_id": "1"})
        self.assertIn("platform", r)
        bot.send_group_music.assert_not_awaited()

    def test_platform_without_song_id(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {"platform": "163"})
        self.assertIn("song_id", r)
        bot.send_group_music.assert_not_awaited()

    def test_custom_fabricated_url_rejected(self):
        """custom 型防 AI 乱编链接：非 http 开头直接拒绝"""
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "platform": "custom", "title": "我的歌",
            "url": "看看这个好听的歌", "image": "https://a.example/x.jpg",
        })
        self.assertIn("http", r)
        bot.send_group_music.assert_not_awaited()

    def test_custom_missing_url_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "platform": "custom", "title": "我的歌",
            "image": "https://a.example/x.jpg",
        })
        self.assertIn("url", r)
        bot.send_group_music.assert_not_awaited()

    def test_custom_image_required(self):
        """NapCat 源码要求 custom 型 image 必填（签名服务会拒绝无封面卡）"""
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "platform": "custom", "title": "我的歌",
            "url": "https://music.example/1",
        })
        self.assertIn("image", r)
        bot.send_group_music.assert_not_awaited()

    def test_custom_image_fabricated_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "platform": "custom", "title": "我的歌",
            "url": "https://music.example/1", "image": "封面图随便一张",
        })
        self.assertIn("image", r)
        bot.send_group_music.assert_not_awaited()

    def test_custom_ok_full(self):
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "platform": "custom", "title": "晴天",
            "url": "https://music.example/1",
            "image": "https://img.example/cover.jpg",
            "audio": "https://audio.example/1.mp3",
        })
        self.assertIn("✓", r)
        bot.send_group_music.assert_awaited_once_with(123, {
            "type": "custom", "url": "https://music.example/1",
            "image": "https://img.example/cover.jpg",
            "title": "晴天", "audio": "https://audio.example/1.mp3",
        })

    def test_url_without_platform_falls_to_custom(self):
        """只给了 url 没给 song_id → 视作 custom 型"""
        bot = _make_bot()
        r = _call_tool(bot, "send_music", {
            "title": "晴天", "url": "https://music.example/1",
            "image": "https://img.example/cover.jpg",
        })
        self.assertIn("✓", r)
        bot.send_group_music.assert_awaited_once()
        data = bot.send_group_music.await_args.args[1]
        self.assertEqual(data["type"], "custom")

    def test_send_failure_reported(self):
        bot = _make_bot()
        bot.send_group_music = mock.AsyncMock(return_value=False)
        r = _call_tool(bot, "send_music", {"platform": "qq", "song_id": "1"})
        self.assertIn("失败", r)


class TestSendGroupMusicClient(unittest.TestCase):
    """client 包装：action 名与参数形状"""

    def test_action_name_and_params(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        ok = _run(client.send_group_music(123, {"type": "qq", "id": "1"}))
        self.assertTrue(ok)
        client._call.assert_awaited_once_with("send_group_msg", {
            "group_id": 123,
            "message": [{"type": "music", "data": {"type": "qq", "id": "1"}}],
        })

    def test_failure_returns_false(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "failed", "retcode": 400})
        self.assertFalse(_run(client.send_group_music(123, {"type": "custom"})))


if __name__ == "__main__":
    unittest.main()
