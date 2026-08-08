"""
内容情绪探测器 — 让 Bot 对群聊内容产生情绪反应
================================================
问题：旧的情绪引擎只对"别人怎么对 Bot"反应（被夸/骂/@），
对群里聊的悲伤新闻、开心事完全没反应 → 最重的"人机感"。

本模块在每条群消息上跑一个轻量关键词门控，命中后调
ai_brain.on_read_emotional_content(valence, arousal)，
让 Bot 的心情跟着内容变。

设计：
  - 即时层（本模块）：关键词 + 简单规则，免费，每条都跑
  - 周期层（后续可加）：LLM 批量 digest，兜底，抓关键词漏的
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("qqbot")

# ─── 情绪词库（valence: 正=开心, 负=难过/愤怒; arousal: 激烈度）───
# 这些词描述的是"群里在聊的事"，不是"对 Bot 说的"
_EMOTION_WORDS = [
    # 负面高激烈（悲伤/灾难/事故）
    {"words": ("去世", "死亡", "遇难", "遇难者", "罹难", "遇害", "牺牲", "殉职"),
     "valence": -0.85, "arousal": 0.7, "topic": "灾难/丧亡"},
    {"words": ("地震", "洪水", "台风", "海啸", "山火", "泥石流", "空难", "爆炸"),
     "valence": -0.8, "arousal": 0.8, "topic": "自然灾害"},
    {"words": ("车祸", "事故", "坠楼", "溺亡", "猝死", "病逝", "重伤"),
     "valence": -0.75, "arousal": 0.6, "topic": "事故"},
    {"words": ("癌症", "白血病", "绝症", "晚期", "病危", "ICU", "手术失败"),
     "valence": -0.8, "arousal": 0.5, "topic": "重病"},
    {"words": ("分手", "离婚", "失恋", "出轨", "背叛", "被绿"),
     "valence": -0.5, "arousal": 0.5, "topic": "感情挫折"},
    {"words": ("失业", "被裁", "裁员", "破产", "倒闭", "欠薪"),
     "valence": -0.6, "arousal": 0.5, "topic": "经济挫折"},
    {"words": ("委屈", "难过", "伤心", "心痛", "心碎", "崩溃", "绝望", "抑郁"),
     "valence": -0.6, "arousal": 0.4, "topic": "情绪低落"},
    {"words": ("可怜", "心疼", "惋惜", "遗憾", " RIP", "走好", "安息"),
     "valence": -0.5, "arousal": 0.3, "topic": "同情/哀悼"},

    # 负面高激烈（愤怒/不公）
    {"words": ("霸凌", "欺凌", "虐待", "家暴", "性侵", "猥亵"),
     "valence": -0.85, "arousal": 0.85, "topic": "恶性事件"},
    {"words": ("贪腐", "腐败", "诈骗", "骗局", "跑路", "割韭菜"),
     "valence": -0.6, "arousal": 0.6, "topic": "社会不公"},
    {"words": ("不公", "冤枉", "无辜", "欺压", "霸王条款"),
     "valence": -0.55, "arousal": 0.5, "topic": "不公"},

    # 正面高激烈（开心/庆祝）
    {"words": ("夺冠", "冠军", "金牌", "破纪录", "创造历史", "首冠"),
     "valence": 0.85, "arousal": 0.85, "topic": "重大成就"},
    {"words": ("结婚", "求婚成功", "领证", "婚礼", "新婚"),
     "valence": 0.75, "arousal": 0.6, "topic": "婚喜"},
    {"words": ("出生", "生子", "生女", "当爸爸", "当妈妈", "喜得"),
     "valence": 0.8, "arousal": 0.6, "topic": "新生命"},
    {"words": ("升职", "加薪", "融资", "上市", "敲钟", "暴富", "中奖"),
     "valence": 0.7, "arousal": 0.6, "topic": "事业成功"},
    {"words": ("康复", "出院", "痊愈", "好转", "平安"),
     "valence": 0.7, "arousal": 0.4, "topic": "好消息"},

    # 正面中激烈
    {"words": ("恭喜", "祝贺", "祝福", "幸福", "感动", "暖心", "治愈"),
     "valence": 0.55, "arousal": 0.35, "topic": "温暖"},
    {"words": ("好玩", "有趣", "搞笑", "笑死", "哈哈哈", "草", "绷不住", "乐死"),
     "valence": 0.5, "arousal": 0.4, "topic": "开心"},

    # 中性高激烈（震惊/意外，触发好奇心）
    {"words": ("震惊", "惊人", "突发", "重磅", "突破", "首次", "前所未有"),
     "valence": 0.0, "arousal": 0.8, "topic": "重大新闻"},
    {"words": ("意外", "没想到", "居然", "竟然", "离谱", "离大谱"),
     "valence": 0.0, "arousal": 0.6, "topic": "意外"},
]

# 否定词（"不难过"不应触发难过）—— 简单前缀检测
_NEGATION = ("不", "没", "别", "莫", "无", "未", "非")


def detect_emotion(message: str) -> Optional[tuple[float, float, str]]:
    """从消息内容检测情绪。返回 (valence, arousal, topic) 或 None。

    valence: -1(很负面)..+1(很正面)
    arousal: 0(平淡)..1(激烈)
    topic: 触发词归属的话题标签（用于记忆）

    只取命中最强烈的一组（避免一条消息触发多种矛盾情绪）。
    """
    if not message or len(message) < 2:
        return None

    best: Optional[tuple[float, float, str, float]] = None  # (v, a, topic, score)
    for group in _EMOTION_WORDS:
        for w in group["words"]:
            idx = message.find(w)
            if idx < 0:
                continue
            # 简单否定检测：词前面 1-2 字内是否是否定词
            prefix = message[max(0, idx - 2):idx]
            if prefix and prefix[-1:] in _NEGATION:
                continue
            # 激烈度 = 词本身 arousal × (消息长度因子，长消息更可能是认真在说)
            length_factor = min(1.0, len(message) / 40 + 0.5)
            score = abs(group["valence"]) * group["arousal"] * length_factor
            if best is None or score > best[3]:
                best = (group["valence"], group["arousal"] * length_factor,
                        group["topic"], score)

    if best is None:
        return None
    return (best[0], best[1], best[2])


def process_message_for_emotion(message: str):
    """处理一条群消息：检测情绪并触发意识事件。

    供 handler 对每条非 Bot 的群消息调用。无 LLM，纯关键词，可高频调用。
    """
    result = detect_emotion(message)
    if result is None:
        return
    valence, arousal, topic = result
    try:
        from . import ai_brain
        ai_brain.on_read_emotional_content(valence, arousal, topic)
        # 只在比较强烈时记日志，避免刷屏
        if abs(valence) > 0.6 or arousal > 0.7:
            tag = "正面" if valence > 0 else ("负面" if valence < 0 else "震惊")
            logger.info(f"💕 内容情绪反应 [{tag}] {topic}: {message[:30]}...")
    except Exception as e:
        logger.debug(f"内容情绪检测跳过: {e}")
