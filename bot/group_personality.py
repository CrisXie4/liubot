"""
群级 AI 人设管理
- 每个群可以有不同的名字、性格、说话风格
- 群特有的口头禅/癖好
- 表情使用偏好
- 回复频率控制
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from . import cache
from .config import config

logger = logging.getLogger("qqbot")

GP_DB_PATH = os.path.join(config.DATA_DIR, "group_personalities.json")

DEFAULT_GROUP_PERSONALITY = {
    "bot_name": "",                # 空=用全局人设
    "personality": "",             # 活泼/严肃/幽默/温柔/毒舌/中二
    "traits": [],                  # 如 ["爱说awa", "喜欢用颜文字"]
    "reply_style": "",             # conversational / concise / detailed
    "custom_prompt": "",           # 群级自定义系统提示词（覆盖全局）
    "quirks": [],                  # 群特有癖好，如 ["每句结尾加awa", "自称本喵"]
    "emoji_style": "mixed",        # qq_face / sticker / mixed / none
    "reply_frequency": 0.5,        # 0-1，回复积极性（0=很少回，1=几乎每次都回）
    "auto_chat_enabled": False,    # 是否主动搭话
    "auto_chat_probability": 0.05, # 主动搭话概率
    "slang_words": {},             # 群特有词汇替换 {"原词": "替换词"}
    "avoid_topics": [],            # 回避话题
    "preferred_topics": [],        # 偏好话题
    "created_at": "",
    "updated_at": "",
}

def _load_all() -> dict:
    """加载所有群的性格配置"""
    if os.path.exists(GP_DB_PATH):
        try:
            with open(GP_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_all(data: dict):
    os.makedirs(os.path.dirname(GP_DB_PATH), exist_ok=True)
    with open(GP_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 清缓存
    if hasattr(cache, 'group_personality_cache'): cache.group_personality_cache.invalidate()

def _cache_key(group_id: int) -> str:
    return f"gp:{group_id}"

def get_group_personality(group_id: int) -> dict:
    """获取某群的性格配置（带缓存）"""
    key = _cache_key(group_id)

    # 检查缓存
    cached = getattr(cache, 'group_personality_cache', None)
    if cached:
        hit = cached.get(key)
        if hit is not None:
            return hit

    data = _load_all()
    gp = data.get(str(group_id), None)

    if gp is None:
        gp = dict(DEFAULT_GROUP_PERSONALITY)
        gp["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data[str(group_id)] = gp
        _save_all(data)

    # 合并默认值
    result = {**DEFAULT_GROUP_PERSONALITY, **gp}

    # 缓存
    if cached is not None:
        cached.set(key, result)

    return result

def save_group_personality(group_id: int, cfg: dict):
    """保存某群的性格配置"""
    data = _load_all()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = data.get(str(group_id), {})
    merged = {**existing, **cfg, "updated_at": now}
    # 防御性过滤：quirks 永远走净化器（拦 Web UI / 旧数据 / 投毒）
    if "quirks" in merged:
        merged["quirks"] = _sanitize_quirks(merged["quirks"])
    if not merged.get("created_at"):
        merged["created_at"] = now
    # 只保存与默认值有差异的
    to_save = {k: v for k, v in merged.items() if DEFAULT_GROUP_PERSONALITY.get(k) != v}
    to_save["updated_at"] = now
    data[str(group_id)] = to_save
    _save_all(data)

def delete_group_personality(group_id: int):
    """删除某群的个性配置（恢复默认）"""
    data = _load_all()
    data.pop(str(group_id), None)
    _save_all(data)

def list_all_groups() -> list[dict]:
    """列出所有有自定义配置的群"""
    data = _load_all()
    return [{"group_id": int(k), **{**DEFAULT_GROUP_PERSONALITY, **v}} for k, v in data.items()]

# ═══════════════════════════════════════════════════════════
#  系统提示词生成
# ═══════════════════════════════════════════════════════════

def build_group_system_prompt(group_id: int, global_prompt: str,
                               global_cfg: dict) -> str:
    """
    构建群级系统提示词。
    如果群有自定义提示词，直接使用；
    否则在全局提示词基础上追加群特有指令。
    """
    gp = get_group_personality(group_id)

    # 群级自定义提示词优先（平台机制附录始终附加）
    if gp.get("custom_prompt"):
        from . import personality as _p
        return gp["custom_prompt"] + _p.MECHANICS_APPENDIX

    # 如果群的性格配置与全局一致，直接使用全局
    has_custom = any([
        gp.get("bot_name") and gp["bot_name"] != global_cfg.get("name"),
        gp.get("personality") and gp["personality"] != global_cfg.get("personality"),
        gp.get("reply_style") and gp["reply_style"] != global_cfg.get("reply_style"),
        gp.get("quirks"),
        gp.get("slang_words"),
    ])

    if not has_custom:
        return global_prompt

    # 构建群特有指令
    extras = []
    gp_name = gp.get("bot_name") or global_cfg.get("name", "小Q")
    gp_personality = gp.get("personality") or global_cfg.get("personality", "活泼")
    gp_traits = gp.get("traits") or global_cfg.get("traits", [])
    gp_style = gp.get("reply_style") or global_cfg.get("reply_style", "conversational")

    styles = {
        "conversational": "大多数回复一句话以内，几个字也行；想说的多就拆成几条短消息",
        "concise": "回复非常简短，几个字到一句话",
        "detailed": "可以说得具体些，但仍是聊天口吻，拆成几条短消息而不是一大段",
    }
    style_instruction = styles.get(gp_style, styles["conversational"])

    traits_str = "、".join(gp_traits) if gp_traits else "友好"

    extras.append(f"你在本群的名叫「{gp_name}」，性格{gp_personality}、{traits_str}。")
    extras.append(f"说话方式: {style_instruction}。")

    # 癖好
    quirks = gp.get("quirks", [])
    if quirks:
        extras.append("本群专属习惯: " + "；".join(quirks))

    # 表情风格
    emoji_style = gp.get("emoji_style", "mixed")
    if emoji_style == "qq_face":
        extras.append("请多用QQ自带表情（如[CQ:face,id=14]）。")
    elif emoji_style == "sticker":
        extras.append("可以适当发送表情包。")
    elif emoji_style == "none":
        extras.append("不要使用任何表情。")

    # 群特有词汇
    slang = gp.get("slang_words", {})
    if slang:
        slang_hints = "；".join(f'把"{k}"说成"{v}"' for k, v in list(slang.items())[:5])
        extras.append(f"本群说话风格: {slang_hints}")

    # 回避/偏好话题
    avoid = gp.get("avoid_topics", [])
    if avoid:
        extras.append(f"回避话题: {', '.join(avoid)}。如果被问到，转移话题。")
    preferred = gp.get("preferred_topics", [])
    if preferred:
        extras.append(f"偏好话题: {', '.join(preferred)}。可以适当引导到这些话题。")

    # 在全局提示词基础上追加
    return global_prompt + "\n\n---\n【本群专属设定】\n" + "\n".join(extras)

def get_group_reply_frequency(group_id: int) -> float:
    """获取某群的回复频率 0-1"""
    gp = get_group_personality(group_id)
    return gp.get("reply_frequency", 0.5)

def should_use_sticker(group_id: int) -> bool:
    """判断本群是否应该使用表情"""
    gp = get_group_personality(group_id)
    return gp.get("emoji_style", "mixed") != "none"

def get_group_bot_name(group_id: int) -> str:
    """获取 Bot 在本群的名称"""
    from . import personality
    gp = get_group_personality(group_id)
    return gp.get("bot_name") or personality.load().get("name", config.BOT_NAME)

# ═══════════════════════════════════════════════════════════
#  自主习得：AI 观察群聊自动调整人设
# ═══════════════════════════════════════════════════════════

# 习得缓冲区：收集群聊观察数据，定期分析
_learn_buffers: dict[int, list[dict]] = {}

MAX_QUIRKS = 8  # quirks 上限，FIFO 淘汰

# 身份/关系词 — 含这些词的"癖好"一律不学，防止群友通过聊天投毒改写人设
# （群友说"丛雨丸是你妹妹""你二爷驾到"会被旧版 LLM 学成口头禅）
_IDENTITY_WORDS = ("妹", "弟", "爷", "叔", "爸", "哥", "姐", "娘", "婆",
                   "妻", "夫", "儿", "老母", "老婆", "老公", "小号", "替身",
                   "分身", "本体", "真人", "假的", "AI妹妹", "亲戚")


def _is_safe_quirk(q) -> bool:
    """判断一条 quirk 是否安全可学。防投毒过滤。

    安全的 quirk 应该是纯语气词/拟声词（awa/www/草/hhhh），
    而不是完整句子（"丛雨丸是你妹妹"这种是群友在挑逗，不是癖好）。
    """
    if not q or not isinstance(q, str):
        return False
    q = q.strip()
    if len(q) > 8:  # 太长 → 几乎都是句子而非语气词
        return False
    if any(w in q for w in _IDENTITY_WORDS):  # 含身份/关系词 → 拒绝
        return False
    if q.startswith(("你", "他", "她", "您", "我")):  # 人称代词开头 → 投毒特征
        return False
    if any(p in q for p in "，。！？、；：""''"):  # 语气词不会带标点
        return False
    if "@" in q or "[CQ" in q or "http" in q:  # @/CQ码/链接 → 拒绝
        return False
    return True


def _sanitize_quirks(quirks: list, existing: list = None) -> list:
    """过滤投毒 + 去重 + FIFO 上限。existing 为已存在的 quirks（也会被一并清洗）。"""
    existing = existing or []
    merged: list[str] = []
    for q in list(existing) + list(quirks or []):
        if not _is_safe_quirk(q):
            continue
        if q in merged:
            continue
        merged.append(q)
    return merged[:MAX_QUIRKS]
_learn_last_flush: dict[int, float] = {}
LEARN_MIN_SAMPLES = 20    # 至少观察20条消息才分析
LEARN_COOLDOWN = 600      # 每10分钟最多分析一次

def observe_message(group_id: int, user_name: str, message: str):
    """观察一条群消息，收集用于自动习得的数据"""
    import time
    buf = _learn_buffers.setdefault(group_id, [])
    buf.append({
        "user": user_name,
        "msg": message[:200],
        "time": time.time(),
    })
    # 限制缓冲区大小
    if len(buf) > 100:
        _learn_buffers[group_id] = buf[-80:]

def should_analyze_and_learn(group_id: int) -> bool:
    """判断是否该分析群聊并自动调整人设"""
    import time
    buf = _learn_buffers.get(group_id, [])
    if len(buf) < LEARN_MIN_SAMPLES:
        return False
    last = _learn_last_flush.get(group_id, 0)
    if time.time() - last < LEARN_COOLDOWN:
        return False
    return True

async def auto_learn_group_style(group_id: int) -> Optional[dict]:
    """
    分析群聊消息，自动习得群的说话风格。

    ⚠️ 安全说明（2026-08-07 修订）：
    曾经用 LLM 从群聊提取"群口头禅/群氛围"，但这等于开了一个让群友
    远程改写 Bot 人设的后门——群友说"丛雨丸是你妹妹""光屁股追老母鸡"
    "你二爷驾到"就被 LLM 当成"群专属习惯"返回，再回灌进系统提示词，
    导致 Bot 自称丛雨丸、被骗"喝酒和路灯比武"。
    现已废弃 LLM 分支，只保留规则版检测纯语气词（awa/www/草/hhhh）。
    身份（bot_name）永不自动学习，只来自全局 personality.json。

    Returns: 习得结果 dict，可直接合并到群级人设中
    """
    import time

    buf = _learn_buffers.get(group_id, [])
    if len(buf) < LEARN_MIN_SAMPLES:
        return None

    result = _rule_based_learn(group_id, buf)
    if not result or not isinstance(result, dict):
        return None

    # 清理缓冲区
    _learn_buffers[group_id] = buf[-10:]
    _learn_last_flush[group_id] = time.time()

    # 构建可保存的人设更新
    updates = {}
    if result.get("mood"):
        mood_map = {"活泼": "活泼", "严肃": "严肃", "技术": "幽默", "娱乐": "幽默", "温馨": "温柔"}
        updates["personality"] = mood_map.get(result["mood"], result["mood"])
    if result.get("emoji_style"):
        updates["emoji_style"] = result["emoji_style"]

    # quirks: 过滤投毒 + FIFO 上限（注意：不自动设 bot_name，身份只来自全局）
    new_quirks = _sanitize_quirks(
        result.get("quirks", []),
        existing=get_group_personality(group_id).get("quirks", []),
    )
    if new_quirks:
        updates["quirks"] = new_quirks

    # 保存
    if updates:
        save_group_personality(group_id, updates)
        logger.info(f"群{group_id} 自动习得: {updates}")

    return updates

def _rule_based_learn(group_id: int, messages: list[dict]) -> dict:
    """基于规则的群风格分析（无需 LLM）"""
    all_text = " ".join(m["msg"] for m in messages)

    result = {"quirks": []}

    # 检测常见语气词
    quirk_patterns = {
        "awa": r"awa|阿瓦",
        "www": r"w{2,}|www",
        "草": r"\b草\b",
        "草生": r"草生",
        "hhhh": r"h{2,}",
        "乐": r"\b乐\b",
        "草死了": r"草死了|笑死",
        "绷不住了": r"绷不住了|蚌埠住了",
    }
    for quirk, pattern in quirk_patterns.items():
        if re.search(pattern, all_text, re.IGNORECASE) and len(messages) > 0:
            # 如果超过30%的消息包含这个词，认为是群癖好
            count = sum(1 for m in messages if re.search(pattern, m["msg"], re.IGNORECASE))
            if count / len(messages) > 0.3:
                result["quirks"].append(quirk)

    # 检测表情使用
    face_count = all_text.count("[CQ:face")
    mface_count = all_text.count("[CQ:mface")
    if face_count > mface_count * 2:
        result["emoji_style"] = "qq_face"
    elif mface_count > face_count * 2:
        result["emoji_style"] = "sticker"

    # 检测氛围
    excitement_words = ["哈哈", "笑死", "草", "乐", "牛逼", "666", "好耶"]
    serious_words = ["请问", "请教", "技术", "代码", "原理", "分析"]
    excitement_score = sum(all_text.count(w) for w in excitement_words)
    serious_score = sum(all_text.count(w) for w in serious_words)
    if excitement_score > serious_score * 2:
        result["mood"] = "活泼"
    elif serious_score > excitement_score * 2:
        result["mood"] = "技术"

    return result
