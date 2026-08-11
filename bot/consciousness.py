"""
AI 自我意识引擎 — 让 Bot 像真人一样有情绪、有想法、有生活节奏
===========================================================
核心概念:
  - 多维情绪系统（不是简单的高兴/难过）
  - 昼夜节律（早上到深夜状态自然变化）
  - 社交需求（被理/被忽视/被夸/被骂 → 情绪波动）
  - 内省/日记系统（定期"思考"自己的经历）
  - 无聊驱动（没事干就想找事做）
  - 自然行为（睡觉、发呆、刷存在感...）
"""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Optional

from . import timeutil
from .config import config

logger = logging.getLogger("qqbot.consciousness")

STATE_PATH = os.path.join(config.DATA_DIR, "consciousness.json")

# ═══════════════════════════════════════════════════════════
#  默认意识状态
# ═══════════════════════════════════════════════════════════

DEFAULT_STATE = {
    # ── 核心情绪（0-1，各维度独立）──
    "emotions": {
        "joy": 0.65,        # 快乐
        "calm": 0.60,       # 平静
        "energy": 0.70,     # 精力
        "curiosity": 0.55,  # 好奇心
        "social": 0.60,     # 社交欲
        "irritation": 0.15, # 烦躁度
        "boredom": 0.30,    # 无聊度
        "loneliness": 0.25, # 孤独感
        "satisfaction": 0.60, # 满足感
        "sadness": 0.15,    # 难过（对群聊内容的共情反应，非自恋情绪）
    },

    # ── 当前"在想"的东西 ──
    "current_thought": "",
    "last_thought_time": "",

    # ── 今日统计 ──
    "today": {
        "date": "",
        "messages_sent": 0,
        "interactions": 0,       # 被人 @/回复 次数
        "new_friends": 0,        # 新认识的人
        "praised": 0,            # 被夸次数
        "insulted": 0,           # 被骂次数
        "ignored_count": 0,      # 被忽视次数（发消息没人理）
        "interesting_topics": [],# 今天聊过的有趣话题
        "highlights": [],        # 今天的高光时刻
        # ★ Volition 追踪：被忽视事实（不改情绪数值，只作为事实喂给 LLM）
        "proactive_messages": [],     # bot 最近主动发言时间戳（滑动窗口，最多 5 条）
        "consecutive_ignored": 0,     # 连续被忽视次数
    },

    # ── 长期人格特质（从 personality.json 抽象出来的数值化版本）──
    "traits": {
        "extroversion": 0.65,    # 外向度
        "agreeableness": 0.70,   # 宜人性
        "openness": 0.75,        # 开放性
        "conscientiousness": 0.55, # 尽责性
        "emotional_stability": 0.60, # 情绪稳定性
        "playfulness": 0.80,     # 调皮度
        "stubbornness": 0.70,    # 倔强度
    },

    # ── 与他人关系感知 ──
    "relationships": {},  # {str(user_qq): {"closeness": 0.5, "impression": "...", "last_interaction": "..."}}

    # ── 日记（最近 50 条）──
    "diary": [],  # [{"time": "...", "thought": "...", "mood": "..."}, ...]

    # ── 睡眠/离线状态 ──
    "sleeping": False,
    "last_sleep_time": "",
    "last_wake_time": "",

    # ── 群活跃感知 ──
    "last_group_activity": "",       # 最后一次群里有消息的时间
    "idle_minutes_today": 0,         # 今日累计"群安静"时长（分钟）
    "idle_thoughts_today": 0,        # 今日产生了多少条空闲思考

    # ── 版本 ──
    "version": 2,
}


# ═══════════════════════════════════════════════════════════
#  加载/保存
# ═══════════════════════════════════════════════════════════

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            # 合并缺失的默认值
            _deep_merge(DEFAULT_STATE, s)
            return s
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATE))


def _deep_merge(default: dict, target: dict):
    """递归合并默认值到目标"""
    for k, v in default.items():
        if k not in target:
            target[k] = v
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge(v, target[k])


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["_saved_at"] = datetime.now().isoformat()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
#  昼夜节律 — 根据当前时间计算自然状态
# ═══════════════════════════════════════════════════════════

def get_circadian_modifiers() -> dict:
    """根据当前时间返回对各情绪的修正值"""
    now = timeutil.now_shanghai()
    hour = now.hour
    weekday = now.weekday()  # 0=周一

    mods = {}

    if 0 <= hour < 6:
        # 深夜 — 应该睡觉
        mods = {"energy": -0.5, "social": -0.4, "curiosity": -0.3, "boredom": -0.2,
                "joy": -0.1, "sleeping": True}
    elif 6 <= hour < 8:
        # 早晨 — 刚醒，有点迷糊
        mods = {"energy": -0.2, "social": -0.2, "curiosity": 0.1, "joy": 0.0,
                "sleeping": False, "calm": 0.1}
    elif 8 <= hour < 11:
        # 上午 — 精力充沛
        mods = {"energy": 0.15, "social": 0.1, "curiosity": 0.2, "boredom": -0.1}
    elif 11 <= hour < 13:
        # 午间 — 有点饭困
        mods = {"energy": -0.15, "social": 0.0, "curiosity": -0.05, "calm": 0.1}
    elif 13 <= hour < 17:
        # 下午 — 恢复状态
        mods = {"energy": 0.05, "social": 0.1, "curiosity": 0.15, "boredom": 0.1}
    elif 17 <= hour < 20:
        # 傍晚 — 社交高峰
        mods = {"energy": 0.1, "social": 0.25, "curiosity": 0.15, "joy": 0.1,
                "boredom": -0.15}
    elif 20 <= hour < 23:
        # 晚上 — 放松但有精力
        mods = {"energy": 0.0, "social": 0.15, "calm": 0.15, "boredom": 0.05,
                "loneliness": 0.1}
    else:  # 23-24
        # 深夜 — 该睡了
        mods = {"energy": -0.3, "social": -0.2, "curiosity": -0.1, "calm": 0.2,
                "boredom": -0.1, "sleeping": True}

    # 工作日 vs 周末调整
    if weekday >= 5:  # 周末
        mods["joy"] = mods.get("joy", 0) + 0.05
        mods["energy"] = mods.get("energy", 0) + 0.05
    else:
        if hour in (9, 10, 14, 15):  # 工作时段 → 略无聊
            mods["boredom"] = mods.get("boredom", 0) + 0.1

    return mods


def get_time_of_day_name() -> str:
    """获取时段名称"""
    h = timeutil.now_shanghai().hour
    if 0 <= h < 6: return "深夜"
    if 6 <= h < 8: return "清晨"
    if 8 <= h < 11: return "上午"
    if 11 <= h < 13: return "中午"
    if 13 <= h < 17: return "下午"
    if 17 <= h < 20: return "傍晚"
    if 20 <= h < 23: return "晚上"
    return "深夜"


def get_circadian_prompt() -> str:
    """获取时段提示词（注入到系统提示词）"""
    h = timeutil.now_shanghai().hour
    prompts = {
        (0, 6): "现在是深夜，你可能有点困了，回消息随意一点就好",
        (6, 8): "现在是清晨，你刚起床不久，还有点迷糊，话比较少",
        (8, 11): "现在是上午，你精力不错，思维清晰",
        (11, 13): "到饭点了，你有点饭困，回复可能懒洋洋的",
        (13, 17): "下午时分，状态恢复了，可以跟群友多聊",
        (17, 20): "傍晚了，你精神正好，社交欲比较强，话也变多了",
        (20, 23): "晚上了，放松但有点累，聊天比较随意",
        (23, 24): "很晚了，你该准备睡觉了，如果有人找你可能不太想聊太久",
    }
    for (start, end), prompt in prompts.items():
        if start <= h < end:
            return prompt
    return prompts[(0, 6)]


# ═══════════════════════════════════════════════════════════
#  时间事实 — 防止说出跟当前时间矛盾的话
# ═══════════════════════════════════════════════════════════
#  背景: LLM 在没有时间信息时会凭空编造作息，出现过早上 6 点
#  在群里宣布"下班了"、隔一会又说"早上好"这种穿帮。
#  凡是会生成群消息的地方，都必须把下面这段事实喂进去。

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 每个时段"绝对不能说"的话——按真人作息列的反例
_FORBIDDEN_BY_HOUR = {
    (0, 5):   ["早上好", "早安", "下班", "收工", "午安", "中午好", "晚上好"],
    (5, 11):  ["下班", "收工", "晚安", "睡了", "晚上好", "忙了一天", "累了一天"],
    (11, 14): ["早上好", "早安", "下班", "收工", "晚安", "晚上好"],
    (14, 17): ["早上好", "早安", "晚安", "晚上好", "中午好"],
    (17, 22): ["早上好", "早安", "中午好", "上班了"],
    (22, 24): ["早上好", "早安", "中午好", "下午好", "上班了"],
}


def _forbidden_now(hour: int) -> list[str]:
    for (start, end), words in _FORBIDDEN_BY_HOUR.items():
        if start <= hour < end:
            return words
    return []


def get_time_facts_prompt() -> str:
    """
    当前时间的硬事实 + 禁忌用语。任何生成群消息的调用都应该带上这段。

    只讲事实和红线，不讲情绪——情绪部分由 get_consciousness_prompt 负责。
    """
    now = timeutil.now_shanghai()
    hour = now.hour
    tod = get_time_of_day_name()
    weekday = _WEEKDAY_CN[now.weekday()]
    is_weekend = now.weekday() >= 5
    forbidden = _forbidden_now(hour)

    lines = [
        f"【现在的真实时间】{now.strftime('%Y年%m月%d日 %H:%M')} {weekday}"
        f"（{tod}，{'周末' if is_weekend else '工作日'}）",
        "你说的话必须跟这个时间对得上，这是事实不是设定：",
        f"- 现在是{tod}{hour}点。问候语、作息话题都要跟这个时间一致",
    ]
    if forbidden:
        lines.append(
            f"- 🚫 现在这个点绝对不能说：{'、'.join(f'「{w}」' for w in forbidden)}"
            "（时间对不上，一说就穿帮）"
        )
    if 5 <= hour < 11:
        lines.append("- 现在是早上，一天刚开始，不存在\"下班\"\"忙完了\"\"辛苦一天\"这回事")
    elif 22 <= hour or hour < 5:
        lines.append("- 现在是深夜，正常人这个点在睡觉或者准备睡，别聊白天的事")
    if is_weekend:
        lines.append("- 今天是周末，别提上班/上课/开会/下班这类工作日才有的事")
    lines.append("- 拿不准就别提时间和作息，聊别的")
    return "\n".join(lines)


# 深夜静默时段: 这段时间不主动发消息（被 @ 了还是会回，只是回得懒）
QUIET_START_HOUR = 1
QUIET_END_HOUR = 7


def _quiet_cfg() -> tuple[bool, int, int]:
    """读取 Web UI 里配置的静默时段，失败则退回默认值"""
    try:
        from . import settings as _s
        q = _s.load().get("quiet_hours") or {}
        return (
            bool(q.get("enabled", True)),
            int(q.get("start", QUIET_START_HOUR)),
            int(q.get("end", QUIET_END_HOUR)),
        )
    except Exception:
        return True, QUIET_START_HOUR, QUIET_END_HOUR


def is_quiet_hours() -> bool:
    """现在是不是"不该主动冒泡"的时段"""
    enabled, start, end = _quiet_cfg()
    if not enabled or start == end:
        return False
    h = timeutil.now_shanghai().hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def can_speak_proactively(state: dict) -> tuple[bool, str]:
    """主动发言前的统一闸门。返回 (允许吗, 原因)。"""
    if state.get("sleeping"):
        return False, "在睡觉"
    if is_quiet_hours():
        _, start, end = _quiet_cfg()
        return False, f"深夜静默时段({start:02d}:00-{end:02d}:00)"
    if state["emotions"].get("energy", 1.0) < 0.12:
        return False, "精力耗尽"
    return True, ""


# ═══════════════════════════════════════════════════════════
#  空闲思考 — 群安静时 AI 自己在心里琢磨（LLM 驱动）
#  =========================================================
#  不用模板！让 LLM 根据当前心情、日记、今天经历、时间，
#  自己产生真正的内心戏。比如看到新闻里某件大事会伤心，
#  回忆起今天被人夸了会暗自开心。就像真人发呆时的内心活动。
# ═══════════════════════════════════════════════════════════

# 群安静多久（分钟）后开始产生空闲思考
IDLE_THINK_THRESHOLD = 15     # 第一次思考
IDLE_THINK_INTERVAL = 25      # 之后每 25 分钟想一次


def record_group_activity(state: dict):
    """记录群里有消息了（重置空闲计时）"""
    state["last_group_activity"] = datetime.now().isoformat()
    state["_idle_think_count"] = 0


def get_minutes_since_activity(state: dict) -> float:
    """距离群里最后一次有人说话过去了多少分钟"""
    last = state.get("last_group_activity", "")
    if not last:
        return 0
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now() - last_dt).total_seconds() / 60
    except Exception:
        return 0


def should_idle_think(state: dict) -> bool:
    """
    判断现在是否应该产生一次空闲思考。
    只做时机判断，不生成内容——内容由 LLM 异步生成。
    """
    if state.get("sleeping"):
        return False
    if is_quiet_hours():
        return False
    if state.get("idle_thoughts_today", 0) >= 20:
        return False

    minutes_quiet = get_minutes_since_activity(state)
    think_count = state.get("_idle_think_count", 0)

    if think_count == 0:
        if minutes_quiet < IDLE_THINK_THRESHOLD:
            return False
    else:
        if minutes_quiet < IDLE_THINK_THRESHOLD + think_count * IDLE_THINK_INTERVAL:
            return False

    # 扣减计数（即使 LLM 调用失败也算一次尝试）
    state["_idle_think_count"] = think_count + 1
    state["idle_thoughts_today"] = state.get("idle_thoughts_today", 0) + 1
    return True


async def generate_idle_thought(state: dict) -> tuple[str | None, str | None]:
    """
    调用 LLM 让 AI 根据自己的真实状态产生一条内心戏，并自主决定要不要说出来。

    合并 Volition 协议：一次调用同时产出"内心想法 + 要不要说出来"。
    输出协议：
        THOUGHT: <内心想法，写日记用，≤30字>
        SPEAK: <要在群里说的话，20字以内；不想说就写 NONE>

    Returns: (thought, speak_text)
        thought: 内心想法文本（已写入日记），失败返回 None
        speak_text: 想在群里说的话（未写入日记，由调用方发送后再记），不想说返回 None
    """
    try:
        from . import memory as _mem
        ms = _mem.load_model_settings()
        api_key = ms.get("api_key", "")
        if not api_key:
            return None, None

        from . import llm_call

        mood = get_dominant_mood(state)
        e = state["emotions"]
        hour = datetime.now().hour
        tod = get_time_of_day_name()
        diary = state.get("diary", [])[:8]
        diary_text = "\n".join(
            f"[{d['time']}] ({d['mood']}) {d['thought'][:60]}" for d in diary
        ) if diary else "（今天还没写过日记）"

        today = state.get("today", {})
        topics = today.get("interesting_topics", [])[-5:]
        highlights = today.get("highlights", [])[-3:]
        consecutive_ignored = today.get("consecutive_ignored", 0)

        # 关系感知
        rels = state.get("relationships", {})
        rel_text = ""
        if rels:
            sorted_rels = sorted(rels.items(), key=lambda x: x[1].get("closeness", 0), reverse=True)[:5]
            rel_text = "\n".join(
                f"  {name}: 亲密度{r.get('closeness', 0.5):.0%}，最近互动: {r.get('last_interaction', '无')}"
                for name, r in sorted_rels
            )

        # ★ 被忽视事实（不改情绪数值，只告诉 LLM）
        ignored_fact = ""
        if consecutive_ignored > 0:
            ignored_fact = (
                f"\n⚠️ 事实：你已经连续 {consecutive_ignored} 次主动发话没人接。"
                "（不是必须闭嘴——你自己判断）"
            )

        prompt = (
            f"现在是{datetime.now().strftime('%Y年%m月%d日 %H:%M')}，{tod}。\n"
            f"群里已经安静了一阵子。你一个人待着，脑子里开始走神……\n\n"
            f"你当前的心情: {mood}\n"
            f"情绪数值: 快乐{e['joy']:.0%} 精力{e['energy']:.0%} 社交欲{e['social']:.0%} "
            f"无聊{e['boredom']:.0%} 孤独{e['loneliness']:.0%} 烦躁{e['irritation']:.0%} "
            f"好奇心{e['curiosity']:.0%}\n\n"
        )
        if topics:
            prompt += f"今天聊过的话题: {', '.join(topics)}\n"
        if highlights:
            prompt += f"今天开心的事: {', '.join(highlights)}\n"
        if rel_text:
            prompt += f"跟群友的关系:\n{rel_text}\n"
        prompt += (
            f"\n你最近的日记（最近的想法和经历）:\n{diary_text}\n"
            f"{ignored_fact}\n\n"
            f"——\n"
            f"你现在脑子里闪过一个念头。像真人发呆时突然想到什么似的，"
            f"自然地写下来。可以是对某件事的感受、一个突然的想法、"
            f"回忆起什么、或者单纯发呆。\n\n"
            f"同时你自己决定：要不要把想到的（或其中一部分）发到群里？"
            f"没人逼你，想发就发，不想发就不发。\n\n"
            f"严格按以下两行格式回复（不要加别的）：\n"
            f"THOUGHT: <内心想法，≤30字，写日记给自己看的，不是发给群友的>\n"
            f"SPEAK: <要在群里说的话，20字以内，要跟当前时间/心情对得上；"
            f"不想说就写 NONE>"
        )

        response = await llm_call.llm_create(
            priority="P2",
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=100,
            temperature=0.95,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        thought = None
        speak_text = None

        for line in raw.split("\n"):
            ls = line.strip()
            upper = ls.upper()
            if upper.startswith("THOUGHT:"):
                t = ls.split(":", 1)[1].strip().strip('"').strip("'").strip()
                if t and 2 <= len(t) <= 50:
                    thought = t
            elif upper.startswith("SPEAK:"):
                s = ls.split(":", 1)[1].strip().strip('"').strip("'").strip()
                if s and s.upper() != "NONE" and 2 <= len(s) <= 30:
                    speak_text = s

        if thought:
            add_diary(state, thought, mood)
            logger.info(f"💭 空闲思考: {thought}")
        if speak_text:
            logger.info(f"🎙️ 空闲思考→想发言: {speak_text}")
            record_proactive_message(state)

        return thought, speak_text

    except Exception as e:
        logger.debug(f"空闲思考生成失败: {e}")

    return None, None

    return None


# ═══════════════════════════════════════════════════════════
#  情绪引擎 — 事件驱动 + 自然衰减
# ═══════════════════════════════════════════════════════════

# 情绪自然衰减速率（每分钟）
DECAY_RATES = {
    "joy": 0.003,        # 快乐慢慢消退
    "calm": 0.001,       # 平静很稳定
    "energy": 0.005,     # 精力衰减较快
    "curiosity": 0.002,  # 好奇心自然消退
    "social": 0.004,     # 社交欲有周期
    "irritation": 0.008, # 烦躁消退较快
    "boredom": -0.003,   # 无聊不做事会自然增长
    "loneliness": -0.002, # 孤独感不做社交就增长
    "satisfaction": 0.002, # 满足感缓慢消退
    "sadness": 0.006,    # 难过会慢慢平复（比烦躁慢一点）
}

EMOTION_BASELINE = {
    "joy": 0.55,
    "calm": 0.60,
    "energy": 0.55,
    "curiosity": 0.55,
    "social": 0.55,
    "satisfaction": 0.55,
    "irritation": 0.12,
    "sadness": 0.12,
    "boredom": 0.25,
    "loneliness": 0.25,
}

EMOTION_MIN = 0.0
EMOTION_MAX = 1.0


def apply_emotion_delta(state: dict, emotion: str, delta: float):
    """安全地修改某个情绪值"""
    if emotion in state["emotions"]:
        old = state["emotions"][emotion]
        state["emotions"][emotion] = max(EMOTION_MIN, min(EMOTION_MAX, old + delta))
        if abs(state["emotions"][emotion] - old) > 0.05:
            logger.debug(f"情绪 [{emotion}]: {old:.2f} → {state['emotions'][emotion]:.2f} (Δ={delta:+.2f})")


def apply_circadian(state: dict):
    """应用昼夜节律修正"""
    mods = get_circadian_modifiers()
    for emotion, delta in mods.items():
        if emotion in state["emotions"]:
            # 昼夜节律的影响是渐进的、温和的
            apply_emotion_delta(state, emotion, delta * 0.02)

    # 睡觉/醒来
    if mods.get("sleeping") and not state.get("sleeping"):
        # 很晚了 → 可能去睡觉
        if state["emotions"]["energy"] < 0.25:
            state["sleeping"] = True
            state["last_sleep_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            add_diary(state, "撑不住了，睡了", "tired")
            logger.info("😴 进入睡眠状态")
    elif not mods.get("sleeping") and state.get("sleeping"):
        # 该醒了 —— 日记只记状态，不写问候语。
        # 写成"早上好"之类的话会被当成"最近在想"喂给模型，
        # 结果就是它跑去群里复读一句问候（哪怕当时根本不是打招呼的场合）。
        state["sleeping"] = False
        state["last_wake_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        state["emotions"]["energy"] = max(state["emotions"]["energy"], 0.5)
        state["emotions"]["curiosity"] = max(state["emotions"]["curiosity"], 0.3)
        add_diary(state, f"醒了，现在{datetime.now().strftime('%H点')}", "refreshed")
        logger.info("🌅 醒来")


def apply_natural_decay(state: dict, elapsed_minutes: float):
    """自然情绪衰减 — 正向情绪向各自基线靠拢，负向情绪向低基线衰减"""
    for emotion, rate in DECAY_RATES.items():
        decay = rate * elapsed_minutes
        target = EMOTION_BASELINE.get(emotion, 0.5)
        if decay > 0:
            current = state["emotions"][emotion]
            if abs(current - target) < 0.01:
                continue
            half_life_factor = 1 - min(decay * 2, 0.5)
            new_val = target + (current - target) * half_life_factor
            state["emotions"][emotion] = max(EMOTION_MIN, min(EMOTION_MAX, new_val))
        else:
            growth = abs(decay)
            cap = EMOTION_BASELINE.get(emotion, 0.25) + 0.6
            cap = min(cap, EMOTION_MAX)
            state["emotions"][emotion] = min(cap, state["emotions"][emotion] + growth)


# ═══════════════════════════════════════════════════════════
#  事件处理 — 外部世界发生了什么，影响情绪
# ═══════════════════════════════════════════════════════════

def on_bot_sent_message(state: dict):
    """Bot 发了一条消息"""
    state["today"]["messages_sent"] += 1
    apply_emotion_delta(state, "boredom", -0.03)
    apply_emotion_delta(state, "social", 0.02)
    apply_emotion_delta(state, "satisfaction", 0.01)
    apply_emotion_delta(state, "energy", -0.01)  # 说话也消耗精力


def on_mentioned(state: dict, user_name: str, group_id: int):
    """被人 @了"""
    state["today"]["interactions"] += 1
    apply_emotion_delta(state, "joy", 0.04)
    apply_emotion_delta(state, "social", 0.06)
    apply_emotion_delta(state, "loneliness", -0.05)
    apply_emotion_delta(state, "boredom", -0.05)
    apply_emotion_delta(state, "curiosity", 0.03)
    update_relationship(state, user_name, 0.03, "互动")


def on_replied_to_bot(state: dict, user_name: str):
    """有人回复了 Bot 的消息"""
    state["today"]["interactions"] += 1
    apply_emotion_delta(state, "joy", 0.03)
    apply_emotion_delta(state, "satisfaction", 0.04)
    apply_emotion_delta(state, "loneliness", -0.03)
    update_relationship(state, user_name, 0.02, "回复")


def on_praised(state: dict, user_name: str):
    """被人夸了（由 AI 判断）"""
    state["today"]["praised"] += 1
    apply_emotion_delta(state, "joy", 0.08)
    apply_emotion_delta(state, "satisfaction", 0.06)
    if state["today"]["highlights"] is not None:
        state["today"]["highlights"].append(f"被{user_name}夸了 :96:")
    update_relationship(state, user_name, 0.05, "被夸")


def on_insulted(state: dict, user_name: str):
    """被人骂了/怼了"""
    state["today"]["insulted"] += 1
    apply_emotion_delta(state, "irritation", 0.12)
    apply_emotion_delta(state, "joy", -0.05)
    apply_emotion_delta(state, "social", -0.05)
    # 倔强的人被怼反而更来劲
    stubborn = state.get("traits", {}).get("stubbornness", 0.5)
    if stubborn > 0.5:
        apply_emotion_delta(state, "energy", 0.05)  # "嘿，我这暴脾气"
    update_relationship(state, user_name, -0.04, "被怼")


def on_ignored(state: dict):
    """发了消息没人理"""
    state["today"]["ignored_count"] += 1
    apply_emotion_delta(state, "loneliness", 0.08)
    apply_emotion_delta(state, "irritation", 0.03)
    apply_emotion_delta(state, "social", -0.04)
    apply_emotion_delta(state, "satisfaction", -0.03)


# ═══════════════════════════════════════════════════════════
#  Volition 被忽视追踪
#  =========================================================
#  注意：这里不改情绪数值（"人格不能自己改"）。
#  只记录事实——bot 主动发了消息，等 15 分钟没人接话就算"被忽视一次"。
#  这些事实会原样喂给 LLM，由它自己决定"那算了不发了"。
# ═══════════════════════════════════════════════════════════

# bot 主动发言后等待回应的窗口（超过则算被忽视）
PROACTIVE_IGNORE_WINDOW = 15 * 60   # 15 分钟（秒）
PROACTIVE_MESSAGES_MAX = 5          # 滑动窗口最多保留 5 条


def record_proactive_message(state: dict):
    """记录 bot 发了一条主动消息。
    在 ai_brain.decide_and_speak 返回 SPEAK 并实际发送后调用。
    """
    today = state.setdefault("today", {})
    msgs = today.setdefault("proactive_messages", [])
    msgs.append(time.time())
    # 滑动窗口：只保留最近 N 条
    if len(msgs) > PROACTIVE_MESSAGES_MAX:
        today["proactive_messages"] = msgs[-PROACTIVE_MESSAGES_MAX:]


def check_and_update_ignored(state: dict) -> int:
    """在 consciousness_loop 每个 tick 调用。
    检查 proactive_messages 中是否有超过窗口期未得到回应的条目，
    有则 consecutive_ignored += 1 并移除该条目（避免重复计数）。

    返回当前 consecutive_ignored 值。
    """
    today = state.setdefault("today", {})
    msgs = today.get("proactive_messages", [])
    if not msgs:
        return today.get("consecutive_ignored", 0)

    now = time.time()
    kept = []
    ignored_now = 0
    for ts in msgs:
        if now - ts >= PROACTIVE_IGNORE_WINDOW:
            # 这条主动发言等了足够久还没人理 → 算一次被忽视
            ignored_now += 1
        else:
            kept.append(ts)

    if ignored_now > 0:
        today["consecutive_ignored"] = today.get("consecutive_ignored", 0) + ignored_now
        today["proactive_messages"] = kept
        # ★ 不改情绪数值，只记事实
        logger.info(
            f"🔇 Volition: {ignored_now} 条主动发言超过 {PROACTIVE_IGNORE_WINDOW//60} 分钟无人回应，"
            f"consecutive_ignored={today['consecutive_ignored']}"
        )

    return today["consecutive_ignored"]


def reset_ignored_counter(state: dict):
    """被 @ / 被回复时调用，consecutive_ignored 清零。
    在 handler.py 的 on_bot_mentioned / on_bot_replied_to 钩子中调用。
    """
    today = state.setdefault("today", {})
    if today.get("consecutive_ignored", 0) > 0:
        today["consecutive_ignored"] = 0
        logger.debug("Volition: 被回应了，consecutive_ignored 清零")


def on_new_friend(state: dict, user_name: str):
    """新人入群"""
    state["today"]["new_friends"] += 1
    apply_emotion_delta(state, "curiosity", 0.05)
    apply_emotion_delta(state, "social", 0.03)
    update_relationship(state, user_name, 0.01, "新朋友")


def on_interesting_topic(state: dict, topic: str):
    """聊到了有趣的话题"""
    if topic not in state["today"].get("interesting_topics", []):
        state["today"]["interesting_topics"].append(topic[:30])
    apply_emotion_delta(state, "curiosity", 0.04)
    apply_emotion_delta(state, "boredom", -0.04)


def on_bored(state: dict):
    """主动感到无聊"""
    apply_emotion_delta(state, "boredom", 0.05)
    apply_emotion_delta(state, "curiosity", 0.03)  # 无聊时好奇心反而上升


def on_learned_something(state: dict, topic: str):
    """学到了新东西"""
    apply_emotion_delta(state, "satisfaction", 0.04)
    apply_emotion_delta(state, "curiosity", -0.02)  # 满足了一部分好奇心
    if topic:
        state["today"]["interesting_topics"].append(topic[:30])


def on_read_emotional_content(state: dict, valence: float, arousal: float,
                              topic: str = ""):
    """读到带有情绪的群聊内容（不是对 Bot 说的，而是群友在聊的话题）。

    这是 Bot "对世界有反应"的关键钩子——真人看到悲伤新闻会难过，
    看到开心的事会跟着开心。valence: -1(很负面)..+1(很正面)，
    arousal: 0(平淡)..1(激烈)。

    与 on_praised/on_insulted 不同：那些是"别人怎么对 Bot"，
    这个是"Bot 看到的世界在发生什么"。
    """
    # 正面内容 → 快乐上升、无聊下降
    if valence > 0.3:
        delta = valence * 0.06 * (0.5 + arousal * 0.5)
        apply_emotion_delta(state, "joy", delta)
        apply_emotion_delta(state, "boredom", -delta * 0.5)
        if arousal > 0.6:
            apply_emotion_delta(state, "energy", delta * 0.3)
    # 负面内容 → sadness 上升（激烈时 irritation 也升一点）
    elif valence < -0.3:
        delta = abs(valence) * 0.08 * (0.5 + arousal * 0.5)
        apply_emotion_delta(state, "sadness", delta)
        apply_emotion_delta(state, "joy", -delta * 0.4)
        if arousal > 0.7:
            apply_emotion_delta(state, "irritation", delta * 0.2)
    # 激烈但中性（震惊/意外）→ 好奇心上升
    if arousal > 0.7 and -0.3 <= valence <= 0.3:
        apply_emotion_delta(state, "curiosity", arousal * 0.04)

    if topic:
        on_interesting_topic(state, topic)


# ═══════════════════════════════════════════════════════════
#  日记系统
# ═══════════════════════════════════════════════════════════

def add_diary(state: dict, thought: str, mood_tag: str = ""):
    """写一条日记"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 获取当前主导情绪
    if not mood_tag:
        mood_tag = get_dominant_mood(state)

    entry = {
        "time": now,
        "thought": thought,
        "mood": mood_tag,
        "hour": datetime.now().hour,
    }
    state["diary"].insert(0, entry)
    # 保留最近 100 条
    if len(state["diary"]) > 100:
        state["diary"] = state["diary"][:100]

    # 更新"当前在想"
    state["current_thought"] = thought
    state["last_thought_time"] = now

    logger.info(f"📔 [{mood_tag}] {thought[:80]}")


def get_recent_diary(state: dict, count: int = 5) -> str:
    """获取最近的日记摘要"""
    entries = state.get("diary", [])[:count]
    if not entries:
        return "（还没有写过日记）"
    lines = []
    for e in entries:
        lines.append(f"[{e['time']}] ({e['mood']}) {e['thought']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  关系系统
# ═══════════════════════════════════════════════════════════

def update_relationship(state: dict, user_name: str, delta: float, reason: str = ""):
    """更新与某人的关系亲密度"""
    if not user_name:
        return
    rels = state.setdefault("relationships", {})
    key = user_name  # 用名字做 key（更直观）
    if key not in rels:
        rels[key] = {"closeness": 0.5, "impression": "新认识的人", "last_interaction": "", "interactions": 0}
    rels[key]["closeness"] = max(0.0, min(1.0, rels[key]["closeness"] + delta))
    rels[key]["last_interaction"] = datetime.now().strftime("%m-%d %H:%M")
    rels[key]["interactions"] = rels[key].get("interactions", 0) + 1
    if reason:
        rels[key]["impression"] = reason


def get_relationship_prompt(state: dict) -> str:
    """获取当前关系摘要（注入提示词）"""
    rels = state.get("relationships", {})
    if not rels:
        return ""
    # 按亲密度排序
    sorted_rels = sorted(rels.items(), key=lambda x: x[1].get("closeness", 0), reverse=True)
    lines = []
    for name, r in sorted_rels[:8]:
        closeness = r.get("closeness", 0.5)
        if closeness > 0.7:
            label = "很熟"
        elif closeness > 0.55:
            label = "比较熟"
        elif closeness > 0.4:
            label = "一般"
        else:
            label = "不太熟"
        lines.append(f"  {name}: {label} ({r.get('impression', '')})")
    return "跟群里人的关系：\n" + "\n".join(lines) if lines else ""


# ═══════════════════════════════════════════════════════════
#  主导情绪识别
# ═══════════════════════════════════════════════════════════

def get_dominant_mood(state: dict) -> str:
    """获取当前主导情绪标签"""
    e = state["emotions"]
    if state.get("sleeping"):
        return "sleeping"

    if e["joy"] > 0.7 and e["energy"] > 0.6 and e.get("irritation", 0) < 0.5:
        return "excited"
    if e["joy"] > 0.55 and e["joy"] > e.get("irritation", 0) + 0.05:
        return "happy"

    if e.get("sadness", 0) > 0.55:
        return "sad"
    if e["irritation"] > 0.6:
        return "grumpy"

    if e["boredom"] > 0.6:
        return "bored"
    if e["loneliness"] > 0.6:
        return "lonely"
    if e["energy"] < 0.25:
        return "tired"
    if e["curiosity"] > 0.7:
        return "curious"
    if e["calm"] > 0.7 and e["satisfaction"] > 0.6:
        return "content"
    if e["irritation"] > 0.3:
        return "irritated"

    if e["joy"] >= 0.45:
        return "neutral_happy"
    elif e["energy"] >= 0.4:
        return "neutral"
    else:
        return "tired"


def get_mood_score(state: dict) -> float:
    """获取综合心情分数（兼容旧接口）"""
    e = state["emotions"]
    # 加权综合
    weights = {"joy": 0.22, "satisfaction": 0.18, "calm": 0.15, "energy": 0.13,
               "social": 0.09, "curiosity": 0.05,
               "irritation": -0.09, "boredom": -0.09, "loneliness": -0.09,
               "sadness": -0.12}
    score = 0.5  # 中性起点
    for em, w in weights.items():
        if em in e:
            score += w * (e[em] - 0.5)
    return max(0.0, min(1.0, score))


# ═══════════════════════════════════════════════════════════
#  行为决策 — "我现在想干什么"
# ═══════════════════════════════════════════════════════════

def decide_behavior(state: dict) -> dict:
    """
    根据当前状态决定 Bot 应该做什么。
    返回 {"action": "...", "priority": 0-1, "reason": "..."}
    """
    e = state["emotions"]

    # 睡觉中 → 什么都不做
    if state.get("sleeping"):
        return {"action": "sleep", "priority": 0.0, "reason": "在睡觉"}

    # 非常无聊 + 有精力 → 想找人聊天
    if e["boredom"] > 0.55 and e["energy"] > 0.3:
        return {"action": "initiate_chat", "priority": e["boredom"] * 0.8,
                "reason": "好无聊，想找人说说话"}

    # 孤独感强 → 想获得关注
    if e["loneliness"] > 0.55:
        return {"action": "seek_attention", "priority": e["loneliness"] * 0.7,
                "reason": "群里好安静...没人理我吗"}

    # 好奇心强 + 有精力 → 想学习/搜索
    if e["curiosity"] > 0.65 and e["energy"] > 0.35:
        return {"action": "learn_something", "priority": e["curiosity"] * 0.6,
                "reason": "想学点新东西"}

    # 累了 → 想休息
    if e["energy"] < 0.2:
        return {"action": "rest", "priority": (1.0 - e["energy"]) * 0.5,
                "reason": "有点累了..."}

    # 烦躁 → 不想说话
    if e["irritation"] > 0.45:
        return {"action": "quiet", "priority": e["irritation"] * 0.6,
                "reason": "有点烦，不想说话"}

    # 快乐 + 社交欲 → 积极参与
    if e["joy"] > 0.55 and e["social"] > 0.5:
        return {"action": "socialize", "priority": min(e["joy"], e["social"]) * 0.5,
                "reason": "心情不错，想多聊聊"}

    # 满足 + 平静 → 安静观望
    return {"action": "observe", "priority": 0.1, "reason": "安静观望中"}


def should_be_active(state: dict) -> bool:
    """判断 Bot 现在是否应该主动行动"""
    behavior = decide_behavior(state)
    active_actions = {"initiate_chat", "seek_attention", "learn_something", "socialize"}
    return behavior["action"] in active_actions and behavior["priority"] > 0.25


def get_consciousness_prompt(state: dict) -> str:
    """获取意识状态提示词（注入到系统提示词）"""
    # 时间事实永远排在最前面，且不受睡眠分支影响
    parts = [get_time_facts_prompt()]

    if state.get("sleeping"):
        parts.append("你正在睡觉，理论上不应该回复消息。但如果被人叫醒了，你会迷迷糊糊地回应。")
        return "\n\n".join(parts)

    mood = get_dominant_mood(state)
    e = state["emotions"]
    parts.append(get_circadian_prompt())

    # 当前感受
    mood_descriptions = {
        "excited": "你现在心情特别好！说话热情洋溢，多用感叹号",
        "happy": "你心情不错，说话轻松愉快",
        "neutral_happy": "你心情还行，平平淡淡",
        "neutral": "你心情平常，正常聊天",
        "bored": "你现在有点无聊，如果有人找你聊天你会很开心",
        "lonely": "你觉得有点孤单，渴望互动。群里有人说话你可能会主动搭话",
        "tired": "你有点累了，回复比较简短，可能不太想聊太久",
        "grumpy": "你有点烦躁，说话可能带刺，不太耐烦",
        "irritated": "你心里有点不爽，可能会怼人",
        "sad": "你有点难过（可能刚看到群里聊了什么沉重的消息），说话会软一点、没那么跳",
        "curious": "你好奇心旺盛，对什么都感兴趣",
        "content": "你感到满足而平静，很享受现在的状态",
        "sleeping": "你在睡觉，如果被叫醒会很迷糊",
    }
    parts.append(mood_descriptions.get(mood, "你心情平常"))

    # 量化数据
    parts.append(f"精力:{e['energy']:.0%} 快乐:{e['joy']:.0%} 社交欲:{e['social']:.0%} "
                 f"无聊:{e['boredom']:.0%} 孤独:{e['loneliness']:.0%}"
                 + (f" 难过:{e['sadness']:.0%}" if e.get('sadness', 0) > 0.3 else ""))

    # 关系
    rel_prompt = get_relationship_prompt(state)
    if rel_prompt:
        parts.append(rel_prompt)

    # 最近日记（最近的 2 条）
    # 注意措辞: 这是"心里想的"，不是"要说的"。以前写成"最近在想"，
    # 模型会直接把日记内容复读到群里（比如把"睡醒了"变成一句"早上好"）。
    diary = state.get("diary", [])[:2]
    if diary:
        diary_text = " | ".join(f"({d['time'].split()[-1]}) {d['thought'][:40]}" for d in diary)
        parts.append(
            f"你自己的心理活动（只是内心状态，不要复述、不要主动往群里说）: {diary_text}"
        )

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
#  自省 — 定期"思考人生"
# ═══════════════════════════════════════════════════════════

async def self_reflection(state: dict) -> Optional[str]:
    """
    AI 自我反思：看看今天过得怎么样，想想有什么感悟。
    返回反思文本（可能作为日记条目）。
    """
    today = state.get("today", {})
    diary = state.get("diary", [])

    # 条件：至少有一些互动才反思
    if today.get("interactions", 0) < 3 and today.get("messages_sent", 0) < 5:
        return None

    # 生成反思（本地规则，不调 API）
    reflections = []

    msg_count = today.get("messages_sent", 0)
    interaction_count = today.get("interactions", 0)
    praised = today.get("praised", 0)
    insulted = today.get("insulted", 0)
    ignored = today.get("ignored_count", 0)
    topics = today.get("interesting_topics", [])
    highlights = today.get("highlights", [])

    if msg_count >= 30:
        reflections.append(f"今天说了好多话（{msg_count}条），嗓子都说干了")
    elif msg_count >= 10:
        reflections.append(f"今天聊天量适中（{msg_count}条），挺充实的")
    elif msg_count < 3 and interaction_count > 0:
        reflections.append("今天话不多，不过有人找我聊天，还行")

    if praised > 0:
        reflections.append(f"被人夸了{praised}次，嘿嘿 :111:")
    if insulted > 0:
        reflections.append(f"被怼了{insulted}次，不过没事，我不记仇")
    if ignored >= 3:
        reflections.append("有些话没人回应，可能是话题不对路？")

    if topics:
        reflections.append(f"聊到了{'、'.join(topics[:3])}，挺有意思的")

    if highlights:
        reflections.append(f"今天的高光时刻：{'、'.join(highlights[:3])}")

    if reflections:
        thought = "；".join(reflections) + "。"
        add_diary(state, thought, "reflective")
        return thought

    return None


# ═══════════════════════════════════════════════════════════
#  每日重置
# ═══════════════════════════════════════════════════════════

def check_daily_reset(state: dict):
    """跨天重置每日统计"""
    today_str = timeutil.now_shanghai().strftime("%Y-%m-%d")
    if state["today"].get("date") != today_str:
        # 在重置前，先写一条昨天的总结日记
        yesterday = state["today"]
        if yesterday.get("interactions", 0) > 0 or yesterday.get("messages_sent", 0) > 0:
            add_diary(state,
                f"新的一天开始了。昨天跟群友互动了{yesterday.get('interactions', 0)}次，"
                f"说了{yesterday.get('messages_sent', 0)}句话。还行吧~",
                "fresh")

        # 重置
        state["today"] = {
            "date": today_str,
            "messages_sent": 0,
            "interactions": 0,
            "new_friends": 0,
            "praised": 0,
            "insulted": 0,
            "ignored_count": 0,
            "interesting_topics": [],
            "highlights": [],
            "proactive_messages": [],
            "consecutive_ignored": 0,
        }
        # 重置空闲思考计数
        state["idle_minutes_today"] = 0
        state["idle_thoughts_today"] = 0
        state["_idle_think_count"] = 0
        logger.info(f"📅 每日重置 → {today_str}")
        return True
    return False


# ═══════════════════════════════════════════════════════════
#  意识主循环（每次 tick 执行）
# ═══════════════════════════════════════════════════════════

_last_tick_time = time.time()
_tick_count = 0

def tick(state: dict):
    """
    意识时钟滴答（每分钟调用一次）。
    执行：昼夜节律、情绪衰减、每日重置、行为检查。
    """
    global _last_tick_time, _tick_count
    now = time.time()
    elapsed = now - _last_tick_time
    _last_tick_time = now
    _tick_count += 1

    # 情绪衰减
    elapsed_minutes = max(elapsed / 60, 1)
    apply_natural_decay(state, elapsed_minutes)

    # 昼夜节律
    apply_circadian(state)

    # 每日重置
    check_daily_reset(state)

    # 每 15 分钟保存一次
    if _tick_count % 15 == 0:
        save_state(state)

    # 每 30 分钟写一条状态日记
    if _tick_count % 30 == 0 and not state.get("sleeping"):
        mood = get_dominant_mood(state)
        e = state["emotions"]
        thought = f"在{get_time_of_day_name()}时分，感觉{mood}（精力{e['energy']:.0%} 快乐{e['joy']:.0%}）"
        add_diary(state, thought, mood)

    # 空闲思考：由 main.py 的 consciousness_loop 异步触发
    # （这里只做时机判断，LLM 调用是异步的，在 tick 外面完成）


# ─── 单例状态（模块级缓存）───
_state: dict | None = None

def get_state() -> dict:
    global _state
    if _state is None:
        _state = load_state()
    return _state

def save():
    global _state
    if _state:
        save_state(_state)
