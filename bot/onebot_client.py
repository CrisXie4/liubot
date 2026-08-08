"""
OneBot v11 协议客户端
- 纯 WebSocket 模式（事件接收 + API 调用）
- 单 reader 线程，消息自动路由到 API 回调或事件处理器
- 支持 CQ 码自动转换为消息段
"""
import asyncio
import json
import logging
import re
import traceback
import uuid
from typing import Callable, Optional

import websockets

# 兼容旧版 websockets（无 asyncio.client 子模块）
try:
    from websockets.asyncio.client import ClientConnection
except ImportError:
    ClientConnection = None

from .config import config

logger = logging.getLogger("qqbot")

# ─── CQ 码 → 消息段转换 ───
_CQ_RE = re.compile(r"\[CQ:(\w+),([^\]]*?)\]")
# 用于分段的 regex
_CQ_SPLIT_RE = re.compile(r"(\[CQ:\w+,[^\]]*?\])")


def _parse_cq_params(params_str: str) -> dict:
    """解析 CQ 码参数: key=value,key2=value2 → {key: value, key2: value2}"""
    data = {}
    # 智能分割: 找所有 key= 的位置避免值中的 = 和 , 干扰
    key_re = re.compile(r",?\s*(\w+)\s*=")
    last_end = 0
    last_key = None
    for km in key_re.finditer(params_str):
        if last_key:
            val = params_str[last_end:km.start()].strip().rstrip(",")
            data[last_key] = val
        last_key = km.group(1)
        last_end = km.end()
    if last_key:
        val = params_str[last_end:].strip().rstrip(",")
        data[last_key] = val
    return data


def cq_text_to_segments(text: str) -> list[dict]:
    """
    将包含 CQ 码的文本转换为 NapCat 消息段数组。
    "哈哈[CQ:face,id=96]" →
      [{"type":"text","data":{"text":"哈哈"}}, {"type":"face","data":{"id":96}}]
    """
    parts = _CQ_SPLIT_RE.split(text)
    segments = []
    for part in parts:
        if not part:
            continue
        if part.startswith("[CQ:"):
            m = _CQ_RE.match(part)
            if m:
                cq_type = m.group(1)
                params = _parse_cq_params(m.group(2))
                # NapCat 需要整数类型的 id/qq/duration 等
                _fix_param_types(cq_type, params)
                segments.append({"type": cq_type, "data": params})
            else:
                segments.append({"type": "text", "data": {"text": part}})
        else:
            segments.append({"type": "text", "data": {"text": part}})
    return segments if segments else [{"type": "text", "data": {"text": text}}]


def _fix_param_types(cq_type: str, params: dict):
    """修正参数类型：NapCat 期望整数类型的字段"""
    int_fields = {
        "face": ["id"],
        "at": ["qq"],
        "image": [],
        "mface": [],
        "record": [],
        "video": [],
        "file": [],
        "reply": ["id"],
        "poke": [],
    }
    fields = int_fields.get(cq_type, [])
    for f in fields:
        if f in params and isinstance(params[f], str) and params[f].isdigit():
            params[f] = int(params[f])


def has_cq_codes(text: str) -> bool:
    """检查文本是否包含 CQ 码或快捷表情"""
    return bool(_CQ_RE.search(text)) or bool(re.search(r':(\d{1,4}):', text))


# ─── 快捷表情替换 ───
# {1,4} 覆盖 4 位大表情 id（如 392 龙年快乐、432 灵蛇献瑞）。
# 旧的 {1,3} 只到 3 位，导致 :392: 这类大表情快捷码发不出去。
_FACE_SHORTCUT_RE = re.compile(r':(\d{1,4}):')

def expand_face_shortcuts(text: str) -> str:
    """将 :14: 快捷格式转为 [CQ:face,id=14]"""
    return _FACE_SHORTCUT_RE.sub(r'[CQ:face,id=\1]', text)


# ─── CQ 码消毒 ───
# LLM 生成的回复常在结尾漏掉 CQ 码的闭合 "]"（如 "[CQ:face,id=14"），
# cq_text_to_segments 只认闭合完整的码，残码会被当纯文本发出去，
# 群里就会看到裸露的 "[CQ:face,id=14" 字样。发送前统一修复/清理。
_DANGLING_FACE_RE = re.compile(r'(\[CQ:face,id=\d{1,4})(?![\d\],=])')

def sanitize_cq_codes(text: str) -> str:
    """修复残缺 CQ 码: face 码缺 ] 则补全；其他修不了的残码整段剥除"""
    # 1) face 码缺 ]：id 是纯数字，数字结束处就是码的边界，直接补 ]
    text = _DANGLING_FACE_RE.sub(r'\1]', text)
    # 2) 其余 [CQ: 开头但到结尾都没有 ] 的片段：丢弃（无法安全补全）
    out, i = [], 0
    while True:
        j = text.find('[CQ:', i)
        if j == -1:
            out.append(text[i:])
            break
        k = text.find(']', j)
        if k == -1:
            out.append(text[i:j])  # 残码从 j 起全部剥掉
            break
        out.append(text[i:k + 1])
        i = k + 1
    return ''.join(out).rstrip()


class OneBotClient:

    def __init__(self):
        self.ws_url = config.NAPCAT_WS_URL
        self.ws_token = config.NAPCAT_WS_TOKEN
        self._ws = None  # websockets 连接
        self._handlers: list[Callable] = []
        self._running = False
        self.bot_qq: str = config.BOT_QQ
        self.self_id: str = ""
        # echo -> Future 映射（WS API 调用回执）
        self._pending: dict[str, asyncio.Future] = {}

    # ==================== API 调用 ====================

    async def _call_http(self, action: str, params: dict) -> dict:
        """HTTP fallback: 通过 NapCat HTTP API 调用（用于 WS 不支持或失败的 action）"""
        import aiohttp
        http_url = config.NAPCAT_HTTP_URL.rstrip("/")
        token = config.NAPCAT_HTTP_TOKEN
        url = f"{http_url}/{action}"
        headers = {"Content-Type": "application/json"}
        if token and token not in ("your_http_token_here", ""):
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=params, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.debug(f"HTTP API [{action}] 成功")
                        return data
                    text = await resp.text()
                    logger.warning(f"HTTP API [{action}] 失败: {resp.status} {text[:200]}")
                    return {"status": "failed", "retcode": resp.status, "msg": text[:200]}
        except Exception as e:
            logger.warning(f"HTTP API [{action}] 异常: {e}")
            return {"status": "failed", "retcode": -1, "msg": str(e)}

    async def _call(self, action: str, params: dict) -> dict:
        """通过 WebSocket 调用 OneBot API（失败时自动走 HTTP fallback）"""
        if self._ws is None:
            return await self._call_http(action, params)

        echo = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        try:
            await self._ws.send(json.dumps({
                "action": action, "params": params, "echo": echo,
            }, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=15)
            if result.get("status") == "ok":
                return result
            # WS 返回失败 → 尝试 HTTP fallback
            logger.debug(f"WS API [{action}] 返回失败(retcode={result.get('retcode')}), 尝试HTTP")
            http_result = await self._call_http(action, params)
            if http_result.get("status") == "ok":
                return http_result
            return result  # HTTP 也失败，返回 WS 结果（含错误信息）
        except asyncio.TimeoutError:
            logger.warning(f"API 超时 [{action}]")
            return {"status": "failed", "retcode": -1, "msg": "timeout"}
        except Exception as e:
            return {"status": "failed", "retcode": -1, "msg": str(e)}
        finally:
            self._pending.pop(echo, None)

    # ---- 消息 ----

    async def send_group_msg(self, group_id: int, message: str | list) -> Optional[int]:
        if isinstance(message, str):
            # 修复/清理残缺 CQ 码（LLM 常漏写闭合 ]）
            if '[CQ:' in message:
                message = sanitize_cq_codes(message)
            # 展开快捷表情 :14: → [CQ:face,id=14]
            if ':' in message:
                message = expand_face_shortcuts(message)
            if has_cq_codes(message):
                message = cq_text_to_segments(message)
            else:
                message = [{"type": "text", "data": {"text": message}}]

        # 先尝试完整发送（含图片/mface）
        r = await self._call("send_group_msg", {"group_id": group_id, "message": message})
        if r.get("status") == "ok":
            try:
                from . import ai_brain
                ai_brain.track_bot_activity()
            except Exception:
                pass
            return r["data"]["message_id"]

        # retcode 1200 通常是限频 → 等一等重试一次
        retcode = r.get("retcode", 0)
        if retcode == 1200:
            import asyncio as _asyncio
            await _asyncio.sleep(2.5)
            r = await self._call("send_group_msg", {"group_id": group_id, "message": message})
            if r.get("status") == "ok":
                try:
                    from . import ai_brain
                    ai_brain.track_bot_activity()
                except Exception:
                    pass
                return r["data"]["message_id"]

        # 失败 → 剥离 image/mface 重试，同时清理失效表情
        text_only = [s for s in message if s.get("type") not in ("image", "mface", "video", "record", "file")]
        if text_only and text_only != message:
            # 后台清理失效的 image 表情
            from . import sticker as _sticker
            for seg in message:
                if seg.get("type") == "image":
                    img_url = seg.get("data", {}).get("url", "")
                    # 找到对应 sticker 并删除
                    data = _sticker._load_stickers()
                    to_del = []
                    for sid, s in data.items():
                        cq = s.get("cq_code", "")
                        if img_url and img_url in cq:
                            to_del.append(int(sid))
                    for sid in to_del:
                        _sticker.remove_sticker(sid)
                        logger.info(f"自动删除过期表情 #{sid}")
                    break  # 只处理第一个失败的
            r2 = await self._call("send_group_msg", {"group_id": group_id, "message": text_only})
            if r2.get("status") == "ok":
                try:
                    from . import ai_brain
                    ai_brain.track_bot_activity()
                except Exception:
                    pass
                return r2["data"]["message_id"]

        # 再失败 → 纯文本
        plain = [s for s in message if s.get("type") == "text"]
        if plain and plain != text_only:
            r3 = await self._call("send_group_msg", {"group_id": group_id, "message": plain})
            if r3.get("status") == "ok":
                try:
                    from . import ai_brain
                    ai_brain.track_bot_activity()
                except Exception:
                    pass
                return r3["data"]["message_id"]

        logger.warning(f"发送群消息失败: {r}")
        return None

    async def send_private_msg(self, user_id: int, message: str | list) -> Optional[int]:
        if isinstance(message, str):
            if '[CQ:' in message:
                message = sanitize_cq_codes(message)
            if ':' in message:
                message = expand_face_shortcuts(message)
            if has_cq_codes(message):
                message = cq_text_to_segments(message)
            else:
                message = [{"type": "text", "data": {"text": message}}]
        r = await self._call("send_private_msg", {"user_id": user_id, "message": message})
        if r.get("status") == "ok":
            return r["data"]["message_id"]

        # retcode 1200 限频 → 重试
        if r.get("retcode") == 1200:
            await asyncio.sleep(2.5)
            r = await self._call("send_private_msg", {"user_id": user_id, "message": message})
            if r.get("status") == "ok":
                return r["data"]["message_id"]

        # 失败 → 去媒体重试
        safe = [s for s in message if s.get("type") not in ("image", "mface", "video", "record", "file")]
        if safe and safe != message:
            r2 = await self._call("send_private_msg", {"user_id": user_id, "message": safe})
            if r2.get("status") == "ok":
                return r2["data"]["message_id"]
        return None

    async def delete_msg(self, message_id: int) -> bool:
        """撤回消息"""
        r = await self._call("delete_msg", {"message_id": message_id})
        return r.get("status") == "ok"

    async def get_msg(self, message_id: int) -> Optional[dict]:
        r = await self._call("get_msg", {"message_id": message_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_info(self, group_id: int) -> Optional[dict]:
        r = await self._call("get_group_info", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_member_info(self, group_id: int, user_id: int) -> Optional[dict]:
        r = await self._call("get_group_member_info", {"group_id": group_id, "user_id": user_id})
        return r["data"] if r.get("status") == "ok" else None

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 600) -> bool:
        r = await self._call("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration})
        return r.get("status") == "ok"

    # ---- 群管理 ----

    async def set_group_whole_ban(self, group_id: int, enable: bool = True) -> bool:
        """全员禁言"""
        r = await self._call("set_group_whole_ban", {"group_id": group_id, "enable": enable})
        return r.get("status") == "ok"

    async def set_group_admin(self, group_id: int, user_id: int, enable: bool = True) -> bool:
        """设置/取消管理员"""
        r = await self._call("set_group_admin", {"group_id": group_id, "user_id": user_id, "enable": enable})
        ok = r.get("status") == "ok"
        if not ok:
            logger.warning(f"set_group_admin 失败: group={group_id} user={user_id} enable={enable} retcode={r.get('retcode')} msg={r.get('msg','')} resp={r}")
        return ok

    async def set_group_card(self, group_id: int, user_id: int, card: str) -> bool:
        """设置群名片"""
        r = await self._call("set_group_card", {"group_id": group_id, "user_id": user_id, "card": card})
        return r.get("status") == "ok"

    async def set_group_special_title(self, group_id: int, user_id: int, special_title: str = "") -> bool:
        """设置群头衔"""
        r = await self._call("set_group_special_title", {
            "group_id": str(group_id), "user_id": str(user_id),
            "special_title": special_title,
        })
        return r.get("status") == "ok"

    async def send_group_notice(self, group_id: int, content: str, image: str = "", pinned: bool = False) -> bool:
        """发布群公告。image=图片URL(可选) pinned=是否置顶(可选)"""
        params: dict = {"group_id": str(group_id), "content": content}
        if image:
            params["image"] = image
        if pinned:
            params["pinned"] = pinned
        r = await self._call("_send_group_notice", params)
        return r.get("status") == "ok"

    async def set_essence_msg(self, message_id: int) -> bool:
        """设为精华消息"""
        r = await self._call("set_essence_msg", {"message_id": message_id})
        return r.get("status") == "ok"

    async def delete_essence_msg(self, message_id: int) -> bool:
        """移除精华消息"""
        r = await self._call("delete_essence_msg", {"message_id": message_id})
        return r.get("status") == "ok"

    async def send_like(self, user_id: int, times: int = 1) -> bool:
        """点赞"""
        r = await self._call("send_like", {"user_id": user_id, "times": times})
        return r.get("status") == "ok"

    async def send_group_poke(self, group_id: int, user_id: int) -> bool:
        """群戳一戳"""
        r = await self._call("group_poke", {"group_id": group_id, "user_id": user_id})
        return r.get("status") == "ok"

    async def send_friend_poke(self, user_id: int) -> bool:
        """好友戳一戳"""
        r = await self._call("friend_poke", {"user_id": user_id})
        return r.get("status") == "ok"

    async def get_group_honor_info(self, group_id: int, type: str = "all") -> dict:
        """群荣誉：talkative/performer/legend/strong_newbie/emotion"""
        r = await self._call("get_group_honor_info", {"group_id": group_id, "type": type})
        return r.get("data", {}) if r.get("status") == "ok" else {}

    async def get_group_member_list(self, group_id: int) -> list:
        """获取群成员列表"""
        r = await self._call("get_group_member_list", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def set_group_name(self, group_id: int, name: str) -> bool:
        """修改群名"""
        r = await self._call("set_group_name", {"group_id": group_id, "group_name": name})
        return r.get("status") == "ok"

    async def set_online_status(self, status: int = 10, ext_status: int = 0, battery_status: int = 0) -> bool:
        """设置在线状态。status: 10=在线 30=离开 40=隐身 50=忙碌。ext_status/battery_status 为 NapCat WS 必填字段。"""
        r = await self._call("set_online_status", {
            "status": status,
            "ext_status": ext_status,
            "battery_status": battery_status,
        })
        ok = r.get("status") == "ok" or r.get("retcode") == 0
        if not ok:
            logger.warning(f"NapCat set_online_status 失败: {r}")
        return ok

    async def set_self_longnick(self, long_nick: str) -> bool:
        """修改个性签名"""
        r = await self._call("set_self_longnick", {"longNick": long_nick})
        ok = r.get("status") == "ok" or r.get("retcode") == 0
        if not ok:
            logger.warning(f"NapCat set_self_longnick 失败: {r}")
        return ok

    async def set_qq_profile(self, nickname: str, signature: str = "") -> bool:
        """修改QQ资料（nickname 必填）"""
        params: dict = {"nickname": nickname}
        if signature:
            params["personal_note"] = signature
        r = await self._call("set_qq_profile", params)
        ok = r.get("status") == "ok" or r.get("retcode") == 0
        if not ok:
            logger.warning(f"NapCat set_qq_profile 失败: {r}")
        return ok

    # ═══════════════════════════════════════════════════════════
    #  通用消息
    # ═══════════════════════════════════════════════════════════

    async def send_msg(self, message_type: str = "group", target_id: int = 0, message: str | list = "") -> Optional[int]:
        """通用发送消息"""
        params = {"message_type": message_type}
        if message_type == "group":
            params["group_id"] = target_id
        else:
            params["user_id"] = target_id
        if isinstance(message, str):
            message = cq_text_to_segments(message) if has_cq_codes(message) else [{"type": "text", "data": {"text": message}}]
        params["message"] = message
        r = await self._call("send_msg", params)
        return r["data"]["message_id"] if r.get("status") == "ok" else None

    async def get_forward_msg(self, message_id: int) -> Optional[dict]:
        """获取合并转发消息"""
        r = await self._call("get_forward_msg", {"id": message_id})
        return r["data"] if r.get("status") == "ok" else None

    async def send_forward_msg(self, messages: list) -> Optional[int]:
        """发送合并转发消息（messages 为 node 数组）"""
        r = await self._call("send_forward_msg", {"messages": messages})
        return r["data"]["message_id"] if r.get("status") == "ok" else None

    async def send_group_forward_msg(self, group_id: int, messages: list) -> Optional[int]:
        """群合并转发"""
        r = await self._call("send_group_forward_msg", {"group_id": group_id, "messages": messages})
        return r["data"]["message_id"] if r.get("status") == "ok" else None

    async def send_private_forward_msg(self, user_id: int, messages: list) -> Optional[int]:
        """私聊合并转发"""
        r = await self._call("send_private_forward_msg", {"user_id": user_id, "messages": messages})
        return r["data"]["message_id"] if r.get("status") == "ok" else None

    async def mark_msg_as_read(self, group_id: int = 0, user_id: int = 0) -> bool:
        """标记消息已读"""
        r = await self._call("mark_msg_as_read", {"group_id": group_id, "user_id": user_id})
        return r.get("status") == "ok"

    async def mark_private_msg_as_read(self, user_id: int) -> bool:
        r = await self._call("mark_private_msg_as_read", {"user_id": user_id})
        return r.get("status") == "ok"

    async def mark_group_msg_as_read(self, group_id: int) -> bool:
        r = await self._call("mark_group_msg_as_read", {"group_id": group_id})
        return r.get("status") == "ok"

    async def get_group_msg_history(self, group_id: int, message_seq: int = 0, count: int = 20) -> Optional[dict]:
        r = await self._call("get_group_msg_history", {"group_id": group_id, "message_seq": message_seq, "count": count})
        return r["data"] if r.get("status") == "ok" else None

    async def get_friend_msg_history(self, user_id: int, message_seq: int = 0, count: int = 20) -> Optional[dict]:
        r = await self._call("get_friend_msg_history", {"user_id": user_id, "message_seq": message_seq, "count": count})
        return r["data"] if r.get("status") == "ok" else None

    async def forward_friend_single_msg(self, user_id: int, message_id: int) -> bool:
        """转发单条消息给好友"""
        r = await self._call("forward_friend_single_msg", {"user_id": user_id, "message_id": message_id})
        return r.get("status") == "ok"

    async def forward_group_single_msg(self, group_id: int, message_id: int) -> bool:
        """转发单条消息到群"""
        r = await self._call("forward_group_single_msg", {"group_id": group_id, "message_id": message_id})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  信息查询
    # ═══════════════════════════════════════════════════════════

    async def get_login_info(self) -> dict:
        r = await self._call("get_login_info", {})
        return r["data"] if r.get("status") == "ok" else {}

    async def get_friend_list(self) -> list:
        r = await self._call("get_friend_list", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_group_list(self) -> list:
        r = await self._call("get_group_list", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_stranger_info(self, user_id: int) -> Optional[dict]:
        r = await self._call("get_stranger_info", {"user_id": user_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_status(self) -> dict:
        r = await self._call("get_status", {})
        return r["data"] if r.get("status") == "ok" else {}

    async def get_version_info(self) -> dict:
        r = await self._call("get_version_info", {})
        return r["data"] if r.get("status") == "ok" else {}

    async def get_online_clients(self) -> list:
        r = await self._call("get_online_clients", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    # ═══════════════════════════════════════════════════════════
    #  系统操作（慎重）
    # ═══════════════════════════════════════════════════════════

    async def set_restart(self, delay: int = 0) -> bool:
        """重启Bot"""
        r = await self._call("set_restart", {"delay": delay})
        return r.get("status") == "ok"

    async def clean_cache(self) -> bool:
        r = await self._call("clean_cache", {})
        return r.get("status") == "ok"

    async def bot_exit(self) -> bool:
        """退出Bot"""
        r = await self._call("bot_exit", {})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  群管理（踢人/禁言类 — 需要用户批准）
    # ═══════════════════════════════════════════════════════════

    async def set_group_kick(self, group_id: int, user_id: int, reject_add_request: bool = False) -> bool:
        """踢出群成员。⚠️ 需要用户批准。"""
        r = await self._call("set_group_kick", {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add_request})
        return r.get("status") == "ok"

    async def set_group_leave(self, group_id: int) -> bool:
        """退出群聊。⚠️ 需要用户批准。"""
        r = await self._call("set_group_leave", {"group_id": group_id})
        return r.get("status") == "ok"

    async def set_group_kick_members(self, group_id: int, user_id: list[int], reject_add_request: bool = False) -> bool:
        """批量踢人。⚠️ 需要用户批准。"""
        r = await self._call("set_group_kick_members", {
            "group_id": str(group_id),
            "user_id": [str(uid) for uid in user_id],
            "reject_add_request": reject_add_request,
        })
        return r.get("status") == "ok"

    async def set_friend_add_request(self, flag: str, approve: bool = True, remark: str = "") -> bool:
        """处理好友申请"""
        r = await self._call("set_friend_add_request", {"flag": flag, "approve": approve, "remark": remark})
        return r.get("status") == "ok"

    async def set_group_add_request(self, flag: str, approve: bool = True, reason: str = "") -> bool:
        """处理加群申请"""
        r = await self._call("set_group_add_request", {"flag": flag, "approve": approve, "reason": reason})
        return r.get("status") == "ok"

    async def set_friend_remark(self, user_id: int, remark: str) -> bool:
        """设置好友备注"""
        r = await self._call("set_friend_remark", {"user_id": user_id, "remark": remark})
        return r.get("status") == "ok"

    async def set_group_remark(self, group_id: int, remark: str) -> bool:
        """设置群备注"""
        r = await self._call("set_group_remark", {"group_id": group_id, "remark": remark})
        return r.get("status") == "ok"

    async def delete_friend(self, user_id: int) -> bool:
        """删除好友。⚠️ 需要用户批准。"""
        r = await self._call("delete_friend", {"user_id": user_id})
        return r.get("status") == "ok"

    async def set_group_robot_add_option(self, group_id: int, robot_member_switch: int = 1, robot_member_examine: int = 0) -> bool:
        """设置机器人加群方式"""
        r = await self._call("set_group_robot_add_option", {
            "group_id": str(group_id),
            "robot_member_switch": robot_member_switch,
            "robot_member_examine": robot_member_examine,
        })
        return r.get("status") == "ok"

    async def set_group_add_option(self, group_id: int, add_type: int = 1) -> bool:
        """设置群加群方式。add_type: 1=允许 2=需要验证 3=不允许"""
        r = await self._call("set_group_add_option", {"group_id": str(group_id), "add_type": add_type})
        return r.get("status") == "ok"

    async def set_group_search(self, group_id: int, no_code_finger_open: int = 0, no_finger_open: int = 0) -> bool:
        """设置群搜索选项。no_code_finger_open/no_finger_open 传 0 或 1。"""
        r = await self._call("set_group_search", {
            "group_id": str(group_id),
            "no_code_finger_open": no_code_finger_open,
            "no_finger_open": no_finger_open,
        })
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  群信息扩展
    # ═══════════════════════════════════════════════════════════

    async def set_group_portrait(self, group_id: int, file: str) -> bool:
        """设置群头像"""
        r = await self._call("set_group_portrait", {"group_id": group_id, "file": file})
        return r.get("status") == "ok"

    async def get_group_detail_info(self, group_id: int) -> Optional[dict]:
        """获取群详细信息"""
        r = await self._call("get_group_detail_info", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_info_ex(self, group_id: int) -> Optional[dict]:
        """获取群扩展信息"""
        r = await self._call("get_group_info_ex", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_shut_list(self, group_id: int) -> list:
        """获取群禁言列表"""
        r = await self._call("get_group_shut_list", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_group_system_msg(self, count: int = 50) -> dict:
        """获取群系统消息。count=拉取数量，默认50。"""
        r = await self._call("get_group_system_msg", {"count": count})
        return r.get("data", {}) if r.get("status") == "ok" else {}

    async def get_group_at_all_remain(self, group_id: int) -> Optional[dict]:
        """查询剩余@全体次数"""
        r = await self._call("get_group_at_all_remain", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_ignored_notifies(self) -> dict:
        """获取群忽略通知列表（无参数）"""
        r = await self._call("get_group_ignored_notifies", {})
        return r.get("data", {}) if r.get("status") == "ok" else {}

    async def get_group_ignore_add_request(self) -> dict:
        """获取群忽略加群请求列表（无参数）"""
        r = await self._call("get_group_ignore_add_request", {})
        return r.get("data", {}) if r.get("status") == "ok" else {}

    async def get_group_signed_list(self, group_id: int) -> list:
        """获取群签到列表"""
        r = await self._call("get_group_signed_list", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    # ═══════════════════════════════════════════════════════════
    #  群公告
    # ═══════════════════════════════════════════════════════════

    async def get_group_notice(self, group_id: int) -> list:
        """获取群公告列表"""
        r = await self._call("_get_group_notice", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def del_group_notice(self, group_id: int, notice_id: str) -> bool:
        """删除群公告"""
        r = await self._call("_del_group_notice", {"group_id": group_id, "notice_id": notice_id})
        return r.get("status") == "ok"

    async def get_essence_msg_list(self, group_id: int) -> list:
        """获取群精华消息列表"""
        r = await self._call("get_essence_msg_list", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    # ═══════════════════════════════════════════════════════════
    #  群文件
    # ═══════════════════════════════════════════════════════════

    async def upload_group_file(self, group_id: int, file: str, name: str = "", folder_id: str = "") -> bool:
        r = await self._call("upload_group_file", {"group_id": group_id, "file": file, "name": name, "folder_id": folder_id})
        return r.get("status") == "ok"

    async def delete_group_file(self, group_id: int, file_id: str) -> bool:
        r = await self._call("delete_group_file", {"group_id": str(group_id), "file_id": file_id})
        return r.get("status") == "ok"

    async def create_group_file_folder(self, group_id: int, name: str) -> bool:
        r = await self._call("create_group_file_folder", {"group_id": str(group_id), "name": name})
        return r.get("status") == "ok"

    async def delete_group_folder(self, group_id: int, folder_id: str) -> bool:
        r = await self._call("delete_group_folder", {"group_id": group_id, "folder_id": folder_id})
        return r.get("status") == "ok"

    async def get_group_file_system_info(self, group_id: int) -> Optional[dict]:
        r = await self._call("get_group_file_system_info", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_root_files(self, group_id: int) -> Optional[dict]:
        r = await self._call("get_group_root_files", {"group_id": group_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_files_by_folder(self, group_id: int, folder_id: str) -> Optional[dict]:
        r = await self._call("get_group_files_by_folder", {"group_id": group_id, "folder_id": folder_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_group_file_url(self, group_id: int, file_id: str) -> Optional[str]:
        r = await self._call("get_group_file_url", {"group_id": str(group_id), "file_id": file_id})
        return r["data"]["url"] if r.get("status") == "ok" else None

    async def upload_private_file(self, user_id: int, file: str, name: str = "") -> bool:
        r = await self._call("upload_private_file", {"user_id": user_id, "file": file, "name": name})
        return r.get("status") == "ok"

    async def move_group_file(self, group_id: int, file_id: str, current_parent_directory: str, target_parent_directory: str) -> bool:
        """移动群文件（需提供源目录和目标目录）"""
        r = await self._call("move_group_file", {
            "group_id": str(group_id), "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "target_parent_directory": target_parent_directory,
        })
        return r.get("status") == "ok"

    async def rename_group_file(self, group_id: int, file_id: str, current_parent_directory: str, new_name: str) -> bool:
        """重命名群文件"""
        r = await self._call("rename_group_file", {
            "group_id": str(group_id), "file_id": file_id,
            "current_parent_directory": current_parent_directory, "new_name": new_name,
        })
        return r.get("status") == "ok"

    async def trans_group_file(self, group_id: int, file_id: str) -> bool:
        """跨群传输文件（根据当前消息目标群传输）"""
        r = await self._call("trans_group_file", {"group_id": str(group_id), "file_id": file_id})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  群相册
    # ═══════════════════════════════════════════════════════════

    async def get_qun_album_list(self, group_id: int) -> list:
        r = await self._call("get_qun_album_list", {"group_id": group_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_group_album_media_list(self, group_id: int, album_id: str) -> list:
        r = await self._call("get_group_album_media_list", {"group_id": group_id, "album_id": album_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def upload_image_to_qun_album(self, group_id: int, file: str, album_id: str, album_name: str) -> bool:
        """上传图片到群相册（album_id, album_name 均必填）"""
        r = await self._call("upload_image_to_qun_album", {
            "group_id": str(group_id), "file": file,
            "album_id": album_id, "album_name": album_name,
        })
        return r.get("status") == "ok"

    async def del_group_album_media(self, group_id: int, album_id: str, lloc: str) -> bool:
        """删除群相册媒体（单条）"""
        r = await self._call("del_group_album_media", {"group_id": str(group_id), "album_id": album_id, "lloc": lloc})
        return r.get("status") == "ok"

    async def set_group_album_media_like(self, group_id: int, album_id: str, media_id: str) -> bool:
        r = await self._call("set_group_album_media_like", {"group_id": group_id, "album_id": album_id, "media_id": media_id})
        return r.get("status") == "ok"

    async def cancel_group_album_media_like(self, group_id: int, album_id: str, media_id: str) -> bool:
        r = await self._call("cancel_group_album_media_like", {"group_id": group_id, "album_id": album_id, "media_id": media_id})
        return r.get("status") == "ok"

    async def do_group_album_comment(self, group_id: int, album_id: str, media_id: str, content: str) -> bool:
        r = await self._call("do_group_album_comment", {"group_id": group_id, "album_id": album_id, "media_id": media_id, "content": content})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  群接龙/Todo
    # ═══════════════════════════════════════════════════════════

    async def set_group_todo(self, group_id: int, message_id: str = "", message_seq: str = "") -> bool:
        """将消息设为群接龙/待办（需提供 message_id 或 message_seq）"""
        params: dict = {"group_id": str(group_id)}
        if message_id:
            params["message_id"] = message_id
        if message_seq:
            params["message_seq"] = message_seq
        r = await self._call("set_group_todo", params)
        return r.get("status") == "ok"

    async def complete_group_todo(self, group_id: int, todo_id: str) -> bool:
        r = await self._call("complete_group_todo", {"group_id": group_id, "todo_id": todo_id})
        return r.get("status") == "ok"

    async def cancel_group_todo(self, group_id: int, todo_id: str) -> bool:
        r = await self._call("cancel_group_todo", {"group_id": group_id, "todo_id": todo_id})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  QQ资料 & 个人状态
    # ═══════════════════════════════════════════════════════════

    async def set_diy_online_status(self, face_id: int = 0, wording: str = "", face_type: int = 1) -> bool:
        """设置自定义在线状态。wording=状态文字 face_id=图标ID face_type=图标类型"""
        r = await self._call("set_diy_online_status", {
            "face_id": face_id,
            "wording": wording,
            "face_type": face_type,
        })
        return r.get("status") == "ok" or r.get("retcode") == 0

    async def set_qq_avatar(self, file: str) -> bool:
        """修改QQ头像（图片路径或URL）"""
        r = await self._call("set_qq_avatar", {"file": file})
        return r.get("status") == "ok"

    async def set_input_status(self, user_id: int, event_type: int = 1) -> bool:
        """设置输入状态（"正在输入..."）。event_type: 1=正在输入 2=取消。仅支持私聊。"""
        r = await self._call("set_input_status", {"user_id": str(user_id), "event_type": event_type})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  表情表态 & 表情管理
    # ═══════════════════════════════════════════════════════════

    async def set_msg_emoji_like(self, message_id: int, emoji_id: str, set: bool = True) -> bool:
        """对消息发表情表态（贴表情）"""
        r = await self._call("set_msg_emoji_like", {"message_id": message_id, "emoji_id": emoji_id, "set": set})
        return r.get("status") == "ok"

    async def fetch_custom_face(self, count: int = 48) -> list:
        """获取收藏表情列表。返回表情URL数组。"""
        r = await self._call("fetch_custom_face", {"count": count})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def fetch_custom_face_detail(self, face_id: str) -> Optional[dict]:
        r = await self._call("fetch_custom_face_detail", {"face_id": face_id})
        return r["data"] if r.get("status") == "ok" else None

    async def add_custom_face(self, file: str, summary: str = "") -> bool:
        """添加收藏表情"""
        r = await self._call("add_custom_face", {"file": file, "summary": summary})
        return r.get("status") == "ok"

    async def delete_custom_face(self, face_id: str) -> bool:
        r = await self._call("delete_custom_face", {"face_id": face_id})
        return r.get("status") == "ok"

    async def set_custom_face_desc(self, face_id: str, summary: str) -> bool:
        r = await self._call("set_custom_face_desc", {"face_id": face_id, "summary": summary})
        return r.get("status") == "ok"

    async def fetch_emoji_like(self, message_id: int, emoji_id: str) -> list:
        """获取消息表情表态列表"""
        r = await self._call("fetch_emoji_like", {"message_id": message_id, "emoji_id": emoji_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_emoji_likes(self, message_id: int) -> list:
        """获取消息所有表情表态"""
        r = await self._call("get_emoji_likes", {"message_id": message_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    # ═══════════════════════════════════════════════════════════
    #  好友 & 联系人
    # ═══════════════════════════════════════════════════════════

    async def get_friends_with_category(self) -> list:
        """获取带分组的好友列表"""
        r = await self._call("get_friends_with_category", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_recent_contact(self, count: int = 20) -> list:
        r = await self._call("get_recent_contact", {"count": count})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_unidirectional_friend_list(self) -> list:
        """获取单向好友列表"""
        r = await self._call("get_unidirectional_friend_list", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_doubt_friends_add_request(self) -> list:
        r = await self._call("get_doubt_friends_add_request", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def set_doubt_friends_add_request(self, user_id: int, is_doubt: bool = True) -> bool:
        r = await self._call("set_doubt_friends_add_request", {"user_id": user_id, "is_doubt": is_doubt})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  分享 / Ark消息
    # ═══════════════════════════════════════════════════════════

    async def send_group_ark_share(self, group_id: int, ark_data: dict) -> bool:
        r = await self._call("send_group_ark_share", {"group_id": group_id, "ark_data": ark_data})
        return r.get("status") == "ok"

    async def send_ark_share(self, target_type: str, target_id: int, ark_data: dict) -> bool:
        r = await self._call("send_ark_share", {"target_type": target_type, "target_id": target_id, "ark_data": ark_data})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  闪传 (Flash)
    # ═══════════════════════════════════════════════════════════

    async def create_flash_task(self, files: list) -> Optional[str]:
        r = await self._call("create_flash_task", {"files": files})
        return r["data"]["task_id"] if r.get("status") == "ok" else None

    async def send_flash_msg(self, fileset_id: str, group_id: int = 0, user_id: int = 0) -> bool:
        """发送闪传消息"""
        params: dict = {"fileset_id": fileset_id}
        if group_id:
            params["group_id"] = str(group_id)
        if user_id:
            params["user_id"] = str(user_id)
        r = await self._call("send_flash_msg", params)
        return r.get("status") == "ok"

    async def get_share_link(self, fileset_id: str) -> Optional[str]:
        r = await self._call("get_share_link", {"fileset_id": fileset_id})
        if r.get("status") == "ok":
            data = r.get("data")
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                return data.get("share_link") or data.get("url")
        return None

    async def download_fileset(self, fileset_id: str) -> bool:
        """下载闪传文件集"""
        r = await self._call("download_fileset", {"fileset_id": fileset_id})
        return r.get("status") == "ok"

    async def get_fileset_info(self, fileset_id: str) -> Optional[dict]:
        r = await self._call("get_fileset_info", {"fileset_id": fileset_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_flash_file_list(self, fileset_id: str) -> list:
        r = await self._call("get_flash_file_list", {"fileset_id": fileset_id})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_flash_file_url(self, fileset_id: str, file_id: str) -> Optional[str]:
        r = await self._call("get_flash_file_url", {"fileset_id": fileset_id, "file_id": file_id})
        return r["data"]["url"] if r.get("status") == "ok" else None

    async def get_fileset_id(self, message_id: int) -> Optional[str]:
        r = await self._call("get_fileset_id", {"message_id": message_id})
        return r["data"]["fileset_id"] if r.get("status") == "ok" else None

    # ═══════════════════════════════════════════════════════════
    #  在线文件
    # ═══════════════════════════════════════════════════════════

    async def send_online_file(self, user_id: int, file_path: str, file_name: str = "") -> bool:
        """发送私聊在线文件"""
        params: dict = {"user_id": str(user_id), "file_path": file_path}
        if file_name:
            params["file_name"] = file_name
        r = await self._call("send_online_file", params)
        return r.get("status") == "ok"

    async def send_online_folder(self, user_id: int, folder_path: str, folder_name: str = "") -> bool:
        """发送私聊在线文件夹"""
        params: dict = {"user_id": str(user_id), "folder_path": folder_path}
        if folder_name:
            params["folder_name"] = folder_name
        r = await self._call("send_online_folder", params)
        return r.get("status") == "ok"

    async def get_online_file_msg(self, user_id: int) -> Optional[dict]:
        """获取在线文件消息列表"""
        r = await self._call("get_online_file_msg", {"user_id": str(user_id)})
        return r["data"] if r.get("status") == "ok" else None

    async def receive_online_file(self, group_id: int, message_id: int, save_path: str = "") -> bool:
        r = await self._call("receive_online_file", {"group_id": group_id, "message_id": message_id, "save_path": save_path})
        return r.get("status") == "ok"

    async def refuse_online_file(self, group_id: int, message_id: int) -> bool:
        r = await self._call("refuse_online_file", {"group_id": group_id, "message_id": message_id})
        return r.get("status") == "ok"

    async def cancel_online_file(self, group_id: int, message_id: int) -> bool:
        r = await self._call("cancel_online_file", {"group_id": group_id, "message_id": message_id})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  QQ空间
    # ═══════════════════════════════════════════════════════════

    async def send_qzone_msg(self, content: str) -> bool:
        r = await self._call("send_qzone_msg", {"content": content})
        return r.get("status") == "ok"

    async def delete_qzone_msg(self, msg_id: str) -> bool:
        r = await self._call("delete_qzone_msg", {"msg_id": msg_id})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  频道/Guild
    # ═══════════════════════════════════════════════════════════

    async def get_guild_list(self) -> list:
        r = await self._call("get_guild_list", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_guild_service_profile(self) -> Optional[dict]:
        r = await self._call("get_guild_service_profile", {})
        return r["data"] if r.get("status") == "ok" else None

    # ═══════════════════════════════════════════════════════════
    #  签到 & AI
    # ═══════════════════════════════════════════════════════════

    async def set_group_sign(self, group_id: int) -> bool:
        """群打卡"""
        r = await self._call("set_group_sign", {"group_id": str(group_id)})
        return r.get("status") == "ok"

    async def send_group_sign(self, group_id: int) -> bool:
        """发送群签到卡片"""
        r = await self._call("send_group_sign", {"group_id": group_id})
        return r.get("status") == "ok"

    async def get_ai_record(self, group_id: int, character_id: str = "", text: str = "") -> Optional[dict]:
        r = await self._call("get_ai_record", {"group_id": group_id, "character_id": character_id, "text": text})
        return r["data"] if r.get("status") == "ok" else None

    async def get_ai_characters(self) -> list:
        """获取AI角色列表"""
        r = await self._call("get_ai_characters", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def send_group_ai_record(self, group_id: int, character_id: str, text: str) -> bool:
        r = await self._call("send_group_ai_record", {"group_id": group_id, "character_id": character_id, "text": text})
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  戳一戳 & 点赞扩展
    # ═══════════════════════════════════════════════════════════

    async def send_poke(self, target_type: str = "group", target_id: int = 0, user_id: int = 0) -> bool:
        """通用戳一戳"""
        params = {"target_type": target_type, "target_id": target_id}
        if user_id:
            params["user_id"] = user_id
        r = await self._call("send_poke", params)
        return r.get("status") == "ok"

    # ═══════════════════════════════════════════════════════════
    #  工具类
    # ═══════════════════════════════════════════════════════════

    async def ocr_image(self, image: str) -> Optional[dict]:
        """图片OCR识别"""
        r = await self._call("ocr_image", {"image": image})
        return r["data"] if r.get("status") == "ok" else None

    async def translate_en2zh(self, words: list[str]) -> Optional[list[str]]:
        """英译中。输入英文单词列表，返回中文列表。"""
        r = await self._call("translate_en2zh", {"words": words})
        return r["data"]["words"] if r.get("status") == "ok" else None

    async def fetch_ptt_text(self, message_id: int) -> Optional[str]:
        """语音转文本（通过消息ID获取语音消息的文本）"""
        r = await self._call("fetch_ptt_text", {"message_id": message_id})
        return r["data"]["text"] if r.get("status") == "ok" else None

    async def get_image(self, file: str) -> Optional[dict]:
        """获取图片信息"""
        r = await self._call("get_image", {"file": file})
        return r["data"] if r.get("status") == "ok" else None

    async def get_record(self, file: str, out_format: str = "mp3") -> Optional[dict]:
        """获取语音文件信息"""
        r = await self._call("get_record", {"file": file, "out_format": out_format})
        return r["data"] if r.get("status") == "ok" else None

    async def can_send_image(self) -> bool:
        r = await self._call("can_send_image", {})
        return r["data"].get("yes", False) if r.get("status") == "ok" else False

    async def can_send_record(self) -> bool:
        r = await self._call("can_send_record", {})
        return r["data"].get("yes", False) if r.get("status") == "ok" else False

    async def get_cookies(self, domain: str = "") -> Optional[str]:
        r = await self._call("get_cookies", {"domain": domain})
        return r["data"]["cookies"] if r.get("status") == "ok" else None

    async def get_csrf_token(self) -> Optional[int]:
        r = await self._call("get_csrf_token", {})
        return r["data"]["token"] if r.get("status") == "ok" else None

    async def get_credentials(self) -> dict:
        r = await self._call("get_credentials", {})
        return r.get("data", {}) if r.get("status") == "ok" else {}

    async def check_url_safely(self, url: str) -> Optional[int]:
        """URL安全检查。返回安全等级。"""
        r = await self._call("check_url_safely", {"url": url})
        return r["data"]["level"] if r.get("status") == "ok" else None

    async def download_file(self, url: str, base64: str = "", name: str = "", headers: list[str] = []) -> Optional[str]:
        """下载文件，返回本地路径。url/base64 二选一。"""
        params: dict = {}
        if url:
            params["url"] = url
        if base64:
            params["base64"] = base64
        if name:
            params["name"] = name
        if headers:
            params["headers"] = headers
        r = await self._call("download_file", params)
        return r["data"]["file"] if r.get("status") == "ok" else None

    async def get_file(self, file_id: str) -> Optional[dict]:
        """获取文件信息"""
        r = await self._call("get_file", {"file_id": file_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_private_file_url(self, user_id: int, file_id: str) -> Optional[str]:
        r = await self._call("get_private_file_url", {"user_id": user_id, "file_id": file_id})
        return r["data"]["url"] if r.get("status") == "ok" else None

    async def get_mini_app_ark(self, type: str, title: str, desc: str, picUrl: str, jumpUrl: str, webUrl: str = "", **kwargs) -> Optional[dict]:
        """获取小程序Ark。type 可用模板: bili / weibo。或用完整自定义参数（见kwargs）。"""
        params: dict = {"type": type, "title": title, "desc": desc, "picUrl": picUrl, "jumpUrl": jumpUrl}
        if webUrl:
            params["webUrl"] = webUrl
        params.update(kwargs)
        r = await self._call("get_mini_app_ark", params)
        return r["data"] if r.get("status") == "ok" else None

    async def click_inline_keyboard_button(self, message_id: int, button_id: str) -> bool:
        """点击内联键盘按钮"""
        r = await self._call("click_inline_keyboard_button", {"message_id": message_id, "button_id": button_id})
        return r.get("status") == "ok"

    async def get_clientkey(self) -> Optional[str]:
        r = await self._call("get_clientkey", {})
        return r["data"]["clientkey"] if r.get("status") == "ok" else None

    async def get_rkey(self) -> Optional[str]:
        r = await self._call("nc_get_rkey", {})
        return r["data"]["rkey"] if r.get("status") == "ok" else None

    async def get_rkey_server(self) -> Optional[str]:
        r = await self._call("get_rkey_server", {})
        return r["data"]["rkey_server"] if r.get("status") == "ok" else None

    async def get_robot_uin_range(self) -> list:
        r = await self._call("get_robot_uin_range", {})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_profile_like(self) -> Optional[dict]:
        """获取自己的资料卡点赞信息"""
        r = await self._call("get_profile_like", {})
        return r["data"] if r.get("status") == "ok" else None

    async def create_collection(self, title: str, content: str, content_type: str = "text") -> bool:
        """创建收藏"""
        r = await self._call("create_collection", {"title": title, "content": content, "content_type": content_type})
        return r.get("status") == "ok"

    async def get_collection_list(self, page: int = 1, count: int = 20) -> list:
        """获取收藏列表"""
        r = await self._call("get_collection_list", {"page": page, "count": count})
        return r.get("data", []) if r.get("status") == "ok" else []

    async def get_packet_status(self, user_id: int) -> Optional[dict]:
        """获取对方封包状态"""
        r = await self._call("nc_get_packet_status", {"user_id": user_id})
        return r["data"] if r.get("status") == "ok" else None

    async def get_user_status(self, user_id: int) -> Optional[dict]:
        """获取用户在线状态"""
        r = await self._call("nc_get_user_status", {"user_id": user_id})
        return r["data"] if r.get("status") == "ok" else None

    # ==================== 事件系统 ====================

    def on_event(self, handler: Callable):
        self._handlers.append(handler)
        return handler

    async def _dispatch(self, event: dict):
        for h in self._handlers:
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(self, event)
                else:
                    h(self, event)
            except Exception:
                logger.error(f"事件处理异常: {traceback.format_exc()}")

    # ==================== 消息路由 ====================

    async def _route_message(self, raw: str):
        """统一消息路由：API 回执 → pending future，事件 → dispatch"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # API 回执
        echo = data.get("echo", "")
        if echo and echo in self._pending:
            self._pending[echo].set_result(data)
            return

        # 生命周期
        if not self.self_id and data.get("self_id"):
            self.self_id = str(data["self_id"])
            if not self.bot_qq:
                self.bot_qq = self.self_id
            logger.info(f"Bot QQ: {self.self_id}")

        meta = data.get("meta_event_type", "")
        if meta == "heartbeat":
            return
        if meta == "lifecycle":
            logger.info(f"Bot 已上线 ({data.get('self_id', '?')})")
            return

        # 业务事件（消息 + 通知 + 请求）
        # ⚠️ 必须包含 "request" —— NapCat 的加群请求是 request 事件，
        # 不放行进 verify.py 的话，新人会被静默吞掉，没人验证。
        post_type = data.get("post_type", "")
        if post_type in ("message", "notice", "request"):
            asyncio.create_task(self._dispatch(data))

    # ==================== 连接管理 ====================

    def _build_url(self) -> str:
        url = self.ws_url
        if self.ws_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.ws_token}"
        return url

    async def connect(self):
        url = self._build_url()
        masked = url.replace(self.ws_token, "***") if self.ws_token else url
        logger.info(f"连接 NapCat: {masked}")

        self._running = True
        delay = 1

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10, max_size=2**23) as ws:
                    self._ws = ws
                    logger.info("NapCat WebSocket 已连接")
                    delay = 1

                    async for raw in ws:
                        await self._route_message(raw)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WS 断开: {e}")
            except (OSError, ConnectionError) as e:
                logger.warning(f"连接失败: {e}")
            except Exception:
                logger.error(f"WS 异常: {traceback.format_exc()}")

            if not self._running:
                break
            logger.info(f"{delay}s 后重连...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def close(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("Bot 已关闭")
