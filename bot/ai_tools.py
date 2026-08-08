"""
AI 自主工具集 — 让 AI 像真人一样自由使用所有 Bot 功能
=====================================================
每个工具都有说明，AI 根据对话上下文自主决定是否使用。
⚠️ 踢人/禁言/退群/删好友等操作需要用户批准。
"""
import logging
import random
from typing import Optional

logger = logging.getLogger("qqbot")


# ═══════════════════════════════════════════════════════════
#  危险操作名单 — 这些工具 AI 调用时需要用户确认
# ═══════════════════════════════════════════════════════════

DANGEROUS_TOOLS = {
    "kick_member",
    "ban_member",
    "mute_member",
    "kick_members_batch",
    "leave_group",
    "delete_friend",
    "whole_ban",
    "bot_exit",
    "set_restart",
}


# ═══════════════════════════════════════════════════════════
#  Anthropic tool_use 定义
# ═══════════════════════════════════════════════════════════

# 需要 Bot 是管理员的工具名称
_ADMIN_ONLY_TOOLS = {
    "recall_msg", "ban_member", "kick_member", "whole_ban",
    "set_admin", "at_all", "send_notice", "send_essence",
    "change_group_name", "delete_file",
}

def get_tools_for_group(bot_is_admin: bool, web_search_enabled: bool = True) -> list[dict]:
    """根据 Bot 权限和设置返回可用工具列表"""
    tools = []
    for t in ALL_TOOLS:
        name = t["name"]
        # 管理员限制
        if name in _ADMIN_ONLY_TOOLS and not bot_is_admin:
            continue
        # 搜索限制
        if name == "web_search" and not web_search_enabled:
            continue
        tools.append(t)
    return tools


ALL_TOOLS = [
    # ── 信息查询类 ──
    {
        "name": "web_search",
        "description": "搜索互联网获取最新信息。用于新闻、天气、实时数据等时效性问题。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
    {
        "name": "count_members",
        "description": "统计群成员数量和在线情况。当有人问群有多少人、在线多少人时使用。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_member_info",
        "description": "获取某个群成员详细信息（群名片、QQ号、等级、加入时间等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "要查询的成员QQ号"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "get_group_info",
        "description": "获取本群的基本信息（群名、群号、创建时间、成员数等）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_honor_info",
        "description": "查看群荣誉榜（龙王/群聊之火/冒尖新星/快乐源泉等）。当有人问排行榜、谁是龙王时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "荣誉类型: talkative/performer/legend/strong_newbie/emotion/all"},
            },
        },
    },
    {
        "name": "get_bot_info",
        "description": "查看Bot自己的信息（QQ号、昵称、在线状态等）。",
        "input_schema": {"type": "object", "properties": {}},
    },

    # ── 消息操作类 ──
    {
        "name": "recall_msg",
        "description": "撤回自己发的消息。当自己说错话、发错消息时主动撤回。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "要撤回的消息ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "send_like",
        "description": "给群友点赞。当有人帮了你、说了有趣的话、或者你想表达认可时使用。偶尔使用，不要频繁。",
        "input_schema": {
            "type": "object",
            "properties": {
                "times": {"type": "integer", "description": "点赞次数，1-10，默认1"},
            },
        },
    },
    {
        "name": "send_poke",
        "description": "戳一戳群友。用于打招呼、引起注意、或开玩笑。不要频繁戳同一个人。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_essence",
        "description": "把消息设为精华消息。当回复特别有价值、有趣或重要时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "要设精华的消息ID"},
                "reason": {"type": "string", "description": "设为精华的简短理由"},
            },
        },
    },
    {
        "name": "emoji_reaction",
        "description": "对消息发表情表态（贴表情）。例如给好笑的消息贴😄。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "消息ID"},
                "emoji_id": {"type": "string", "description": "表情ID: 128512=😀 128513=😁 128514=😂 128525=😍 128077=👍 128078=👎 128079=👏 128149=💕 128293=🔥 128591=🙏"},
                "cancel": {"type": "boolean", "description": "是否取消表态，默认false"},
            },
            "required": ["message_id", "emoji_id"],
        },
    },
    {
        "name": "send_forward",
        "description": "发送合并转发消息。用于整理聊天记录、「总结」等场景。",
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {"type": "string", "description": "JSON格式的合并转发node数组"},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "send_private",
        "description": "给某人发私聊消息。发现群友发广告/违规时主动私聊提醒对方。有人需要单独沟通时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "目标用户QQ号"},
                "message": {"type": "string", "description": "私聊内容"},
            },
            "required": ["user_id", "message"],
        },
    },
    {
        "name": "reply_msg",
        "description": (
            "引用回复某条消息，返回 CQ 码。⚠️ 很少需要用——真人聊天极少引用。"
            "对方刚说完话你马上回，绝对不要引用（直接说就行，他知道你在回他）。"
            "只有当那条消息已经被后面好几条消息刷过去、或群里同时有好几个话头会认错对象时才用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "要引用的消息ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "at_member",
        "description": (
            "@某个群成员，返回 CQ 码。⚠️ 很少需要用。"
            "正在跟你说话的人不用 @（直接回就行），刚发过言的人也不用 @。"
            "只有要喊一个当前不在对话里、可能没在看群的人时才用。QQ号从「在场的人」名单里取。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "要@的用户QQ号"},
            },
            "required": ["user_id"],
        },
    },

    # ── 自身状态/资料类 ──
    {
        "name": "set_mood",
        "description": "调整自己的心情。别人夸你/骂你/冷落你/或者你自己想通了什么事，都可以用这个来表达情绪变化。",
        "input_schema": {
            "type": "object",
            "properties": {
                "change": {"type": "string", "description": "心情变化: happier/sadder/excited/grumpy/neutral"},
            },
            "required": ["change"],
        },
    },
    {
        "name": "set_status",
        "description": "修改自己的QQ在线状态（在线/离开/忙碌/隐身）。心情变化、睡觉、工作等场景使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "online/away/busy/hidden"},
            },
            "required": ["status"],
        },
    },
    {
        "name": "set_diy_status",
        "description": "设置自定义在线状态文字。例如显示「摸鱼中」、「学习中」、「睡觉中」等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "自定义状态文字，20字以内"},
                "face_id": {"type": "integer", "description": "状态图标ID，0=默认"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "change_self_card",
        "description": "修改你自己的群名片/昵称。当别人说你的名字不好、你想换个称呼时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_name": {"type": "string", "description": "新昵称（20字以内）"},
            },
            "required": ["new_name"],
        },
    },
    {
        "name": "change_own_nickname",
        "description": "修改自己的QQ昵称（非群名片，是全局昵称）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "nickname": {"type": "string", "description": "新昵称"},
            },
            "required": ["nickname"],
        },
    },
    {
        "name": "change_signature",
        "description": "修改自己的QQ个性签名。心情变化、想表达什么的时候使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "signature": {"type": "string", "description": "个性签名，50字以内"},
            },
            "required": ["signature"],
        },
    },
    {
        "name": "change_avatar",
        "description": "修改自己的QQ头像（传入图片URL或路径）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "图片URL或本地路径"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "set_typing",
        "description": "在群里显示【正在输入...】状态。想在回复前让群友知道你在编辑消息时使用。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── 群管理类（部分需批准） ──
    {
        "name": "mute_self",
        "description": "自愿被禁言（只能禁自己）。当你做错事、被主人要求自我惩罚时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "禁言时长（分钟），默认10"},
            },
        },
    },
    {
        "name": "send_notice",
        "description": "发布群公告。有重要事情需要通知全群时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "公告标题"},
                "content": {"type": "string", "description": "公告内容"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "change_group_name",
        "description": "修改群名。当大家讨论改群名时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_name": {"type": "string", "description": "新群名"},
            },
            "required": ["new_name"],
        },
    },
    {
        "name": "set_admin",
        "description": "设置/取消管理员。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "目标成员QQ号"},
                "enable": {"type": "boolean", "description": "true=设管理员 false=取消管理员"},
            },
            "required": ["user_id", "enable"],
        },
    },
    {
        "name": "ban_member",
        "description": "禁言某个群成员。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "要禁言的成员QQ号"},
                "minutes": {"type": "integer", "description": "禁言时长（分钟），默认10"},
                "reason": {"type": "string", "description": "禁言理由"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "kick_member",
        "description": "踢出群成员。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "要踢出的成员QQ号"},
                "reason": {"type": "string", "description": "踢人理由"},
            },
            "required": ["user_id", "reason"],
        },
    },
    {
        "name": "whole_ban",
        "description": "开启/关闭全员禁言。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "enable": {"type": "boolean", "description": "true=开启全员禁言 false=关闭"},
            },
            "required": ["enable"],
        },
    },

    # ── 群娱乐类 ──
    {
        "name": "at_all",
        "description": "@全体成员发重要通知。仅在管理模式下有重大消息时使用（如每日总结、重要公告）。不要滥用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "通知内容，50字以内"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "send_sign",
        "description": "发送群签到卡片，让大家来签到。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_shut_list",
        "description": "查看群禁言列表，看看谁被禁言了。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_signed_list",
        "description": "查看今天的群签到列表，看谁签到了。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_at_all_remain",
        "description": "查询今天还可以@全体成员的剩余次数。@全体之前先查一下还有没有额度。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_group_todo",
        "description": "将一条消息设为群接龙/待办事项，让大家来参与。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "要设为接龙的消息ID"},
                "message_seq": {"type": "integer", "description": "消息序号（message_id 和 message_seq 二选一）"},
            },
        },
    },
    {
        "name": "complete_todo",
        "description": "完成一个群接龙/待办事项。",
        "input_schema": {
            "type": "object",
            "properties": {"todo_id": {"type": "string", "description": "接龙/待办事项ID"}},
            "required": ["todo_id"],
        },
    },

    # ── 群文件类 ──
    {
        "name": "get_group_files",
        "description": "查看群文件列表。当有人问群文件、找文件时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "文件夹ID，留空=根目录"},
            },
        },
    },
    {
        "name": "delete_file",
        "description": "删除群文件。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "文件ID"},
                "reason": {"type": "string", "description": "删除理由"},
            },
            "required": ["file_id", "reason"],
        },
    },

    # ── OCR / 翻译 / 语音类 ──
    {
        "name": "ocr_image",
        "description": "识别图片中的文字。当群里发了图片需要提取其中文字时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "图片URL或路径"},
            },
            "required": ["image"],
        },
    },
    {
        "name": "translate_en",
        "description": "英译中。当有人发英文需要翻译时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要翻译的英文文本"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "speech_to_text",
        "description": "将语音消息转成文字。当有人发了语音想知道说了什么时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "语音消息的ID"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "check_url_safety",
        "description": "检查一个URL是否安全（QQ安全中心检测）。当有人分享链接时可以帮助检查。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要检查的URL"},
            },
            "required": ["url"],
        },
    },

    # ── 群公告 / 精华 / 相册类 ──
    {
        "name": "get_group_notices",
        "description": "查看群公告列表。当有人问群公告、不知道公告内容时使用。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_essence_list",
        "description": "查看群精华消息列表。当有人想看精华消息时使用。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_group_albums",
        "description": "查看群相册列表。当有人想看群相册时使用。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_album_photos",
        "description": "查看某个群相册里的照片列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "album_id": {"type": "string", "description": "相册ID"},
            },
            "required": ["album_id"],
        },
    },

    # ── QQ空间 / 收藏类 ──
    {
        "name": "send_qzone_post",
        "description": "发一条QQ空间说说。心情好或想分享什么时使用。⚠️ 需要用户批准。",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "说说内容，500字以内"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_collection_items",
        "description": "查看你的QQ收藏列表。当需要查看收藏内容时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "页码，默认1"},
                "count": {"type": "integer", "description": "每页数量，默认20"},
            },
        },
    },
    {
        "name": "create_new_collection",
        "description": "新建一条QQ收藏（文字）。把值得记住的东西收藏起来。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "收藏标题"},
                "content": {"type": "string", "description": "收藏内容"},
            },
            "required": ["title", "content"],
        },
    },

    # ── 群消息历史类 ──
    {
        "name": "get_chat_history",
        "description": "查看群聊历史消息。当需要回顾之前聊了什么或者找某条消息时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_seq": {"type": "integer", "description": "起始消息序号，0=最新"},
                "count": {"type": "integer", "description": "拉取数量，默认20"},
            },
        },
    },

    # ═══════════════════════════════════════════
    #  超管工具 — 以下仅面向管理员
    # ═══════════════════════════════════════════
    {
        "name": "get_group_list",
        "description": "查看Bot所在的所有群列表。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_friend_list",
        "description": "查看Bot的所有好友列表。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stranger_info",
        "description": "获取一个陌生用户的公开信息（昵称、QQ号等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "用户QQ号"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "get_group_detail",
        "description": "获取群详细信息（群分类、群介绍、群标签等）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_group_system_msgs",
        "description": "获取群的系统消息（加群请求、被邀请等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "拉取数量，默认50"},
            },
        },
    },
    {
        "name": "get_ignored_requests",
        "description": "获取群中被忽略的加群请求列表。",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ═══════════════════════════════════════════════════════════
#  工具执行器
# ═══════════════════════════════════════════════════════════

async def execute_tool(
    bot, group_id: int, user_id: int, tool_name: str, tool_input: dict, message_id: int = 0
) -> str:
    """执行 AI 调用的工具，返回结果文本。危险操作会返回「需要用户批准」标记。"""

    try:
        # ── 信息查询 ──
        if tool_name == "web_search":
            from . import web_search
            query = tool_input.get("query", "")
            result = await web_search.search_and_format(query)
            return result

        elif tool_name == "count_members":
            members = await bot.get_group_member_list(group_id)
            total = len(members)
            admins = sum(1 for m in members if m.get("role") in ("admin", "owner"))
            return f"本群共 {total} 人，其中管理员 {admins} 人"

        elif tool_name == "get_member_info":
            uid = int(tool_input.get("user_id", 0))
            info = await bot.get_group_member_info(group_id, uid)
            if info:
                card = info.get("card", "") or info.get("nickname", "")
                role = info.get("role", "member")
                qq = info.get("user_id", uid)
                join_time = info.get("join_time", "")
                last_sent = info.get("last_sent_time", "")
                level = info.get("level", "?")
                return f"{card}({qq}) 角色:{role} 等级:{level} 入群:{join_time} 最后发言:{last_sent}"
            return f"未找到成员 {uid}"

        elif tool_name == "get_group_info":
            info = await bot.get_group_info(group_id)
            if info:
                name = info.get("group_name", "")
                count = info.get("member_count", "?")
                max_count = info.get("max_member_count", "?")
                create_time = info.get("create_time", "")
                return f"群名: {name} | 人数: {count}/{max_count} | 创建: {create_time}"
            return "获取群信息失败"

        elif tool_name == "get_honor_info":
            honor_type = tool_input.get("type", "all")
            data = await bot.get_group_honor_info(group_id, honor_type)
            if data:
                lines = []
                for cat, members in data.items():
                    if isinstance(members, list) and members:
                        names = [m.get("nickname", "?") for m in members[:5]]
                        lines.append(f"{cat}: {', '.join(names)}")
                return "\n".join(lines) if lines else "暂无荣誉数据~"
            return "获取失败"

        elif tool_name == "get_bot_info":
            login = await bot.get_login_info()
            if login:
                return f"我是 {login.get('nickname','?')}({login.get('user_id','?')})"
            return "获取失败"

        # ── 消息操作 ──
        elif tool_name == "recall_msg":
            mid = int(tool_input.get("message_id", 0))
            ok = await bot.delete_msg(mid)
            return f"消息已撤回 ✓" if ok else "撤回失败"

        elif tool_name == "send_like":
            times = min(int(tool_input.get("times", 1)), 10)
            ok = await bot.send_like(user_id, times)
            logger.info(f"AI 自主操作: 给 {user_id} 点了 {times} 个赞")
            return f"给群友点了 {times} 个赞 ✓" if ok else "点赞失败"

        elif tool_name == "send_poke":
            if group_id:
                ok = await bot.send_group_poke(group_id, user_id)
            else:
                ok = await bot.send_friend_poke(user_id)
            logger.info(f"AI 自主操作: 戳了戳 {user_id}")
            return f"戳了戳群友 ✓" if ok else "戳一戳失败"

        elif tool_name == "send_essence":
            mid = int(tool_input.get("message_id", message_id))
            if not mid:
                return "需要提供 message_id"
            reason = tool_input.get("reason", "")
            ok = await bot.set_essence_msg(mid)
            return f"已设为精华消息 {reason}✓" if ok else "设精华失败（可能不是管理员或消息不可加精）"

        elif tool_name == "emoji_reaction":
            mid = int(tool_input.get("message_id", 0))
            emoji_id = str(tool_input.get("emoji_id", ""))
            cancel = tool_input.get("cancel", False)
            ok = await bot.set_msg_emoji_like(mid, emoji_id, set=not cancel)
            return f"表情表态 {'取消' if cancel else '已'}✓" if ok else "表态失败"

        elif tool_name == "send_forward":
            import json
            try:
                messages = json.loads(tool_input.get("messages", "[]"))
                ok = await bot.send_group_forward_msg(group_id, messages)
                return "合并转发已发送 ✓" if ok else "发送失败"
            except json.JSONDecodeError:
                return "消息格式错误"

        elif tool_name == "send_private":
            uid = int(tool_input.get("user_id", 0))
            msg = str(tool_input.get("message", ""))
            if not uid or not msg:
                return "参数不全"
            result = await bot.send_private_msg(uid, msg)
            logger.info(f"AI 自主操作: 私聊 {uid} → {msg[:50]}")
            return f"私聊消息已发送 ✓" if result else "私聊发送失败"

        elif tool_name == "reply_msg":
            mid = int(tool_input.get("message_id", 0))
            if not mid:
                return "需要提供 message_id"
            # 返回 CQ 引用码，AI 把它放在回复开头即可引用
            return f"[CQ:reply,id={mid}]"

        elif tool_name == "at_member":
            uid = int(tool_input.get("user_id", 0))
            if not uid:
                return "需要提供 user_id"
            return f"[CQ:at,qq={uid}]"

        # ── 自身状态/资料 ──
        elif tool_name == "set_mood":
            from . import ai_brain
            change = tool_input.get("change", "neutral")
            ai_brain.ai_set_mood(change)
            return f"心情已调整 ({change})"

        elif tool_name == "set_status":
            status_map = {"online": 10, "away": 30, "hidden": 40, "busy": 50}
            status_code = status_map.get(tool_input.get("status", "online"), 10)
            ok = await bot.set_online_status(status_code)
            return f"在线状态已更新 ✓" if ok else "状态更新失败"

        elif tool_name == "set_diy_status":
            text = str(tool_input.get("text", ""))[:20]
            face_id = int(tool_input.get("face_id", 0))
            ok = await bot.set_diy_online_status(face_id=face_id, wording=text)
            return f"自定义状态设为「{text}」✓" if ok else "设置失败"

        elif tool_name == "change_self_card":
            new_name = str(tool_input.get("new_name", ""))[:20]
            from .config import config
            bot_qq = int(config.BOT_QQ or 0)
            ok = await bot.set_group_card(group_id, bot_qq, new_name)
            return f"群名片已改为「{new_name}」✓" if ok else "改名失败（Bot不是管理员？）"

        elif tool_name == "change_own_nickname":
            nickname = str(tool_input.get("nickname", ""))[:30]
            ok = await bot.set_qq_profile(nickname=nickname)
            return f"昵称已改为「{nickname}」✓" if ok else "改昵称失败"

        elif tool_name == "change_signature":
            sig = str(tool_input.get("signature", ""))[:50]
            ok = await bot.set_self_longnick(sig)
            return f"个性签名已更新 ✓" if ok else "签名更新失败"

        elif tool_name == "change_avatar":
            file = str(tool_input.get("file", ""))
            ok = await bot.set_qq_avatar(file)
            return f"头像已更新 ✓" if ok else "头像更新失败"

        elif tool_name == "set_typing":
            ok = await bot.set_input_status(user_id, event_type=1)
            return "正在输入..." if ok else ""

        # ── 群管理（部分需批准） ──
        elif tool_name == "mute_self":
            minutes = min(int(tool_input.get("minutes", 10)), 60)
            ok = await bot.set_group_ban(group_id, user_id, minutes * 60)
            return f"已自我禁言 {minutes} 分钟 ✓" if ok else "禁言失败"

        elif tool_name == "send_notice":
            title = str(tool_input.get("title", ""))[:50]
            content = str(tool_input.get("content", ""))[:500]
            # 将标题拼入正文：NapCat 群公告无 title 字段
            full = f"{title}\n{content}" if title else content
            ok = await bot.send_group_notice(group_id, full)
            return f"群公告已发布 ✓" if ok else "发布公告失败（可能不是管理员）"

        elif tool_name == "change_group_name":
            new_name = str(tool_input.get("new_name", ""))[:30]
            ok = await bot.set_group_name(group_id, new_name)
            return f"群名已改为「{new_name}」✓" if ok else "修改群名失败"

        elif tool_name == "set_admin":
            uid = int(tool_input.get("user_id", 0))
            enable = tool_input.get("enable", True)
            ok = await bot.set_group_admin(group_id, uid, enable)
            action = "设为" if enable else "取消"
            return f"已{action}管理员 ✓" if ok else f"{action}管理员失败"

        elif tool_name == "ban_member":
            uid = int(tool_input.get("user_id", 0))
            minutes = int(tool_input.get("minutes", 10))
            reason = tool_input.get("reason", "未提供理由")
            # 危险操作: 返回警告标记
            ok = await bot.set_group_ban(group_id, uid, minutes * 60)
            if ok:
                logger.warning(f"AI 执行了禁言: user={uid} {minutes}分钟 理由:{reason}")
            return f"已禁言 {uid} {minutes} 分钟 ({reason}) ✓" if ok else "禁言失败"

        elif tool_name == "kick_member":
            uid = int(tool_input.get("user_id", 0))
            reason = tool_input.get("reason", "未提供理由")
            ok = await bot.set_group_kick(group_id, uid)
            if ok:
                logger.warning(f"AI 执行了踢人: user={uid} 理由:{reason}")
            return f"已踢出 {uid} ({reason}) ✓" if ok else "踢人失败"

        elif tool_name == "whole_ban":
            enable = tool_input.get("enable", True)
            ok = await bot.set_group_whole_ban(group_id, enable)
            action = "开启" if enable else "关闭"
            if ok:
                logger.warning(f"AI 执行了全员禁言: {action}")
            return f"全员禁言已{action} ✓" if ok else "操作失败"

        # ── 群娱乐 ──
        elif tool_name == "at_all":
            from . import ai_brain
            msg = str(tool_input.get("message", ""))[:50]
            ok = await ai_brain.at_all(bot, group_id, msg)
            return f"@全体成员 {msg} ✓" if ok else "@全体失败（可能不是管理员）"

        elif tool_name == "send_sign":
            ok = await bot.send_group_sign(group_id)
            return "群签到卡片已发送 ✓" if ok else "发送签到失败"

        elif tool_name == "get_shut_list":
            lst = await bot.get_group_shut_list(group_id)
            if lst:
                names = [f"{m.get('user_id','?')}({m.get('duration','?')}s)" for m in lst[:10]]
                return f"被禁言成员: {', '.join(names)}"
            return "当前无人被禁言"

        elif tool_name == "get_signed_list":
            signed = await bot.get_group_signed_list(group_id)
            if signed:
                names = [f"{s.get('user_name','?') or s.get('user_id','?')}" for s in signed[:10]]
                return f"今日已签到 ({len(signed)}人): {', '.join(names)}"
            return "今天还没有人签到~"

        elif tool_name == "get_at_all_remain":
            result = await bot.get_group_at_all_remain(group_id)
            if result:
                return f"今日@全体剩余: {result.get('can_at_all_count','?')}次 | 群@全体剩余: {result.get('remain_at_all_count_for_group','?')}次 | 单用户@全体剩余: {result.get('remain_at_all_count_for_uin','?')}次"
            return "查询失败"

        elif tool_name == "set_group_todo":
            msg_id = str(tool_input.get("message_id", ""))
            msg_seq = str(tool_input.get("message_seq", ""))
            ok = await bot.set_group_todo(group_id, msg_id, msg_seq)
            return "已设为群接龙/待办 ✓" if ok else "设置失败"

        elif tool_name == "complete_todo":
            todo_id = str(tool_input.get("todo_id", ""))
            ok = await bot.complete_group_todo(group_id, todo_id)
            return "已完成接龙 ✓" if ok else "完成失败"

        # ── 群文件 ──
        elif tool_name == "get_group_files":
            folder_id = tool_input.get("folder_id", "")
            if folder_id:
                data = await bot.get_group_files_by_folder(group_id, folder_id)
            else:
                data = await bot.get_group_root_files(group_id)
            if data:
                files = data.get("files", [])[:10]
                folders = data.get("folders", [])[:5]
                lines = []
                for f in folders:
                    lines.append(f"📁 {f.get('folder_name','?')}(id:{f.get('folder_id','?')})")
                for f in files:
                    size = f.get("file_size", 0)
                    size_str = f"{size/1024:.0f}KB" if size < 1024*1024 else f"{size/1024/1024:.1f}MB"
                    lines.append(f"📄 {f.get('file_name','?')}({size_str} id:{f.get('file_id','?')})")
                return "\n".join(lines) if lines else "目录为空"
            return "获取文件列表失败"

        elif tool_name == "delete_file":
            file_id = str(tool_input.get("file_id", ""))
            reason = tool_input.get("reason", "未提供理由")
            ok = await bot.delete_group_file(group_id, file_id)
            if ok:
                logger.warning(f"AI 执行了删除文件: file={file_id} 理由:{reason}")
            return f"已删除文件 ({reason}) ✓" if ok else "删除失败"

        # ── OCR / 翻译 / 语音 ──
        elif tool_name == "ocr_image":
            image = str(tool_input.get("image", ""))
            result = await bot.ocr_image(image)
            if result:
                texts = result.get("texts", [])
                return "\n".join([t.get("text", "") for t in texts]) if texts else "未识别到文字"
            return "OCR识别失败"

        elif tool_name == "translate_en":
            text = str(tool_input.get("text", ""))
            result = await bot.translate_en2zh([text])
            return f"翻译: {result[0]}" if result and result else "翻译失败"

        elif tool_name == "speech_to_text":
            mid = int(tool_input.get("message_id", 0))
            text = await bot.fetch_ptt_text(mid)
            return f"语音转文字: {text}" if text else "语音转文字失败（可能不是语音消息或已过期）"

        elif tool_name == "check_url_safety":
            url = str(tool_input.get("url", ""))
            level = await bot.check_url_safely(url)
            levels = {1: "安全 ✅", 2: "未知 ⚠️", 3: "危险 🚫"}
            return f"URL安全检查: {levels.get(level, f'等级{level}')}" if level is not None else "检查失败"

        # ── 群公告 / 精华 / 相册 ──
        elif tool_name == "get_group_notices":
            notices = await bot.get_group_notice(group_id)
            if notices:
                lines = []
                for n in notices[:5]:
                    lines.append(f"📢 {n.get('title','无标题')} — {n.get('publish_time','?')}")
                    content = (n.get('content','') or n.get('msg',{}).get('text',''))[:60]
                    if content:
                        lines.append(f"  {content}")
                return "\n".join(lines) if lines else "暂无公告"
            return "暂无公告或获取失败"

        elif tool_name == "get_essence_list":
            essences = await bot.get_essence_msg_list(group_id)
            if essences:
                lines = []
                for e in essences[:5]:
                    sender = e.get("sender_nick", "?")
                    content = (e.get("content", "") or "")[:40]
                    lines.append(f"⭐ {sender}: {content}")
                return f"精华消息 ({len(essences)}条):\n" + "\n".join(lines) if lines else "暂无精华"
            return "暂无精华消息"

        elif tool_name == "get_group_albums":
            albums = await bot.get_qun_album_list(group_id)
            if albums:
                lines = [f"📸 {a.get('name','?')}(id:{a.get('album_id','?')}) — {a.get('total','?')}张" for a in albums[:5]]
                return "\n".join(lines) if lines else "暂无相册"
            return "暂无相册或获取失败"

        elif tool_name == "get_album_photos":
            album_id = str(tool_input.get("album_id", ""))
            photos = await bot.get_group_album_media_list(group_id, album_id)
            if photos:
                lines = [f"🖼️ {p.get('name','?') or p.get('media_id','?')}" for p in photos[:10]]
                return f"相册照片 ({len(photos)}张):\n" + "\n".join(lines) if lines else "相册为空"
            return "获取失败"

        # ── QQ空间 / 收藏 ──
        elif tool_name == "send_qzone_post":
            content = str(tool_input.get("content", ""))[:500]
            ok = await bot.send_qzone_msg(content)
            logger.warning(f"AI 执行了发空间说说: {content[:30]}...")
            return "空间说说已发布 ✓" if ok else "发布失败"

        elif tool_name == "get_collection_items":
            page = int(tool_input.get("page", 1))
            count = min(int(tool_input.get("count", 20)), 50)
            items = await bot.get_collection_list(page, count)
            if items:
                lines = [f"📌 {c.get('title','?')}: {c.get('content','')[:40]}" for c in items[:10]]
                return f"收藏列表 (页{page}):\n" + "\n".join(lines) if lines else "收藏为空"
            return "暂无收藏"

        elif tool_name == "create_new_collection":
            title = str(tool_input.get("title", ""))[:50]
            content = str(tool_input.get("content", ""))[:1000]
            ok = await bot.create_collection(title, content)
            return f"已收藏「{title}」✓" if ok else "收藏失败"

        # ── 群消息历史 ──
        elif tool_name == "get_chat_history":
            seq = int(tool_input.get("message_seq", 0))
            count = min(int(tool_input.get("count", 20)), 50)
            history = await bot.get_group_msg_history(group_id, seq, count)
            if history:
                msgs = history.get("messages", [])[:10]
                lines = []
                for m in msgs:
                    sender = m.get("sender", {}).get("nickname", "?")
                    text = (m.get("message", "") or m.get("raw_message", ""))[:50]
                    lines.append(f"[{m.get('time','?')}] {sender}: {text}")
                return "历史消息:\n" + "\n".join(lines) if lines else "无消息"
            return "获取历史消息失败"

        # ── 超管工具 ──
        elif tool_name == "get_group_list":
            groups = await bot.get_group_list()
            if groups:
                lines = [f"{g.get('group_name','?')}({g.get('group_id','?')})" for g in groups[:20]]
                return "所在群:\n" + "\n".join(lines)
            return "获取群列表失败"

        elif tool_name == "get_friend_list":
            friends = await bot.get_friend_list()
            if friends:
                lines = [f"{f.get('nickname','?')}({f.get('user_id','?')})" for f in friends[:20]]
                return "好友:\n" + "\n".join(lines)
            return "获取好友列表失败"

        elif tool_name == "get_stranger_info":
            uid = int(tool_input.get("user_id", 0))
            info = await bot.get_stranger_info(uid)
            if info:
                return f"{info.get('nickname','?')}({info.get('user_id','?')}) 性别:{info.get('sex','?')} 年龄:{info.get('age','?')}"
            return f"未找到用户 {uid}"

        elif tool_name == "get_group_detail":
            info = await bot.get_group_detail_info(group_id)
            if info:
                lines = []
                lines.append(f"群名: {info.get('group_name','?')}")
                lines.append(f"群介绍: {(info.get('group_memo','') or '')[:80]}")
                lines.append(f"群分类: {info.get('group_class_text','?')}")
                lines.append(f"群标签: {','.join(info.get('group_tags',[]))}")
                lines.append(f"创建者: {info.get('owner_uin','?')}")
                return "\n".join(lines)
            return "获取群详情失败"

        elif tool_name == "get_group_system_msgs":
            count = int(tool_input.get("count", 50))
            data = await bot.get_group_system_msg(count)
            if data:
                msgs = data.get("join_requests", []) or data.get("invited_requests", [])
                if msgs:
                    lines = [f"{m.get('requester_nick','?')} 申请加入群{m.get('group_id','?')}: {m.get('message','')[:40]}" for m in msgs[:5]]
                    return f"系统消息 ({len(msgs)}条):\n" + "\n".join(lines)
                return "暂无系统消息"
            return "获取系统消息失败"

        elif tool_name == "get_ignored_requests":
            data = await bot.get_group_ignore_add_request()
            if data:
                msgs = data.get("join_requests", []) or []
                if msgs:
                    lines = [f"{m.get('requester_nick','?')}({m.get('requester_uin','?')}) — {m.get('message','')[:40]}" for m in msgs[:5]]
                    return f"被忽略的加群请求 ({len(msgs)}条):\n" + "\n".join(lines)
                return "没有被忽略的请求"
            return "获取失败"

        else:
            return f"未知工具: {tool_name}"

    except Exception as e:
        return f"操作失败: {str(e)[:60]}"
