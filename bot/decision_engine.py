"""
AI 自主回复决策引擎
- 综合考虑多维度因素决定是否回复
- 因素：群友影响力、群人设、消息内容、时间、Bot 活跃度
- 受到群成员行为和群氛围的影响
"""
import asyncio
import logging
import random
import re
import time
from typing import Optional

from . import (
    group_personality, influence, sticker, media_reader,
    quote_handler, memory, personality, db,
)
from .config import config
from .onebot_client import OneBotClient

logger = logging.getLogger("qqbot")

# 全局冷却控制（防止 Bot 刷屏）
_last_reply_time: dict[int, float] = {}
GLOBAL_COOLDOWN = 10  # 全局冷却（秒）— 会话态下可以接得密一点

# 注：旧版 decision_engine 维护自己独立的 _bot_mood，跟 consciousness 的多维
# 情绪系统打架。现已统一——consciousness 是唯一权威，get_bot_mood 只读映射。


# ═══════════════════════════════════════════════════════════
#  Bot 情绪（只读自 consciousness）
# ═══════════════════════════════════════════════════════════

MOODS = {
    "happy": "开心，更愿意互动",
    "neutral": "平静，正常回复",
    "tired": "有点累，回复变少",
    "excited": "兴奋，积极参与",
}


def get_bot_mood(group_id: int) -> dict:
    """获取 Bot 情绪状态（只读自 consciousness 单一数据源）。

    旧版有独立的 mood 字典，跟 consciousness 打架（同一个概念两个值）。
    现在 consciousness 是唯一权威，这里只做格式映射供旧代码使用。
    """
    try:
        from . import consciousness as _cs
        state = _cs.get_state()
        mood_label = _cs.get_dominant_mood(state)
        e = state["emotions"]
        energy = e.get("energy", 0.7)
        # 映射到旧格式（excited/happy/neutral/tired）
        if mood_label == "excited":
            old_mood = "excited"
        elif mood_label in ("happy", "neutral_happy", "curious", "content"):
            old_mood = "happy"
        elif mood_label in ("tired", "sad", "sleeping"):
            old_mood = "tired"
        elif mood_label in ("grumpy", "irritated"):
            old_mood = "tired"  # 烦躁/不爽 → 不想多说
        else:
            old_mood = "neutral"
        return {"mood": old_mood, "energy": energy, "updated_at": time.time()}
    except Exception:
        return {"mood": "neutral", "energy": 0.7, "updated_at": time.time()}


def update_bot_mood(group_id: int, delta: float):
    """更新 Bot 情绪能量。

    现已委托给 consciousness 的事件钩子（on_bot_sent_message 等）统一管理，
    这里保留接口兼容但不维护独立状态，避免两套系统打架。
    """
    pass


# ═══════════════════════════════════════════════════════════
#  决策引擎
# ═══════════════════════════════════════════════════════════

async def should_reply_to_message(
    bot: OneBotClient,
    group_id: int,
    user_qq: int,
    user_name: str,
    message: str,
    is_at_bot: bool = False,
    is_reply_to_bot: bool = False,
    quoted_context: Optional[dict] = None,
) -> tuple[bool, str]:
    """
    决定 Bot 是否应该回复这条消息。

    三级决策:
      1. 确定性回复 — 被@、明确求助、聊到Bot自己 → 必回
      2. 高概率回复 — 有问题特征、情绪表达 → 大概率回
      3. 随机回复 — 普通闲聊 → 按概率碰运气

    Returns:
        (should_reply: bool, reason: str) — reason 用于日志
    """

    # 🟢 被 @ 了 → 100% 回复
    if is_at_bot:
        return True, "被@提及"

    # 检查冷却
    now = time.time()
    cooldown = _last_reply_time.get(group_id, 0)
    if now - cooldown < GLOBAL_COOLDOWN:
        return False, "全局冷却中"

    # 获取群设置和Bot名字
    gp = group_personality.get_group_personality(group_id)
    from .personality import load as _load_personality
    pcfg = _load_personality()
    bot_name = gp.get("bot_name") or pcfg.get("name", "")

    # ── 第一级：确定性回复（不需要随机，直接决定）──

    # 1a. 消息里明确提到 Bot（支持模糊匹配：名字片段也能命中）
    bot_name_hit = _fuzzy_bot_name_match(message, bot_name)
    if bot_name_hit:
        # 提到Bot名字 → 必回（除非是机器人自己的消息，但那不可能）
        update_bot_mood(group_id, -0.03)
        _last_reply_time[group_id] = now
        influence.track_interaction(group_id, user_qq, user_name, "bot_replied_to")
        return True, f"提到Bot({bot_name_hit})"

    # 1b. 明确求助/提问 → 必回
    is_question, question_type = _is_clear_question(message)
    if is_question:
        update_bot_mood(group_id, -0.03)
        _last_reply_time[group_id] = now
        influence.track_interaction(group_id, user_qq, user_name, "bot_replied_to")
        return True, f"明确求助({question_type})"

    # 1c. 回复了 Bot 的消息 → 必回
    if is_reply_to_bot:
        update_bot_mood(group_id, -0.03)
        _last_reply_time[group_id] = now
        influence.track_interaction(group_id, user_qq, user_name, "bot_replied_to")
        return True, "回复了Bot的消息"

    # ── 第二级：高概率回复 ──
    content_score = _score_message_relevance(message)
    # 内容高度相关 → 90% 概率
    if content_score >= 0.60:
        if random.random() < 0.90:
            update_bot_mood(group_id, -0.04)
            _last_reply_time[group_id] = now
            influence.track_interaction(group_id, user_qq, user_name, "bot_replied_to")
            return True, f"高相关({content_score:.0%})"

    # ── 第三级：概率回复 ──
    if gp.get("auto_chat_enabled"):
        base_prob = gp.get("auto_chat_probability", 0.15)
    else:
        base_prob = 0.15  # 默认 15%（比旧版 12% 高一点，减少"必须@才回"的感觉）

    # 🤖 Bot 情绪因子
    mood = get_bot_mood(group_id)
    energy = mood.get("energy", 0.7)
    if mood["mood"] == "tired":
        base_prob *= 0.4
    elif mood["mood"] == "excited":
        base_prob *= 1.6
    elif mood["mood"] == "happy":
        base_prob *= 1.25

    # 👥 群友影响力因子
    user_influence = influence.get_influence(group_id, user_qq)
    influence_bonus = min(0.4, user_influence / 100)
    base_prob += influence_bonus

    # 📝 消息内容因子
    base_prob += content_score * 0.5

    # 💕 内容情绪因子：消息带强烈情绪（悲伤/开心/震惊的新闻）→ Bot 更可能插嘴
    # 真人看到群里聊大事也会凑过去，不会只在被@时才说话
    try:
        from . import content_emotion as _ce
        emo = _ce.detect_emotion(message)
        if emo:
            _val, _arous, _topic = emo
            base_prob += min(0.3, abs(_val) * 0.25 + _arous * 0.15)
    except Exception:
        pass

    # 📎 引用内容因子
    if quoted_context:
        quoted_text = quoted_context.get("text", "")
        if _contains_bot_related(quoted_text, bot.bot_qq):
            base_prob += 0.2

    # 📷 媒体内容因子
    media = media_reader.parse_media_from_message(message)
    if media:
        base_prob += 0.1 * len(media)

    # 🎯 回复频率控制
    group_freq = group_personality.get_group_reply_frequency(group_id)
    base_prob *= group_freq

    # 最终概率限制
    final_prob = min(0.95, max(0.01, base_prob))

    # 😴 拟人：偶尔已读不回 —— 真人不会每条消息都回
    # 心情平静或累的时候偶尔假装没看到；但心情好或内容很情绪化时不装瞎
    if mood["mood"] in ("neutral", "tired") and random.random() < 0.06:
        # 如果内容情绪强烈（群里在聊大事），即使累也不会装瞎
        try:
            from . import content_emotion as _ce2
            _emo2 = _ce2.detect_emotion(message)
            if not _emo2 or (abs(_emo2[0]) < 0.5 and _emo2[1] < 0.6):
                return False, f"已读不回(拟人) prob={final_prob:.2f}"
        except Exception:
            return False, f"已读不回(拟人) prob={final_prob:.2f}"

    decide = random.random() < final_prob

    reason = (
        f"prob={final_prob:.2f} energy={energy:.1f} "
        f"influence={user_influence:.0f} freq={group_freq:.1f} "
        f"mood={mood['mood']}"
    )

    if decide:
        update_bot_mood(group_id, -0.05)
        _last_reply_time[group_id] = now
        influence.track_interaction(group_id, user_qq, user_name, "bot_replied_to")

    return decide, reason


def _score_message_relevance(message: str) -> float:
    """评估消息对 Bot 的相关性 (0-1)"""
    score = 0.0

    # 包含问号 → 高相关
    if "?" in message or "？" in message:
        score += 0.4
    if any(w in message for w in ["吗", "呢", "吧", "多少", "几", "谁", "哪"]):
        score += 0.2

    # 包含讨论/求助性质（扩展版——覆盖技术问题、日常求助）
    discussion_words = [
        # 直接求助
        "我觉得", "推荐", "有没有", "谁知道", "怎么", "为什么", "什么",
        "哪里", "怎么样", "好不好", "行不行", "帮我", "教我",
        "能不能", "可不", "算一下", "告诉我", "解释",
        # 技术/报错相关
        "报错", "出错", "error", "Error", "bug", "BUG",
        "怎么处理", "如何解决", "什么原因", "怎么回事",
        "咋回事", "怎么办", "咋办", "帮我看", "看一下",
        "求助", "请问", "想问", "请教", "有人知道",
        "谁会", "有人会", "懂不懂", "了解吗",
        # 不确定/困惑
        "搞不懂", "不懂", "不明白", "不理解", "啥意思",
        "什么意思", "这是啥", "那是什么",
    ]
    for w in discussion_words:
        if w in message:
            score += 0.25  # 提高权重
            break

    # 包含情感表达
    emotion_words = ["哈哈", "笑死", "呜呜", "哭了", "好开心", "太棒了", "无语", "离谱",
                    "卧槽", "牛", "绝了", "绷不住", "确实", "好无聊", "无聊",
                    "有没有人", "冒泡", "好安静", "没人吗"]
    for w in emotion_words:
        if w in message:
            score += 0.1
            break

    # 消息长度适中（过长可能是刷屏，过短可能是灌水）
    msg_len = len(message)
    if 10 <= msg_len <= 150:
        score += 0.15
    elif msg_len > 150:
        score -= 0.05

    return min(1.0, max(0.0, score))


def _is_clear_question(message: str) -> tuple[bool, str]:
    """
    检测消息是否是明确的问题/求助。
    返回 (是否为问题, 问题类型)。

    确定性匹配——命中就说明对方确实在问东西、需要帮助，
    不需要随机概率，直接回。
    """
    # 技术/报错类问题（高优先级）
    tech_patterns = [
        (["报错", "出错", "error", "Error", "bug", "BUG",
          "崩溃", "闪退", "不行", "没用", "失败",
          "打不开", "连不上", "登不上", "用不了"], "技术问题"),
        (["怎么处理", "如何解决", "怎么解决", "如何修复",
          "什么原因", "怎么回事", "咋回事", "什么情况"], "故障排查"),
        (["帮我看", "帮我查", "帮我看下", "看一下", "看下",
          "帮我看看", "帮忙看看", "帮个忙"], "请求帮助"),
    ]

    for patterns, qtype in tech_patterns:
        if any(p in message for p in patterns):
            return True, qtype

    # 明确的知识询问
    knowledge_patterns = [
        "是什么意思", "什么意思", "啥意思",
        "什么是", "什么叫", "是谁", "啥是",
        "介绍一下", "科普", "讲讲",
    ]
    if any(p in message for p in knowledge_patterns):
        return True, "知识询问"

    # 显式提问（问号 + 具体内容）
    if ("?" in message or "？" in message) and len(message) >= 8:
        return True, "提问"

    # 直接求助句式
    direct_help = [
        "请问", "想问", "请教", "求助", "有人知道",
        "谁知道", "谁会", "有人会",
    ]
    if any(p in message for p in direct_help) and len(message) >= 6:
        return True, "直接求助"

    return False, ""


def _fuzzy_bot_name_match(message: str, bot_name: str) -> str:
    """
    模糊匹配 Bot 名字 —— 支持昵称/简称。
    返回匹配到的片段（用于日志），未匹配返回空字符串。

    策略：
    1. 精确匹配（bot_name in message）
    2. 去后缀匹配（"六哥AI" → 检查 "六哥" 是否在消息中）
    3. 分词匹配（名字含标点/空格时拆分匹配）
    """
    if not bot_name or not message:
        return ""

    # 1. 精确匹配
    if bot_name in message:
        return bot_name

    # 2. 去常见后缀后匹配（"AI"、"Bot"、"机器人" 等）
    suffixes = ["AI", "ai", "Bot", "bot", "机器人", "酱", "君", "桑", "大人"]
    for suffix in suffixes:
        if bot_name.endswith(suffix):
            core = bot_name[:-len(suffix)]
            if len(core) >= 2 and core in message:
                return core

    # 3. 名字分段匹配（取 >=2 个字符的子串，从最长开始试）
    if len(bot_name) >= 3:
        for sub_len in range(len(bot_name) - 1, 1, -1):
            for start in range(len(bot_name) - sub_len + 1):
                sub = bot_name[start:start + sub_len]
                # 跳过纯数字/纯符号的子串
                if sub.isdigit() or all(c in "_-[]{}()（）" for c in sub):
                    continue
                if sub in message:
                    return sub

    return ""


def _contains_bot_related(text: str, bot_qq: str) -> bool:
    """检查文本是否包含 Bot 相关信息"""
    bot_keywords = ["机器人", "bot", "Bot", "AI", "小Q", bot_qq]
    return any(kw in text for kw in bot_keywords if kw)


# ═══════════════════════════════════════════════════════════
#  增强回复生成
# ═══════════════════════════════════════════════════════════

async def enhance_reply_with_media(
    reply_text: str,
    group_id: int,
    message: str = "",
    emotion: str = "",
) -> str:
    """
    增强 AI 回复：自动添加合适的表情/贴纸。
    """
    gp = group_personality.get_group_personality(group_id)
    emoji_style = gp.get("emoji_style", "mixed")

    if emoji_style == "none":
        return reply_text

    # AI 自主决定发不发表情——不再自动追加
    # （AI 可以在回复中直接写 [CQ:face,id=14] 格式的表情码）
    return reply_text


def _detect_emotion_from_reply(text: str) -> str:
    """从回复文本中检测情绪"""
    emotion_map = {
        "开心": ["哈哈", "嘿嘿", "开心", "太好了", "nice", "👍", "棒"],
        "撒娇": ["嘛", "啦", "哦", "喵", "人家"],
        "惊讶": ["啊", "天哪", "居然", "什么"],
        "无奈": ["唉", "算了", "好吧", "行吧"],
        "害羞": ["嘿嘿", "不好意思", "害羞"],
        "喜欢": ["喜欢", "爱", "❤", "好喜欢"],
        "赞": ["厉害", "牛", "强", "666"],
        "安慰": ["没事", "抱抱", "别难过"],
    }
    for emotion, keywords in emotion_map.items():
        for kw in keywords:
            if kw in text:
                return emotion
    return ""


# ═══════════════════════════════════════════════════════════
#  拟人：表情表态代替文字回复
# ═══════════════════════════════════════════════════════════

# 情绪词 → QQ 表情 ID 映射（用 emoji_like 贴表情）
EMOJI_REACT_MAP = {
    # 笑/开心 → 😂 / 😆
    "笑死": "128", "哈哈": "128", "笑死我了": "128", "哈哈哈": "128",
    "红红火火": "128", "恍恍惚惚": "128", "笑": "128",
    # 牛/厉害 → 👍
    "牛": "76", "牛逼": "76", "666": "76", "厉害": "76",
    "太强": "76", "强": "76", "太猛": "76",
    # 无语/离谱 → 😅
    "无语": "178", "离谱": "178", "……": "178",
    # 哭/惨 → 😭
    "呜呜": "4", "哭了": "4", "好惨": "4",
    # 惊/卧槽 → 😱
    "卧槽": "1", "天哪": "1", "什么鬼": "1",
    # 赞 → 👍
    "赞": "76", "好棒": "76", "太棒": "76",
}


def should_emoji_react(message: str) -> tuple[bool, str, str]:
    """
    判断是否应该用表情表态代替文字回复。

    纯情绪表达的消息，贴个表情比打一段文字更像真人。

    Returns:
        (should_react: bool, emoji_id: str, reason: str)
        emoji_id 是 QQ 表情 ID，用于 set_msg_emoji_like
    """
    if not message or len(message) > 30:
        return False, "", ""

    msg = message.strip()

    for keyword, emoji_id in EMOJI_REACT_MAP.items():
        if keyword in msg:
            # 50% 概率选择表情表态而不是文字回复
            if random.random() < 0.50:
                return True, emoji_id, f"表情表态({keyword}→{emoji_id})"

    return False, "", ""
