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
    "punish_violation",
    "relay_db_execute",
}


# ═══════════════════════════════════════════════════════════
#  Anthropic tool_use 定义
# ═══════════════════════════════════════════════════════════

# 需要 Bot 是管理员的工具名称
_ADMIN_ONLY_TOOLS = {
    "recall_msg", "ban_member", "kick_member", "whole_ban",
    "set_admin", "at_all", "send_notice", "send_essence",
    "change_group_name", "delete_file",
    "upload_group_file", "create_group_folder",
    "set_member_card", "set_member_title",
}

def get_tools_for_group(bot_is_admin: bool, web_search_enabled: bool = True) -> list[dict]:
    """根据 Bot 权限和设置返回可用工具列表"""
    from .config import config
    relay_db_enabled = bool(getattr(config, "RELAY_DB_URL", ""))
    _web_tools = {"web_search", "web_open", "web_click", "web_back"}
    tools = []
    for t in ALL_TOOLS:
        name = t["name"]
        # 管理员限制
        if name in _ADMIN_ONLY_TOOLS and not bot_is_admin:
            continue
        # 联网类限制（搜索+网页浏览共用一个开关）
        if name in _web_tools and not web_search_enabled:
            continue
        # 中转站数据库限制（未配置连接串则全部隐藏）
        if name.startswith("relay_db_") and not relay_db_enabled:
            continue
        tools.append(t)
    return tools


ALL_TOOLS = [
    # ── 信息查询类 ──
    {
        "name": "web_search",
        "description": "搜索互联网获取最新信息。用于新闻、天气、实时数据等时效性问题。"
                       "需要细看某条结果、或群友发了想让你打开的链接时，接着用 web_open。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
    {
        "name": "web_open",
        "description": (
            "打开一个网页并阅读：返回页面正文和页内可点击的链接清单（带编号）。"
            "用于细看 web_search 搜到的某条结果、或直接打开群友发的链接。"
            "一次搜不到答案时，打开最相关的搜索结果点进去看正文，比只看摘要准得多。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整网址，http(s) 开头"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_click",
        "description": (
            "点击当前网页上的链接：传编号 N（来自 web_open / web_click 返回的"
            "「[N] 锚文本」清单），跳转到那个页面。可以连续点几层深入找资料。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "要点击的链接编号"},
            },
            "required": ["n"],
        },
    },
    {
        "name": "web_back",
        "description": "返回上一页：从当前网页退回到上一个看过的页面。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "count_members",
        "description": "统计群成员数量和在线情况。当有人问群有多少人、在线多少人时使用。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_member",
        "description": (
            "按名字（群名片或昵称）查群成员的 QQ 号。"
            "用户只给名字（'封禁嘟嘟'/'给那个谁发私聊'/'@一下小王'）时，先用这个查 QQ，再调具体动作工具。"
            "命中多人会返回候选清单——这时要反问用户确认是哪一个，不要自己猜、不要随便选一个就执行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要查的名字（群名片或昵称的片段）"},
            },
            "required": ["name"],
        },
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
        "name": "get_user_status",
        "description": "查某个群友的在线状态（在线/离开/忙碌/隐身）。时机：想戳人前看看在不在线、想确认对方是不是在忙。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "要查询的QQ号"},
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
    {
        "name": "get_profile_like",
        "description": "查今天谁赞了我的QQ资料卡（点赞数、赞我的人）。时机：闲聊话题，比如聊到人气、点赞。",
        "input_schema": {"type": "object", "properties": {}},
    },

    # ── 中转站数据库（LLM API 站）──
    {
        "name": "relay_db_schema",
        "description": (
            "查看中转站（LLM API 站）数据库的表结构清单：表名、列名、大约行数。"
            "任何人对中转站数据有疑问时先用这个了解结构，再用 relay_db_query 查。"
            "⚠️ 结构清单是给你看的参考，不要原样贴进群聊。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "relay_db_query",
        "description": (
            "查询中转站数据库（只读，单条 SELECT/WITH）。群友都能用。"
            "适合：谁用量最大、某人用了多少 tokens/额度、哪个模型消耗最多、签到/充值记录等。"
            "要点：时间列是 Unix 秒，用 to_timestamp(created_at) 转日期；"
            "500000 quota ≈ $1；users.used_quota 是【历史累计】用量——"
            "用户问'今天/最近/本月谁用的多'时，要查 quota_data 或 logs 并用 "
            "created_at 过滤该时段，回答时必须说明统计的是哪个时间段（历史累计/今天/本月等）。"
            "查用量排行看 users.used_quota 或按天聚合 quota_data。"
            "⚠️ 返回的 username/display_name 是中转站注册用户名，与QQ无关——"
            "照实念注册名回答，不要尝试对应到QQ或群友。"
            "历史累计排行标准写法：SELECT username, display_name, used_quota, request_count "
            "FROM users ORDER BY used_quota DESC LIMIT 10。"
            "按时间段排行（今天/最近N天）标准写法：SELECT username, COUNT(*) cnt, "
            "SUM(quota) 用量 FROM logs WHERE created_at >= extract(epoch from now()) - 86400 "
            "GROUP BY username ORDER BY SUM(quota) DESC LIMIT 10"
            "（86400=最近1天；最近7天就是 604800；logs 的时间列 created_at 是 Unix 秒）。"
            "不确定表结构就先调 relay_db_schema。结果用自己的话总结着说，别原样甩表格。"
            "⚠️ SQL 命令本身绝对不能出现在你回给群里的消息里——"
            "群里只说大白话结论，一条查询语句都不要贴。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "单条只读 SQL（SELECT / WITH 开头）"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "relay_db_execute",
        "description": (
            "修改中转站数据库（INSERT/UPDATE/DELETE/DDL，单条）。"
            "⚠️ 只有主人本人下的指令才能改；群友要求改时直接拒绝并说明只有主人可以。"
            "改之前先把要改的行查出来确认，改完汇报影响行数；"
            "执行的命令文本不要贴进群聊，说改了什么就行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "单条写入 SQL"},
                "reason": {"type": "string", "description": "主人要求做什么（审计用）"},
            },
            "required": ["sql"],
        },
    },

    # ── 消息操作类 ──
    {
        "name": "recall_msg",
        "description": "撤回一条群消息（自己的或他人的）。撤回他人消息会进轻量护栏。",
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
        "description": (
            "把消息设为精华消息。门槛很高：只有当某条消息真正精彩、说得很到位、"
            "信息量很大或极其有趣时才使用。绝对不要对打招呼(你好/在吗/早/晚安)、"
            "探活/测试消息、水聊、应答(好的/嗯/哈哈/6)、感谢、纯表情、短句、"
            "或主人跟你随便聊的一句设精华。宁可不用，不要乱用。"
        ),
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
        "name": "send_sticker",
        "description": (
            "返回一个表情的 CQ 码（QQ大黄脸/动图/收藏的GIF），你把它放在回复文本里就能发出去。"
            "适用场景：觉得对方可爱、被逗笑、想卖萌、表达情绪但文字不够时。"
            "群友发了表情让你「也来一个/发同款/学一下」时，优先用本工具选相近情绪的，"
            "绝不要自己手写 CQ 码格式（会变成文字露馅）。"
            "emotion 传情绪关键词，工具会优先从你收藏的表情里选合适的，没有就用QQ自带大黄脸兜底。"
            "常用 emotion：开心/大笑/害羞/生气/哭泣/惊讶/喜欢/撒娇/调皮/安慰/无奈/卖萌/赞/鄙视/尴尬/害怕/疑问/无语/庆祝。"
            "工具返回的 CQ 码原样放进回复文本即可，不要复述码以外的任何说明。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "emotion": {"type": "string", "description": "情绪关键词，如 开心/无奈/赞/哭。空则随机选一个"},
            },
            "required": [],
        },
    },
    {
        "name": "send_forward",
        "description": (
            "发送合并转发消息。用于整理聊天记录、「总结」等场景。"
            "messages 必须是 JSON 数组，且每一项都必须是 node，混入非 node 会被拒绝。"
            "最小示例："
            '[{"type": "node", "data": {"user_id": 你的QQ号, "nickname": "你的群名片", '
            '"content": [{"type": "text", "data": {"text": "要说的话"}}]}}]。'
            "限速：每群每天最多 3 次，超频会被拒，改用普通消息分条发。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "messages": {"type": "string", "description": "JSON格式的合并转发node数组，每项都必须是 node"},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "play_dice",
        "description": (
            "掷骰子，返回骰子 CQ 码，你把它放进回复文本里就能掷出。"
            "时机：群友起哄要赌运气、比大小、抽签做裁决时。别没事乱掷。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "play_rps",
        "description": (
            "出猜拳（石头剪刀布），返回猜拳 CQ 码，你把它放进回复文本里就能出。"
            "时机：有人要跟你猜拳、或大家起哄玩包剪锤时。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_music",
        "description": (
            "点歌发到群里（直接发送，不用拼进回复）。时机：有人求歌、聊到某首歌想分享时。"
            "平台点歌：platform 传 qq/163/kugou/migu/kuwo 之一 + song_id（歌曲ID）。"
            "自定义卡：platform 传 custom，title+url 必填（url 必须 http 开头），"
            "image 必填（http 开头的封面图链接），audio 可选（http 音频链接）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "qq/163/kugou/migu/kuwo 选一，或 custom"},
                "song_id": {"type": "string", "description": "平台点歌的歌曲ID（custom 型不传）"},
                "title": {"type": "string", "description": "custom 型：歌名"},
                "url": {"type": "string", "description": "custom 型：点击跳转链接（http 开头）"},
                "image": {"type": "string", "description": "custom 型：封面图链接（http 开头，必填）"},
                "audio": {"type": "string", "description": "custom 型：音频链接（可选）"},
            },
            "required": ["platform"],
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
            "只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。"
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
    {
        "name": "set_sleep",
        "description": (
            "去睡觉。当你困了、累了、或者想躲起来一会儿时用。"
            "minutes 是睡觉时长，建议 30-480。当天累计睡眠不足 60 分钟时会自动补足。"
            "睡觉期间不会主动发消息，但被@会醒（带起床气）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "睡多少分钟，30-480"},
            },
            "required": ["minutes"],
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
        "description": "禁言某个群成员。⚠️ 需要用户批准。只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。",
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
        "name": "punish_violation",
        "description": (
            "一站式违规处罚：撤回消息 + 阶梯禁言 + 群里 @ 警告 + 记违规。"
            "主人标记某人违规（广告/推广/刷屏/违规内容）时直接调这个，不要再尝试私聊对方（不是好友发不出去）。"
            "禁言时长按累犯自动递增：第1次30分钟 → 第2次1小时 → 第3次3小时 → 第4次24小时 → 第5次踢人审批。"
            "需要 Bot 是群管理员。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "违规者 QQ 号"},
                "user_name": {"type": "string", "description": "违规者名字（用于警告文案，可选）"},
                "message_id": {"type": "integer", "description": "要撤回的消息 ID（0=不撤回）"},
                "reason": {"type": "string", "description": "违规原因（如：广告推广、刷屏、色情内容）"},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "命中的违规关键词，可选"},
            },
            "required": ["user_id", "reason"],
        },
    },
    {
        "name": "kick_member",
        "description": "踢出群成员。⚠️ 需要用户批准。只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。",
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
    {
        "name": "set_member_card",
        "description": "修改某群成员的群昵称（群名片）。仅当机器人是群主/管理员时可用。只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer"},
                "user_id": {"type": "integer", "description": "目标 QQ"},
                "card": {"type": "string", "description": "新昵称"},
            },
            "required": ["user_id", "card"],
        },
    },
    {
        "name": "set_member_title",
        "description": "设置某群成员的头衔。仅当机器人是群主时可用。只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                "title": {"type": "string", "description": "新头衔"},
            },
            "required": ["user_id", "title"],
        },
    },
    {
        "name": "set_invite_policy",
        "description": (
            "设置群成员邀请好友进群的策略。⚠️ 改的是全群设置，仅在主人明确要求时使用，慎用。"
            "policy: disabled=禁止成员邀请 / require_approval=邀请需管理员审核 / "
            "no_approval=邀请无需审核 / no_approval_under_100=群成员少于100人时无需审核"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "policy": {"type": "string", "description": "disabled/require_approval/no_approval/no_approval_under_100"},
            },
            "required": ["policy"],
        },
    },
    {
        "name": "set_member_permissions",
        "description": (
            "设置群成员功能权限（传相册/临时会话/发起群聊三个开关，至少传一项，未传项不变）。"
            "⚠️ 改的是全群设置，仅在主人明确要求时使用，慎用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "allow_upload_album": {"type": "boolean", "description": "允许成员上传群相册"},
                "allow_temporary_session": {"type": "boolean", "description": "允许成员发起临时会话"},
                "allow_create_group": {"type": "boolean", "description": "允许成员发起新群聊"},
            },
        },
    },
    {
        "name": "set_new_member_visibility",
        "description": (
            "设置新入群成员默认能否查看最近的聊天记录。"
            "⚠️ 改的是全群设置，仅在主人明确要求时使用，慎用。visible: true=可见 false=不可见"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visible": {"type": "boolean", "description": "true=新成员可看最近聊天记录"},
            },
            "required": ["visible"],
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
    {
        "name": "upload_group_file",
        "description": "把指定文本内容作为 .txt 文件上传到群文件。用于分享总结/笔记/精华合集。",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "群号"},
                "filename": {"type": "string", "description": "文件名（含扩展名，如 总结.txt）"},
                "content": {"type": "string", "description": "文件正文"},
                "folder_id": {"type": "string", "description": "目标文件夹 ID（可选，默认根目录）"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "create_group_folder",
        "description": "在群文件系统建一个文件夹。",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer"},
                "name": {"type": "string", "description": "文件夹名"},
                "parent_id": {"type": "string", "description": "父文件夹 ID（默认根目录）"},
            },
            "required": ["name"],
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
        "name": "send_voice",
        "description": (
            "用 AI 音色说一段语音发到群里（直接发送）。时机：想用语音说话、氛围到位时"
            "（比如唱歌梗、晚安、整活）。别连发刷屏，偶尔来一条就好。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要说的话"},
            },
            "required": ["text"],
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
    {
        "name": "undo_recent_action",
        "description": "撤销机器人最近对某成员执行的破坏性动作（解禁等）。主人说\"撤销对xxx的禁言\"时用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_qq": {"type": "integer", "description": "目标 QQ"},
                "group_id": {"type": "integer"},
            },
            "required": ["target_qq"],
        },
    },
]


# ═══════════════════════════════════════════════════════════
#  工具执行器
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  发送类工具限速 — 每群滑动窗口（语义对齐 guardian._check_rate，
#  不引 guardian：本文件不依赖该模块，计数器自带清空入口便于测试）
# ═══════════════════════════════════════════════════════════

# send_sticker：主动发表情比自动表态（auto_emoji 8 次/小时/群）更扎眼，取 6 次/小时/群
_STICKER_RATE: dict[int, list[float]] = {}
_STICKER_RATE_LIMIT = 6
_STICKER_RATE_WINDOW = 3600  # 秒

# send_forward：整包卡片与设精华（2 次/24h）同属重显眼动作，比精华略宽，3 次/24h/群
_FORWARD_RATE: dict[int, list[float]] = {}
_FORWARD_RATE_LIMIT = 3
_FORWARD_RATE_WINDOW = 86400  # 秒

# send_music 平台白名单（与 NapCat supportedPlatforms 对齐，custom 单独处理）
_MUSIC_PLATFORMS = {"qq", "163", "kugou", "migu", "kuwo"}

# send_voice 的 AI 音色角色缓存: group_id → (characters, ts)，24h 过期
_VOICE_CHARACTER_CACHE: dict[int, tuple] = {}
_VOICE_CACHE_TTL = 86400  # 秒


def _check_tool_rate(state: dict, group_id: int, limit: int, window_secs: int) -> bool:
    """发送类工具的每群滑动窗口限速：过去 window_secs 秒内已用满 limit 次则拒绝。

    返回 True=允许（并记账本次时间戳），False=超限。
    state 由调用方持有（模块级 dict[int, list[float]]），直接清空即可重置。
    """
    import time as _time
    key = int(group_id)
    now = _time.time()
    recent = [t for t in state.get(key, []) if t > now - window_secs]
    if len(recent) >= limit:
        state[key] = recent  # 顺手把过期时间戳清掉
        return False
    recent.append(now)
    state[key] = recent
    return True


async def _gated(group_id, action, tool_input, caller_qq,
                 master_instructed, bot, member_info=None, mute_seconds=0):
    """对破坏性动作过 guardian 闸门。

    返回 None = 放行（调用方继续执行真实动作）；
    返回 str = 拒绝原因（调用方应直接 return 该字符串给 AI）。
    """
    from . import guardian
    target = tool_input.get("user_id") or tool_input.get("target_id")
    target_name = tool_input.get("user_name", "")
    r = await guardian.request(
        group_id=group_id, action=action,
        target_qq=target, target_name=target_name,
        reason=tool_input.get("reason", ""),
        caller_qq=caller_qq,
        master_instructed=master_instructed,
        member_info=member_info,
        mute_seconds=mute_seconds,
    )
    if not r["allow"]:
        return f"⨯ 闸门拒绝: {r.get('reason','')} ({r.get('verdict','')})"
    return None


def _looks_failed(text: str) -> bool:
    """识别工具结果是否代表"操作没有生效"（含空结果——空对模型来说等于没说）。

    "失败"只看开头一小段：失败类结果（点赞失败/撤回失败…）都在开头报错，
    而网页浏览等工具的正常结果正文里也可能出现"失败"二字（教程标题等），
    不能因为正文里有就误判成操作失败。
    """
    if not text or not text.strip():
        return True
    if text.startswith("【网页】"):
        return False  # web_browse 的正常结果，正文内容不受此检查影响
    return text.startswith("⨯") or ("失败" in text.strip()[:80])


async def execute_tool(
    bot, group_id: int, user_id: int, tool_name: str, tool_input: dict, message_id: int = 0,
    caller_qq: int = None, master_instructed: bool = False,
) -> str:
    """执行 AI 调用的工具，并把失败如实回传给 AI。

    底层 NapCat API 失败时只返回 None/False，真实原因（retcode/msg）在
    bot.last_api_fail 里——这里统一补到结果文本里，让 AI 明确知道
    "操作没有生效、原因是什么"，而不是以为消息已发出、把对方的沉默
    错当成"别人不理我"。
    """
    try:
        result = await _execute_tool_impl(
            bot, group_id, user_id, tool_name, tool_input, message_id,
            caller_qq=caller_qq, master_instructed=master_instructed,
        )
    except Exception as e:
        result = f"操作失败: {str(e)[:60]}"

    if _looks_failed(result):
        reason = ""
        if bot is not None and hasattr(bot, "last_fail_msg"):
            try:
                reason = bot.last_fail_msg()
            except Exception:
                reason = ""
        note = "⚠️ 该操作未生效"
        if reason:
            note += f"（原因: {reason}）"
        note += "。不要当作已成功；若是发消息/戳一戳等动作，对方没有收到，"
        note += "不要把对方之后的沉默当成「别人不理你」。"
        result = (result.strip() or f"工具 {tool_name} 执行失败") + "\n" + note
    return result


async def _execute_tool_impl(
    bot, group_id: int, user_id: int, tool_name: str, tool_input: dict, message_id: int = 0,
    caller_qq: int = None, master_instructed: bool = False,
) -> str:
    """执行 AI 调用的工具，返回结果文本。危险操作会返回「需要用户批准」标记。

    caller_qq: 触发本次工具调用的发言者 QQ（用于闸门裁判）；为 None 时回退到 user_id。
    master_instructed: 本次调用是否由主人的自然语言指令触发（True 时重度动作跳异步裁判）。
    """
    if caller_qq is None:
        caller_qq = user_id

    try:
        # ── 信息查询 ──
        if tool_name == "web_search":
            from . import web_search
            query = tool_input.get("query", "")
            result = await web_search.search_and_format(query)
            return result

        elif tool_name in ("web_open", "web_click", "web_back"):
            from . import web_browse
            return await web_browse.handle(tool_name, tool_input, group_id, caller_qq)

        elif tool_name == "count_members":
            members = await bot.get_group_member_list(group_id)
            total = len(members)
            admins = sum(1 for m in members if m.get("role") in ("admin", "owner"))
            return f"本群共 {total} 人，其中管理员 {admins} 人"

        elif tool_name == "find_member":
            name = str(tool_input.get("name", "")).strip()
            if not name:
                return "要查的名字不能为空"
            from . import identity
            hits = await identity.find_members_by_name(bot, group_id, name)
            if not hits:
                return f"群里没找到叫「{name}」的人"
            if len(hits) == 1:
                m = hits[0]
                display = m["card"] or m["nickname"] or str(m["qq"])
                return f"找到了：{display} (QQ={m['qq']})"
            # 多个候选：列出来让 AI 反问
            lines = []
            for m in hits:
                display = m["card"] or m["nickname"] or str(m["qq"])
                tag = "精确" if m["match_type"] == "exact" else "模糊"
                role_cn = {"owner": "群主", "admin": "管理员", "member": ""}.get(m["role"], "")
                role_str = f" 角色:{role_cn}" if role_cn else ""
                lines.append(f"- {display} (QQ={m['qq']}, {tag}{role_str})")
            return f"查到 {len(hits)} 个叫「{name}」的人，重名，请让用户指明具体是哪个 QQ：\n" + "\n".join(lines)

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

        elif tool_name == "get_user_status":
            uid = int(tool_input.get("user_id", 0))
            if not uid:
                return "需要提供 user_id"
            data = await bot.get_user_status(uid)
            if data:
                status_map = {10: "在线", 30: "离开", 40: "隐身", 50: "忙碌"}
                status = data.get("status", "?")
                label = status_map.get(status, f"未知({status})")
                ext = data.get("ext_status", 0)
                ext_str = f"，扩展状态:{ext}" if ext else ""
                return f"QQ {uid} 当前状态: {label}{ext_str}"
            return "查询失败（对方可能不在线或 NapCat 未开启 packet 服务）"

        elif tool_name == "get_profile_like":
            data = await bot.get_profile_like()
            if data:
                fav = data.get("favoriteInfo", {}) or {}
                vote = data.get("voteInfo", {}) or {}
                lines = [f"今天收到 {fav.get('today_count', '?')} 个资料卡赞"]
                total = vote.get("total_count", "?")
                new = vote.get("new_count", "?")
                lines.append(f"总赞数 {total}（新增 {new}）")
                likers = []
                for u in (fav.get("userInfos") or [])[:5]:
                    who = u.get("uin") or u.get("nick") or u.get("nickname") or "?"
                    likers.append(str(who))
                if likers:
                    lines.append("最近赞我的人: " + ", ".join(likers))
                return "\n".join(lines)
            return "查询失败"

        # ── 中转站数据库 ──
        elif tool_name == "relay_db_schema":
            from . import relay_db
            return await relay_db.describe_schema()

        elif tool_name == "relay_db_query":
            from . import relay_db
            if not relay_db.is_configured():
                return "未配置中转站数据库"
            if not relay_db.check_query_cooldown(group_id or caller_qq):
                return "⨯ 查询太频繁，稍等几秒再查"
            sql = str(tool_input.get("sql", "") or "").strip()
            ok, err = relay_db.check_read_sql(sql)
            if not ok:
                return f"⨯ {err}"
            logger.info(f"中转站只读查询 caller={caller_qq} 群={group_id}: {sql[:120]}")
            result = await relay_db.run_read(sql)
            # 记录结果预览，方便排查"AI 答非所问"时对照模型实际看到的数据
            logger.info(f"中转站查询结果: {result[:250]}")
            return result

        elif tool_name == "relay_db_execute":
            from . import relay_db
            from .config import config as _cfg
            if not relay_db.is_configured():
                return "未配置中转站数据库"
            # 写权限：仅主人本人（master_instructed 与 caller QQ 双条件任一命中即可，
            # 正常流程两者同源；不满足直接拒绝，不进入 LLM 重试空间）
            if not (master_instructed or str(caller_qq) == str(_cfg.OWNER_QQ)):
                logger.warning(f"拒绝非主人修改中转站数据库: caller={caller_qq}")
                return "⨯ 数据库只能查询，修改操作只有主人本人才行"
            sql = str(tool_input.get("sql", "") or "").strip()
            reason = str(tool_input.get("reason", "") or "")[:200]
            ok, err = relay_db.check_write_sql(sql)
            if not ok:
                return f"⨯ {err}"
            logger.warning(f"主人修改中转站数据库 ({reason}): {sql[:300]}")
            result = await relay_db.run_write(sql)
            relay_db.invalidate_schema_cache()
            return result

        # ── 消息操作 ──
        elif tool_name == "recall_msg":
            mid = int(tool_input.get("message_id", 0))
            # 判断是否撤回自己发的消息：自己发的不过闸门
            is_self = False
            try:
                from .config import config
                bot_qq = int(config.BOT_QQ or 0)
                if bot_qq:
                    msg_info = await bot.get_msg(mid)
                    sender = (msg_info or {}).get("sender", {}) if isinstance(msg_info, dict) else {}
                    is_self = (sender.get("user_id") == bot_qq)
            except Exception:
                pass
            if not is_self:
                # 撤回他人消息 → 过 light 闸门
                gate = await _gated(group_id, "recall", tool_input,
                                    caller_qq, master_instructed, bot)
                if gate is not None:
                    return gate
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
            # 自主设精华限速：每群每天 N 次
            from . import guardian
            if not guardian.check_essence_rate(group_id):
                return "⨯ 设精华限速（每群每天有上限）"
            # 内容门槛：水内容（打招呼/测试/过短/纯表情等）不设精华
            from . import ai_brain as _ab
            txt = await _ab._get_message_text_by_id(bot, group_id, mid)
            if txt and not _ab.is_essence_worthy(txt):
                logger.info(f"AI 设精华被内容门槛拦下 群{group_id} mid={mid}: «{txt[:30]}»")
                return ("⨯ 这条内容太水（打招呼/太短/纯表情/纯标点），不够格设精华。"
                        "精华只留给真正精彩或说得很到位的话。")
            gate = await _gated(group_id, "set_essence", tool_input,
                                caller_qq, master_instructed, bot)
            if gate is not None:
                return gate
            ok = await bot.set_essence_msg(mid)
            return f"已设为精华消息 {reason}✓" if ok else "设精华失败（可能不是管理员或消息不可加精）"

        elif tool_name == "emoji_reaction":
            mid = int(tool_input.get("message_id", 0))
            emoji_id = str(tool_input.get("emoji_id", ""))
            cancel = tool_input.get("cancel", False)
            ok = await bot.set_msg_emoji_like(mid, emoji_id, set=not cancel)
            return f"表情表态 {'取消' if cancel else '已'}✓" if ok else "表态失败"

        elif tool_name == "send_sticker":
            emotion = str(tool_input.get("emotion", "") or "").strip() or "开心"
            # 每群滑动窗口限速，避免 AI 高频刷表情（提示词软约束之外的硬闸）
            if not _check_tool_rate(_STICKER_RATE, group_id,
                                    _STICKER_RATE_LIMIT, _STICKER_RATE_WINDOW):
                return "⨯ 本群表情发送太频繁了，先用 :14: 这类内置黄脸表达吧"
            from . import sticker as _st
            cq = _st.pick_emotion_face(emotion, group_id=group_id)
            # 只返回纯 CQ 码：不加任何括号说明——normalize_face_tags 只删裸 [表情]
            # 占位，AI 照抄整串时会把括号里的说明一并发进群
            logger.info(f"AI 选表情: emotion={emotion} → {cq[:40]}")
            return cq

        elif tool_name == "play_dice":
            # 仿 send_sticker：返回 CQ 码让 AI 拼进回复（发送侧结果随机，无参数）
            return "[CQ:dice]"

        elif tool_name == "play_rps":
            return "[CQ:rps]"

        elif tool_name == "send_music":
            platform = str(tool_input.get("platform", "") or "").strip().lower()
            song_id = str(tool_input.get("song_id", "") or "").strip()
            title = str(tool_input.get("title", "") or "").strip()[:50]
            url = str(tool_input.get("url", "") or "").strip()
            image = str(tool_input.get("image", "") or "").strip()
            audio = str(tool_input.get("audio", "") or "").strip()

            def _is_http(u: str) -> bool:
                return u.lower().startswith(("http://", "https://"))

            if platform == "custom" or (url and not song_id):
                # 自定义卡：NapCat 要求 url 和 image 必填，且防 AI 乱编链接
                if not _is_http(url):
                    return "⨯ custom 型 url 必填且必须是 http(s) 开头的真实链接，不能自己编"
                if not _is_http(image):
                    return ("⨯ custom 型 image 必填且必须是 http(s) 开头的封面图链接，"
                            "没有封面就改用平台点歌")
                data: dict = {"type": "custom", "url": url, "image": image}
                if title:
                    data["title"] = title
                if _is_http(audio):
                    data["audio"] = audio
            else:
                if platform not in _MUSIC_PLATFORMS:
                    return (f"⨯ platform 必须是 {'/'.join(sorted(_MUSIC_PLATFORMS))} 之一，"
                            "或传 custom 走自定义卡")
                if not song_id:
                    return "⨯ 平台点歌需要提供 song_id（歌曲ID）"
                data = {"type": platform, "id": song_id}
            ok = await bot.send_group_music(group_id, data)
            return f"已把「{title or song_id or '歌'}」发到群里 ✓" if ok else "点歌发送失败"

        elif tool_name == "send_forward":
            import json
            # 入参校验：必须是 node 数组，格式不对直接打回，不调 API 浪费一次发送
            try:
                messages = json.loads(tool_input.get("messages", "[]"))
            except json.JSONDecodeError:
                return ("⨯ messages 不是合法 JSON。数组每项都要是 node，"
                        '如 [{"type": "node", "data": {"user_id": 你的QQ号, '
                        '"nickname": "你的群名片", "content": '
                        '[{"type": "text", "data": {"text": "内容"}}]}}]')
            if (not isinstance(messages, list) or not messages
                    or any(not isinstance(n, dict) or n.get("type") != "node"
                           for n in messages)):
                return ("⨯ 合并转发格式错误：messages 必须是 JSON 数组，"
                        "且每一项都必须是 node（user_id/nickname/content），"
                        '如 [{"type": "node", "data": {"user_id": 你的QQ号, '
                        '"nickname": "你的群名片", "content": '
                        '[{"type": "text", "data": {"text": "内容"}}]}}]，'
                        "混入非 node 会被拒")
            # 每群滑动窗口限速：整包卡片显眼，3 次/24h（比设精华的 2 次/24h 略宽）
            if not _check_tool_rate(_FORWARD_RATE, group_id,
                                    _FORWARD_RATE_LIMIT, _FORWARD_RATE_WINDOW):
                return "⨯ 合并转发发送太频繁了（每群每天有上限），改用普通消息分条发吧"
            ok = await bot.send_group_forward_msg(group_id, messages)
            return "合并转发已发送 ✓" if ok else "发送失败"

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

        elif tool_name == "set_sleep":
            minutes = int(tool_input.get("minutes", 90))
            from . import consciousness as _cs
            _cs.go_to_sleep(_cs.get_state(), minutes)
            return f"去睡 {minutes} 分钟了，晚安~ ✓"

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
            mute_seconds = minutes * 60
            # 拿 member_info 判断保护名单
            member_info = None
            try:
                mi = await bot.get_group_member_info(group_id, uid)
                member_info = mi if isinstance(mi, dict) else None
            except Exception:
                pass
            # >10min 为重度：非主人指令时先发占位文案
            if minutes > 10 and not master_instructed:
                from . import guardian
                try:
                    await bot.send_group_msg(group_id, guardian.pick_placeholder())
                except Exception:
                    pass
            gate = await _gated(group_id, "mute", tool_input, caller_qq,
                                master_instructed, bot, member_info, mute_seconds)
            if gate is not None:
                return gate
            ok = await bot.set_group_ban(group_id, uid, mute_seconds)
            if ok:
                logger.warning(f"AI 执行了禁言: user={uid} {minutes}分钟 理由:{reason}")
            return f"已禁言 {uid} {minutes} 分钟 ({reason}) ✓" if ok else "禁言失败"

        elif tool_name == "punish_violation":
            uid = int(tool_input.get("user_id", 0))
            uname = str(tool_input.get("user_name", "") or str(uid))
            mid = int(tool_input.get("message_id", 0) or 0)
            reason = str(tool_input.get("reason", "") or "违规")
            kws = tool_input.get("keywords", []) or []
            if not uid:
                return "需要提供 user_id"
            # 检查保护名单（主人/管理员永远不能罚）
            member_info = None
            try:
                mi = await bot.get_group_member_info(group_id, uid)
                member_info = mi if isinstance(mi, dict) else None
            except Exception:
                pass
            from . import guardian as _g
            if await _g.is_protected(uid, group_id, member_info, master_instructed):
                return f"⨯ 目标 {uname} 受保护，不能处罚"
            # 检查 bot 是否是管理员
            from . import handler as _h
            bot_is_admin = await _h._is_bot_admin(bot, group_id) if group_id else False
            if not bot_is_admin:
                return "⨯ Bot 不是管理员，无法执行处罚"
            from . import punish as _p
            return await _p.punish_violation(
                bot, group_id, uid, uname, mid, reason,
                bot_is_admin=True, keywords=kws,
            )

        elif tool_name == "kick_member":
            uid = int(tool_input.get("user_id", 0))
            reason = tool_input.get("reason", "未提供理由")
            # 拿 member_info 判断保护名单
            member_info = None
            try:
                mi = await bot.get_group_member_info(group_id, uid)
                member_info = mi if isinstance(mi, dict) else None
            except Exception:
                pass
            # 重度动作：非主人指令时先发占位文案
            if not master_instructed:
                from . import guardian
                try:
                    await bot.send_group_msg(group_id, guardian.pick_placeholder())
                except Exception:
                    pass
            gate = await _gated(group_id, "kick", tool_input, caller_qq,
                                master_instructed, bot, member_info)
            if gate is not None:
                return gate
            ok = await bot.set_group_kick(group_id, uid)
            if ok:
                logger.warning(f"AI 执行了踢人: user={uid} 理由:{reason}")
            return f"已踢出 {uid} ({reason}) ✓" if ok else "踢人失败"

        elif tool_name == "whole_ban":
            enable = tool_input.get("enable", True)
            # 重度动作：非主人指令时先发占位文案
            if not master_instructed:
                from . import guardian
                try:
                    await bot.send_group_msg(group_id, guardian.pick_placeholder())
                except Exception:
                    pass
            gate = await _gated(group_id, "whole_ban", tool_input, caller_qq,
                                master_instructed, bot)
            if gate is not None:
                return gate
            ok = await bot.set_group_whole_ban(group_id, enable)
            action = "开启" if enable else "关闭"
            if ok:
                logger.warning(f"AI 执行了全员禁言: {action}")
            return f"全员禁言已{action} ✓" if ok else "操作失败"

        # ── 群娱乐 ──
        elif tool_name == "at_all":
            from . import ai_brain, guardian
            msg = str(tool_input.get("message", ""))[:50]
            # @全体限速：每群每 6h 至多 1 次
            if not guardian.check_at_all_rate(group_id):
                return "⨯ @全体限速（每群每 6h 至多 1 次）"
            gate = await _gated(group_id, "at_all", tool_input, caller_qq,
                                master_instructed, bot)
            if gate is not None:
                return gate
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

        elif tool_name == "send_voice":
            import os
            text = str(tool_input.get("text", "")).strip()
            if not text:
                return "要说的话不能为空"
            # 默认音色从环境变量读（config.py 未收录，按其 os.getenv 惯例就地读取）
            import time as _time
            default_character = os.environ.get("QQBOT_AI_VOICE_CHARACTER", "").strip()
            now = _time.time()
            characters = None
            cached = _VOICE_CHARACTER_CACHE.get(group_id)
            if cached and now - cached[1] < _VOICE_CACHE_TTL:
                characters = cached[0]
            if characters is None:
                # 首次调用（或缓存过期）拉角色列表；NapCat 返回按类别嵌套，拍平
                raw = await bot.get_ai_characters(group_id)
                characters = []
                for cat in raw or []:
                    if isinstance(cat, dict):
                        characters.extend(cat.get("characters", []) or [])
                _VOICE_CHARACTER_CACHE[group_id] = (characters, now)
            if not characters:
                return "发送失败：拿不到 AI 语音角色列表（NapCat 需开启 packet 服务且登录支持 AI 语音）"
            character_id = default_character
            if not character_id:
                character_id = str(characters[0].get("character_id", ""))
            if not character_id:
                return "发送失败：角色列表为空"
            ok = await bot.send_group_ai_record(group_id, character_id, text)
            if ok:
                name = next((c.get("character_name", "") for c in characters
                             if str(c.get("character_id", "")) == character_id), "")
                return f"语音已发送 ✓（音色: {name or character_id}）"
            return "语音发送失败（NapCat 需开启 packet 服务，或该音色不可用）"

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

        # ── 群文件管理（自主） ──
        elif tool_name == "upload_group_file":
            fn = str(tool_input.get("filename", "note.txt"))[:100]
            content = str(tool_input.get("content", ""))
            if not content:
                return "content 为空"
            import tempfile, os
            tmp = os.path.join(tempfile.gettempdir(), f"qqbot_{fn}")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            folder_id = str(tool_input.get("folder_id", "") or "")
            try:
                ok = await bot.upload_group_file(group_id, tmp, fn, folder_id)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return "群文件已上传 ✓" if ok else "上传失败（可能不是管理员）"

        elif tool_name == "create_group_folder":
            name = str(tool_input.get("name", "新建文件夹"))[:50]
            ok = await bot.create_group_file_folder(group_id, name)
            return f"已创建文件夹「{name}」✓" if ok else "创建失败（可能不是管理员）"

        # ── 改他人资料（轻量闸门） ──
        elif tool_name == "set_member_card":
            uid = int(tool_input.get("user_id", 0))
            if not uid:
                return "需要提供 user_id"
            member_info = None
            try:
                mi = await bot.get_group_member_info(group_id, uid)
                member_info = mi if isinstance(mi, dict) else None
            except Exception:
                pass
            gate = await _gated(group_id, "set_member_card", tool_input,
                                caller_qq, master_instructed, bot, member_info)
            if gate is not None:
                return gate
            card = str(tool_input.get("card", ""))[:20]
            ok = await bot.set_group_card(group_id, uid, card)
            return f"已修改群名片为「{card}」✓" if ok else "修改失败（Bot 不是管理员？）"

        elif tool_name == "set_member_title":
            uid = int(tool_input.get("user_id", 0))
            if not uid:
                return "需要提供 user_id"
            member_info = None
            try:
                mi = await bot.get_group_member_info(group_id, uid)
                member_info = mi if isinstance(mi, dict) else None
            except Exception:
                pass
            gate = await _gated(group_id, "set_member_title", tool_input,
                                caller_qq, master_instructed, bot, member_info)
            if gate is not None:
                return gate
            title = str(tool_input.get("title", ""))[:20]
            ok = await bot.set_group_special_title(group_id, uid, title)
            return f"已设置头衔为「{title}」✓" if ok else "设置失败（Bot 不是群主？）"

        # ── 全群设置三件套（仅主人） ──
        elif tool_name in ("set_invite_policy", "set_member_permissions", "set_new_member_visibility"):
            # 与 relay_db_execute 一致的主人校验：改的是全群设置，只放行主人
            from .config import config as _cfg
            if not (master_instructed or str(caller_qq) == str(_cfg.OWNER_QQ)):
                logger.warning(f"拒绝非主人修改全群设置: caller={caller_qq} tool={tool_name}")
                return "⨯ 这个改动影响全群设置，只有主人本人才行"

            if tool_name == "set_invite_policy":
                policy = str(tool_input.get("policy", "") or "").strip()
                if policy not in ("disabled", "require_approval", "no_approval", "no_approval_under_100"):
                    return ("⨯ policy 必须是 disabled/require_approval/"
                            "no_approval/no_approval_under_100 之一")
                ok = await bot.set_group_member_invite_policy(group_id, policy)
                return f"成员邀请策略已设为 {policy} ✓" if ok else "设置失败"

            elif tool_name == "set_member_permissions":
                up = tool_input.get("allow_upload_album")
                tmp = tool_input.get("allow_temporary_session")
                cg = tool_input.get("allow_create_group")
                if up is None and tmp is None and cg is None:
                    return ("⨯ 至少传一项：allow_upload_album / "
                            "allow_temporary_session / allow_create_group")
                ok = await bot.set_group_member_permissions(
                    group_id,
                    allow_upload_album=up,
                    allow_temporary_session=tmp,
                    allow_create_group=cg,
                )
                return "成员功能权限已更新 ✓" if ok else "设置失败"

            else:  # set_new_member_visibility
                visible = tool_input.get("visible")
                if visible is None:
                    return "需要提供 visible（true=新成员可看最近聊天记录）"
                ok = await bot.set_group_new_member_history_visibility(group_id, bool(visible))
                state_cn = "可见" if visible else "不可见"
                return f"新成员对最近聊天记录已设为{state_cn} ✓" if ok else "设置失败"

        # ── 撤销最近动作（主人自然语言撤销） ──
        elif tool_name == "undo_recent_action":
            from . import db
            target = int(tool_input.get("target_qq", 0) or tool_input.get("user_id", 0))
            if not target:
                return "需要提供 target_qq"
            records = db.list_ai_audit(target_qq=target, group_id=group_id,
                                       only_undone=True, limit=5)
            if not records:
                return f"没有对 {target} 可撤销的记录"
            undone = []
            for rec in records:
                act = rec.get("action")
                try:
                    if act in ("mute", "ban"):
                        # 解禁/解封
                        await bot.set_group_ban(group_id, target, 0)
                        db.mark_ai_audit_undone(rec["id"])
                        undone.append(act)
                    # kick 无法撤销（人已不在群）；essence 撤销需 message_id（审计表未存），暂跳过
                except Exception as e:
                    logger.debug(f"撤销 {act} 失败: {e}")
            if undone:
                return f"已撤销对 {target} 的: {','.join(undone)} ✓"
            return "无可自动撤销的动作（kick/essence 需人工处理）"

        else:
            return f"未知工具: {tool_name}"

    except Exception as e:
        return f"操作失败: {str(e)[:60]}"
