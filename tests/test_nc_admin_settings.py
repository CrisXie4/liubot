"""管理员三件套测试：client 包装的 action 名/参数形状 + dispatch 的主人校验。

NapCat 新版 action（本地源码核对）：
- set_group_member_invite_policy {group_id: str, policy: 4 选 1}
- set_group_member_permissions {group_id: str, allow_member_*: bool 至少一项}
- set_group_new_member_history_visibility {group_id: str, visible: bool}
工具层沿 relay_db_execute 的主人校验惯例（master_instructed 或 caller 为主人）。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools
from bot.config import config
from bot.onebot_client import OneBotClient

_MASTER_QQ = "424242"  # 运行时校验用占位（非真实凭据）


def _run(coro):
    return asyncio.run(coro)


def _make_bot():
    bot = mock.MagicMock()
    bot.set_group_member_invite_policy = mock.AsyncMock(return_value=True)
    bot.set_group_member_permissions = mock.AsyncMock(return_value=True)
    bot.set_group_new_member_history_visibility = mock.AsyncMock(return_value=True)
    bot.last_fail_msg = mock.MagicMock(return_value="")
    return bot


def _call_tool(bot, name, tool_input, caller_qq=_MASTER_QQ, master_instructed=False):
    return _run(ai_tools.execute_tool(
        bot, 123, 456, name, tool_input,
        caller_qq=caller_qq, master_instructed=master_instructed,
    ))


class OwnerCheckCase(unittest.TestCase):
    """统一管理 OWNER_QQ 的备份/恢复"""

    def setUp(self):
        self._old_owner = config.OWNER_QQ
        config.OWNER_QQ = _MASTER_QQ

    def tearDown(self):
        config.OWNER_QQ = self._old_owner


class TestClientWrappers(unittest.TestCase):
    """client 方法 → action 名与参数形状（group_id 转 str，与源码 Type.String 对齐）"""

    def test_invite_policy(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        self.assertTrue(_run(client.set_group_member_invite_policy(123, "disabled")))
        client._call.assert_awaited_once_with("set_group_member_invite_policy", {
            "group_id": "123", "policy": "disabled"})

    def test_permissions_single_key_only(self):
        """只传一项时其余键不出现（NapCat 未传项保持不变）"""
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        self.assertTrue(_run(client.set_group_member_permissions(123, allow_upload_album=True)))
        client._call.assert_awaited_once_with("set_group_member_permissions", {
            "group_id": "123", "allow_member_upload_album": True})

    def test_permissions_multi_keys(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        _run(client.set_group_member_permissions(
            123, allow_temporary_session=False, allow_create_group=True))
        client._call.assert_awaited_once_with("set_group_member_permissions", {
            "group_id": "123",
            "allow_member_temporary_session": False,
            "allow_member_create_group": True,
        })

    def test_new_member_visibility(self):
        client = OneBotClient()
        client._call = mock.AsyncMock(return_value={"status": "ok"})
        self.assertTrue(_run(client.set_group_new_member_history_visibility(123, False)))
        client._call.assert_awaited_once_with("set_group_new_member_history_visibility", {
            "group_id": "123", "visible": False})


class TestOwnerGate(OwnerCheckCase):
    """主人校验：非主人直接拒绝且不调 API"""

    def test_non_owner_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_invite_policy", {"policy": "disabled"}, caller_qq=999)
        self.assertIn("主人", r)
        bot.set_group_member_invite_policy.assert_not_awaited()

    def test_owner_allowed(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_invite_policy", {"policy": "disabled"})
        self.assertIn("✓", r)
        bot.set_group_member_invite_policy.assert_awaited_once_with(123, "disabled")

    def test_master_instructed_allowed(self):
        """主人自然语言指令（master_instructed）即使 caller 传递有误也放行"""
        bot = _make_bot()
        r = _call_tool(bot, "set_new_member_visibility", {"visible": True},
                       caller_qq=999, master_instructed=True)
        self.assertIn("✓", r)
        bot.set_group_new_member_history_visibility.assert_awaited_once_with(123, True)

    def test_non_owner_permissions_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_member_permissions", {"allow_upload_album": True},
                       caller_qq=999)
        self.assertIn("主人", r)
        bot.set_group_member_permissions.assert_not_awaited()


class TestToolValidation(OwnerCheckCase):
    """入参校验（主人已通过的前提下）"""

    def test_invalid_policy_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_invite_policy", {"policy": "open"})
        self.assertIn("policy", r)
        bot.set_group_member_invite_policy.assert_not_awaited()

    def test_permissions_without_any_key_rejected(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_member_permissions", {})
        self.assertIn("至少", r)
        bot.set_group_member_permissions.assert_not_awaited()

    def test_permissions_passes_only_given_keys(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_member_permissions", {"allow_create_group": False})
        self.assertIn("✓", r)
        bot.set_group_member_permissions.assert_awaited_once_with(
            123, allow_upload_album=None,
            allow_temporary_session=None, allow_create_group=False)

    def test_visibility_requires_visible(self):
        bot = _make_bot()
        r = _call_tool(bot, "set_new_member_visibility", {})
        self.assertIn("visible", r)
        bot.set_group_new_member_history_visibility.assert_not_awaited()

    def test_visibility_false_not_treated_as_missing(self):
        """visible=False 是合法值，不能被当成缺参"""
        bot = _make_bot()
        r = _call_tool(bot, "set_new_member_visibility", {"visible": False})
        self.assertIn("✓", r)
        bot.set_group_new_member_history_visibility.assert_awaited_once_with(123, False)

    def test_set_failure_reported(self):
        bot = _make_bot()
        bot.set_group_member_invite_policy = mock.AsyncMock(return_value=False)
        r = _call_tool(bot, "set_invite_policy", {"policy": "no_approval"})
        self.assertIn("失败", r)


if __name__ == "__main__":
    unittest.main()
