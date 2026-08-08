"""
群友影响力追踪模块
- 跟踪群友与 Bot 的互动频率
- 计算每个群友的影响力分数
- 高影响力用户的消息更容易被 Bot 回复
- 影响 AI 的回复决策
"""
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from . import cache, db
from .config import config

logger = logging.getLogger("qqbot")

INFLUENCE_PATH = os.path.join(config.DATA_DIR, "member_influence.json")


def _load() -> dict:
    """加载影响力数据 {f"{group_id}:{user_qq}": {...}}"""
    if os.path.exists(INFLUENCE_PATH):
        try:
            with open(INFLUENCE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(INFLUENCE_PATH), exist_ok=True)
    with open(INFLUENCE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _key(group_id: int, user_qq: int) -> str:
    return f"{group_id}:{user_qq}"


# ═══════════════════════════════════════════════════════════
#  影响力计算
# ═══════════════════════════════════════════════════════════

# 权重配置
WEIGHTS = {
    "mention_bot": 10,       # @了 Bot
    "reply_to_bot": 8,       # 回复了 Bot 的消息
    "bot_replied_to": 6,     # Bot 回复过此人
    "message_count": 0.1,    # 每条普通消息
    "sticker_shared": 3,     # 分享了表情
    "image_shared": 2,       # 分享了图片
    "helpful_action": 5,     # 帮助性行为（如告诉Bot信息）
    "time_decay_days": 7,    # 影响力衰减天数
}


def track_interaction(
    group_id: int,
    user_qq: int,
    user_name: str = "",
    interaction_type: str = "message_count",
    value: float = 1.0,
):
    """记录一次互动"""
    data = _load()
    key = _key(group_id, user_qq)
    now = time.time()

    entry = data.get(key, {
        "user_qq": user_qq,
        "user_name": user_name,
        "group_id": group_id,
        "total_score": 0,
        "interactions": {},
        "first_seen": now,
    })

    if user_name and not entry.get("user_name"):
        entry["user_name"] = user_name

    entry["last_seen"] = now
    entry["interactions"][interaction_type] = (
        entry.get("interactions", {}).get(interaction_type, 0) + value
    )

    # 重新计算分数（带时间衰减）
    weight = WEIGHTS.get(interaction_type, 0.5)
    entry["total_score"] = _calculate_score(entry["interactions"], now)

    data[key] = entry
    _save(data)

    # 清缓存
    cache.influence_cache.invalidate(f"influence:{group_id}")


def _calculate_score(interactions: dict, now: float) -> float:
    """计算影响力分数（带时间衰减）"""
    score = 0.0
    for itype, count in interactions.items():
        weight = WEIGHTS.get(itype, 0.5)
        score += count * weight
    return round(score, 2)


def get_influence(group_id: int, user_qq: int) -> float:
    """获取某人在某群的影响力分数"""
    data = _load()
    entry = data.get(_key(group_id, user_qq))
    if not entry:
        return 0.0
    return entry.get("total_score", 0.0)


def get_top_influencers(group_id: int, limit: int = 10) -> list[dict]:
    """获取某群影响力最高的人"""
    data = _load()
    prefix = f"{group_id}:"
    results = []
    for key, entry in data.items():
        if key.startswith(prefix):
            results.append(entry)
    results.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return results[:limit]


def get_group_influence_map(group_id: int) -> dict[int, float]:
    """获取某群所有成员的影响力映射 {user_qq: score}"""
    data = _load()
    prefix = f"{group_id}:"
    result = {}
    for key, entry in data.items():
        if key.startswith(prefix):
            result[entry["user_qq"]] = entry.get("total_score", 0.0)
    return result


# ═══════════════════════════════════════════════════════════
#  回复决策辅助
# ═══════════════════════════════════════════════════════════

def should_reply_to_user(
    group_id: int,
    user_qq: int,
    base_probability: float = 0.5,
) -> bool:
    """
    判断是否应该回复此人。
    影响力越高 → 越可能回复。
    """
    import random
    influence = get_influence(group_id, user_qq)

    # 影响力加成
    influence_bonus = min(0.4, influence / 100)  # 最多 +40%
    adjusted_prob = min(0.95, base_probability + influence_bonus)

    should = random.random() < adjusted_prob
    if not should:
        logger.debug(f"跳过回复 {user_qq}: prob={adjusted_prob:.2f}, influence={influence}")

    return should


def summarize_group_influence(group_id: int) -> str:
    """生成群影响力摘要（用于 AI 上下文）"""
    top = get_top_influencers(group_id, limit=8)
    if not top:
        return ""

    lines = ["🌟 **活跃群友（影响力排行）:**"]
    for i, entry in enumerate(top, 1):
        name = entry.get("user_name", "") or f"QQ({entry.get('user_qq', '')})"
        score = entry.get("total_score", 0)
        lines.append(f"  {i}. {name} (影响力: {score:.0f})")
    return "\n".join(lines)
