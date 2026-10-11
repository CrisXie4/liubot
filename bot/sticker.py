"""
AI 表情/贴纸管理系统
- 收藏表情（从群聊中学习）
- 搜索表情库
- AI 自动选用表情回复
- 按群/标签管理
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from . import db, cache
from .config import config

logger = logging.getLogger("qqbot")

# --- 内存缓存 ---
_sticker_cache: dict[int, list[dict]] = {}
_cache_time: dict[int, float] = {}
CACHE_TTL = 300  # 5分钟

# --- 本地持久化路径 ---
STICKER_DB_PATH = os.path.join(config.DATA_DIR, "stickers.json")
STICKER_CACHE_DIR = os.path.join(config.DATA_DIR, "sticker_cache")

# 确保缓存目录存在
os.makedirs(STICKER_CACHE_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════
#  CRUD
# ═══════════════════════════════════════════════════════════

def _load_stickers() -> dict:
    """加载所有表情数据 {sticker_id: {}} """
    if os.path.exists(STICKER_DB_PATH):
        try:
            with open(STICKER_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_stickers(data: dict):
    os.makedirs(os.path.dirname(STICKER_DB_PATH), exist_ok=True)
    with open(STICKER_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 清缓存
    _sticker_cache.clear()
    _cache_time.clear()

def add_sticker(
    sticker_type: str,      # "face" (QQ表情) / "mface" (商城表情) / "image" (自定义图片)
    cq_code: str,           # 完整 CQ 码，如 [CQ:face,id=123] 或 [CQ:mface,summary=xxx,url=xxx]
    tags: list[str] = None,
    group_id: int = 0,
    added_by: int = 0,
    description: str = "",
) -> int:
    """收藏一个表情，返回 ID"""
    data = _load_stickers()
    sid = max((int(k) for k in data.keys()), default=0) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data[str(sid)] = {
        "id": sid,
        "sticker_type": sticker_type,
        "cq_code": cq_code,
        "tags": tags or [],
        "group_id": group_id,
        "added_by": added_by,
        "description": description,
        "use_count": 0,
        "favorite_count": 1,
        "created_at": now,
        "last_used_at": now,
    }
    _save_stickers(data)
    logger.info(f"收藏表情 #{sid}: {sticker_type} tags={tags}")
    return sid

def remove_sticker(sticker_id: int) -> bool:
    """删除表情（含本地缓存文件）"""
    data = _load_stickers()
    sid = str(sticker_id)
    if sid in data:
        # 删本地缓存
        for ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
            cache_file = os.path.join(STICKER_CACHE_DIR, f"{sticker_id}{ext}")
            if os.path.exists(cache_file):
                try: os.remove(cache_file)
                except: pass
        del data[sid]
        _save_stickers(data)
        return True
    return False

async def download_to_cache(sticker_id: int, image_url: str) -> Optional[str]:
    """下载图片到本地缓存，返回本地 file:// 路径"""
    import httpx
    # 入口反转义：入库的 URL 可能带 HTML 转义（&amp; 等），带着转义去下载必失败
    image_url = html.unescape(image_url)
    ext = ".gif"
    if ".png" in image_url.lower(): ext = ".png"
    elif ".jpg" in image_url.lower() or ".jpeg" in image_url.lower(): ext = ".jpg"
    elif ".webp" in image_url.lower(): ext = ".webp"

    cache_path = os.path.join(STICKER_CACHE_DIR, f"{sticker_id}{ext}")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100:
        return f"file:///{cache_path.replace(os.sep, '/')}"

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as http:
            resp = await http.get(image_url, headers={"User-Agent": "QQBot/5.0"})
            if resp.status_code == 200 and len(resp.content) > 100:
                with open(cache_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"表情缓存下载: #{sticker_id} → {cache_path} ({len(resp.content)}B)")
                return f"file:///{cache_path.replace(os.sep, '/')}"
    except Exception as e:
        logger.debug(f"表情下载失败 #{sticker_id}: {e}")
    return None

def get_cached_path(sticker_id: int) -> Optional[str]:
    """获取已缓存的本地文件路径"""
    for ext in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
        cache_file = os.path.join(STICKER_CACHE_DIR, f"{sticker_id}{ext}")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
            return f"file:///{cache_file.replace(os.sep, '/')}"
    return None

def get_sticker(sticker_id: int) -> Optional[dict]:
    data = _load_stickers()
    return data.get(str(sticker_id))

def list_stickers(
    group_id: int = 0,
    tag: str = "",
    sticker_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """列出表情库"""
    data = _load_stickers()
    results = []
    for s in data.values():
        if group_id and s.get("group_id") not in (0, group_id):
            continue
        if tag and tag not in s.get("tags", []):
            continue
        if sticker_type and s.get("sticker_type") != sticker_type:
            continue
        results.append(s)
    # 按使用次数排序
    results.sort(key=lambda x: (x.get("use_count", 0), x.get("favorite_count", 0)), reverse=True)
    return results[offset:offset + limit]

def search_stickers(query: str, limit: int = 20) -> list[dict]:
    """搜索表情（匹配标签、描述）"""
    data = _load_stickers()
    results = []
    q = query.lower()
    for s in data.values():
        tags_lower = " ".join(t.lower() for t in s.get("tags", []))
        desc = s.get("description", "").lower()
        if q in tags_lower or q in desc:
            results.append(s)
    results.sort(key=lambda x: (x.get("use_count", 0), x.get("favorite_count", 0)), reverse=True)
    return results[:limit]

def use_sticker(sticker_id: int):
    """记录表情被使用"""
    data = _load_stickers()
    s = data.get(str(sticker_id))
    if s:
        s["use_count"] = s.get("use_count", 0) + 1
        s["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_stickers(data)

def favorite_sticker(sticker_id: int):
    """收藏计数+1"""
    data = _load_stickers()
    s = data.get(str(sticker_id))
    if s:
        s["favorite_count"] = s.get("favorite_count", 0) + 1
        _save_stickers(data)

def get_group_stickers(group_id: int, limit: int = 30) -> list[dict]:
    """获取某群常用表情"""
    return list_stickers(group_id=group_id, limit=limit)


def recent_stickers_summary(group_id: int = 0, limit: int = 6) -> str:
    """最近收藏的表情一览（给 AI「发同款」定位 sticker_id 用），按收藏先后倒序。

    只列 face/image——mface 的 url 会过期（发送侧也禁用），列出来只会误导。
    空库返回空串。
    """
    rows = [
        s for s in _load_stickers().values()
        if (not group_id or s.get("group_id") in (0, group_id))
        and s.get("sticker_type") != "mface"
    ]
    rows.sort(key=lambda x: int(x.get("id", 0)), reverse=True)
    lines = []
    for s in rows[:limit]:
        stype = s.get("sticker_type", "?")
        kind = {"image": "图/GIF", "face": "黄脸"}.get(stype, stype)
        tags = "/".join((s.get("tags") or [])[:4]) or "无标签"
        desc = (s.get("description") or "").strip()
        line = f"#{s.get('id')} [{kind}] 标签:{tags}"
        if desc:
            line += f" {desc[:24]}"
        lines.append(line)
    return "\n".join(lines)

def get_sticker_stats() -> dict:
    """表情库统计"""
    data = _load_stickers()
    by_type = defaultdict(int)
    for s in data.values():
        by_type[s.get("sticker_type", "unknown")] += 1
    return {
        "total": len(data),
        "by_type": dict(by_type),
        "most_used": sorted(data.values(), key=lambda x: x.get("use_count", 0), reverse=True)[:5],
    }

# ═══════════════════════════════════════════════════════════
#  CQ 码解析与构建
# ═══════════════════════════════════════════════════════════

# 更健壮的 CQ 码正则: 匹配 [CQ:type,key=val,...] 支持值中包含逗号
CQ_RE = re.compile(r"\[CQ:(\w+),([^\]]*?)\]")

# 分段正则: 把文本拆成 [普通文字, CQ码, 普通文字, ...]
_SEGMENT_RE = re.compile(r"(\[CQ:\w+,[^\]]*?\])")

def parse_single_cq(cq_str: str) -> tuple[str, dict]:
    """解析单个 CQ 码字符串，返回 (type, {key: val, ...})"""
    m = CQ_RE.match(cq_str)
    if not m:
        return "", {}
    cq_type = m.group(1)
    params_str = m.group(2)
    params = {}
    # 用更聪明的方式解析 key=value: 按 ,key= 模式分割
    # 先找到所有 key= 的位置
    key_pattern = re.compile(r",?\s*(\w+)\s*=")
    last_end = 0
    last_key = None
    for km in key_pattern.finditer(params_str):
        if last_key:
            # 上一个 key 的 value 是从 last_end 到当前 match 开始
            val = params_str[last_end:km.start()].strip().rstrip(",")
            params[last_key] = val
        last_key = km.group(1)
        last_end = km.end()
    if last_key:
        val = params_str[last_end:].strip().rstrip(",")
        params[last_key] = val
    return cq_type, params

# 从 onebot_client 导入统一的分段转换函数
def text_to_onebot_segments(text: str) -> list[dict]:
    """将含 CQ 码文本转为 OneBot 消息段（委托给 onebot_client）"""
    # 延迟导入避免循环依赖
    from .onebot_client import cq_text_to_segments
    return cq_text_to_segments(text)

def parse_cq_codes(message: str) -> list[dict]:
    """从消息中解析所有 CQ 码（兼容旧接口）"""
    results = []
    for m in CQ_RE.finditer(message):
        cq_type = m.group(1)
        params_str = m.group(2)
        params = {}
        for part in params_str.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        results.append({"type": cq_type, "params": params, "raw": m.group(0)})
    return results

def extract_sticker_codes(message: str) -> list[dict]:
    """从消息中提取表情 CQ 码"""
    codes = parse_cq_codes(message)
    return [c for c in codes if c["type"] in ("face", "mface", "image")]

def build_face_cq(face_id: int) -> str:
    """构建 QQ 表情 CQ 码"""
    return f"[CQ:face,id={face_id}]"

def build_mface_cq(summary: str, url: str = "") -> str:
    """构建商城表情 CQ 码"""
    parts = [f"summary={summary}"]
    if url:
        parts.append(f"url={url}")
    return "[CQ:mface," + ",".join(parts) + "]"

def build_image_cq(file: str, url: str = "") -> str:
    """构建图片 CQ 码"""
    parts = [f"file={file}"]
    if url:
        parts.append(f"url={url}")
    return "[CQ:image," + ",".join(parts) + "]"

# ═══════════════════════════════════════════════════════════
#  智能表情选择
# ═══════════════════════════════════════════════════════════

# 情绪 → QQ表情ID 映射（完整版）
EMOTION_FACE_MAP = {
    "开心": [14, 20, 96, 101, 104, 178, 182, 183],
    "大笑": [14, 20, 28, 96, 104, 178, 182, 183],
    "害羞": [6, 99, 109, 111],
    "生气": [5, 11, 18, 30, 98, 105, 110, 146, 208],
    "哭泣": [4, 5, 9, 19, 96, 107, 173, 210],
    "惊讶": [0, 1, 3, 97, 115, 177],
    "喜欢": [2, 21, 42, 76, 78, 85, 109, 116, 207],
    "撒娇": [109, 111, 112],
    "调皮": [16, 101, 182, 218, 221],
    "安慰": [84, 113, 212],
    "无奈": [24, 27, 97, 105, 206],
    "卖萌": [21, 109, 111, 112, 176],
    "赞": [76, 78, 118, 143],
    "鄙视": [17, 55, 77, 105],
    "尴尬": [25, 27, 50, 97, 111, 206],
    "嘚瑟": [86, 178, 179, 221],
    "害怕": [7, 10, 26, 41, 98, 100, 111, 177],
    "难受": [7, 9, 19, 23, 34, 37, 173],
    "疑问": [32, 33, 115, 197],
    "冷酷": [4, 16, 28, 211, 216, 220],
    "困了": [8, 34, 36, 180, 217],
    "得意": [16, 86, 178, 179],
    "感动": [2, 9, 76, 96, 113],
    "打招呼": [39, 43, 79, 121, 122, 123],
    "吃货": [21, 60, 61, 147, 148, 149, 150, 171, 215],
    "加油": [114, 119, 120, 143, 165],
    "无语": [1, 15, 22, 23, 24, 25, 27, 97, 180, 194, 197],
    "庆祝": [74, 75, 79, 136, 137, 138, 139, 144, 189],
}

def pick_emotion_face(emotion: str, group_id: int = 0) -> str:
    """按情绪语义选表情：收藏库标签匹配 → 内置黄脸兜底。

    旧版 70% 从收藏库【随机】抽——收藏库是群友发的什么表情都收，
    所以"开心"语境抽出发尴尬脸（2026-09-06 线上实测牛头不对马嘴）。
    现在只有语义匹配命中才用收藏库（image 类型含 GIF，命中即发动图），
    匹配不到一律用内置情绪映射，绝不随机。
    """
    import random
    emo = _match_emotion_key(emotion) or emotion

    # 1) 收藏库语义匹配（face/image 皆可；mface 的 url 会过期，排除）
    if group_id:
        candidates = [
            s for s in get_group_stickers(group_id, limit=50)
            if s.get("sticker_type") != "mface"
        ]
        scored = sorted(
            ((_score_sticker_for_emotion(s, emo), s) for s in candidates),
            key=lambda x: x[0], reverse=True,
        )
        if scored and scored[0][0] >= 0.5:
            best = scored[0][1]
            use_sticker(best["id"])
            cq = _build_sticker_cq(best)
            if cq:
                return cq

    # 2) 表情目录含义直配：内置情绪表没覆盖的语义词先查目录
    #    （2026-10-10 实录：吃瓜 兜底成 14 微笑，而目录里明明有 271:吃瓜——
    #    语义跑偏不如直配；开心/尴尬这类情绪表已覆盖的词保持原路径不动）
    from . import face_catalog as _fc
    if emo not in EMOTION_FACE_MAP and emotion not in EMOTION_FACE_MAP:
        for probe in {emo, emotion} - {""}:
            for fid, meaning in _fc.FACE_CATALOG.items():
                if probe == meaning or (len(probe) >= 2 and probe in meaning):
                    return build_face_cq(fid)

    # 3) 内置黄脸（语义正确的兜底）
    ids = EMOTION_FACE_MAP.get(emo, EMOTION_FACE_MAP.get(emotion, [14]))
    return build_face_cq(random.choice(ids))


# ─── 情绪名归一与语义打分 ───

# LLM/群友可能写的中文表情名 → EMOTION_FACE_MAP 键
EMOTION_ALIASES = {
    "尴尬": "尴尬", "尴尬表情": "尴尬", "汗": "无语", "黑线": "无语",
    "饥饿": "吃货", "饿": "吃货", "吃货": "吃货", "吃": "吃货", "干饭": "吃货",
    "笑": "大笑", "大笑": "大笑", "哈哈": "大笑", "乐": "大笑",
    "开心": "开心", "高兴": "开心", "快乐": "开心", "元气": "开心",
    "哭": "哭泣", "哭泣": "哭泣", "委屈": "哭泣", "难过": "难受",
    "伤心": "难受", "难受": "难受", "累": "困了", "困": "困了",
    "困了": "困了", "睡": "困了", "晚安": "困了",
    "生气": "生气", "怒": "生气", "愤怒": "生气",
    "惊讶": "惊讶", "惊": "惊讶", "震惊": "惊讶", "什么": "惊讶",
    "无语": "无语", "无奈": "无奈", "叹气": "无奈",
    "害羞": "害羞", "脸红": "害羞", "喜欢": "喜欢", "爱": "喜欢",
    "心": "喜欢", "比心": "喜欢", "赞": "赞", "点赞": "赞", "强": "赞",
    "好耶": "赞", "厉害": "赞", "666": "赞",
    "调皮": "调皮", "皮": "调皮", "捣蛋": "调皮",
    "疑问": "疑问", "问号": "疑问", "疑惑": "疑问", "问": "疑问",
    "害怕": "害怕", "恐惧": "害怕", "慌": "害怕",
    "庆祝": "庆祝", "鼓掌": "庆祝", "撒花": "庆祝", "生日快乐": "庆祝",
    "加油": "加油", "努力": "加油",
    "撒娇": "撒娇", "卖萌": "卖萌", "可爱": "卖萌",
    "安慰": "安慰", "抱抱": "安慰", "摸头": "安慰",
    "得意": "得意", "嘚瑟": "嘚瑟", "酷": "冷酷", "冷酷": "冷酷",
    "感动": "感动", "鄙视": "鄙视", "踩": "鄙视",
    "打招呼": "打招呼", "你好": "打招呼", "hello": "打招呼", "hi": "打招呼",
    "变态": "鄙视", "坏笑": "调皮", "斜眼": "无语", "翻白眼": "无语",
    "微笑": "开心", "耶": "庆祝", "捂脸": "尴尬", "狗头": "调皮",
}


def _match_emotion_key(name: str) -> str:
    """把 LLM 写的表情名归一到 EMOTION_FACE_MAP 的键，认不出返回空。"""
    n = (name or "").strip().lower()
    if not n:
        return ""
    if n in EMOTION_ALIASES:
        return EMOTION_ALIASES[n]
    # 去掉"表情/脸"后缀再试（如"尴尬表情"）
    n2 = re.sub(r"(表情|脸|包| emoticon)$", "", n).strip()
    return EMOTION_ALIASES.get(n2, "")


def _score_sticker_for_emotion(s: dict, emotion: str) -> float:
    """收藏表情与情绪的语义匹配分：tags/description/summary 与情绪词（含别名）重叠。"""
    emo = _match_emotion_key(emotion) or emotion
    terms = {emo, emotion.strip().lower()}
    terms |= {a for a, k in EMOTION_ALIASES.items() if k == emo}
    hay = " ".join(
        [s.get("description", ""), s.get("summary", ""),
         " ".join(s.get("tags", []) or [])]
    ).lower()
    if not hay.strip():
        return 0.0
    hit = sum(1 for t in terms if t and t in hay)
    # 收藏数加一点权重（同样命中时优先常用的）
    pop = min(0.1, s.get("use_count", 0) * 0.01 + s.get("favorite_count", 0) * 0.005)
    return (hit / max(len(terms), 1)) * 1.5 + pop


# 回复后处理：把 LLM 自创的中文表情标签转成真表情，转不出来的删掉。
# （换模型后 DeepSeek 会写 [尴尬]/[饥饿]/[尴尬表情]/:尴尬: 这类文本标签，
#   原样发出去就是一行"[表情]"，群友看着莫名其妙。）
_FACE_TAG_RE = re.compile(r"\[([一-鿿a-zA-Z0-9]{1,6}?)\]|:([一-鿿]{1,4}?):")


def normalize_face_tags(text: str) -> str:
    """归一化回复里的中文表情标签。

    - `[尴尬]` `[尴尬表情]` `:尴尬:` → 对应的 [CQ:face,id=N]
    - 裸的 `[表情]`（占位符照抄）→ 删除
    - 认不得的名字（如 [图片]）→ 原样保留（可能是正常文字）
    """
    def _sub(m: "re.Match") -> str:
        name = (m.group(1) or m.group(2) or "").strip()
        if not name:
            return m.group(0)
        if name == "表情":
            return ""  # 工具结果里照抄出来的占位符
        emo = _match_emotion_key(name)
        if not emo:
            return m.group(0)  # 不是表情名，别动
        import random
        ids = EMOTION_FACE_MAP.get(emo, [14])
        return build_face_cq(random.choice(ids))

    # 跳过 CQ 码本体（[CQ:...] 内部不会被 _FACE_TAG_RE 命中，因为名字段不含冒号逗号）
    return _FACE_TAG_RE.sub(_sub, text)

def pick_context_sticker(group_id: int, message: str, tags: list[str] = None) -> Optional[str]:
    """选一个合适的收藏表情，优先用本地缓存（永不过期）"""
    stickers = get_group_stickers(group_id, limit=30)
    if not stickers:
        return None

    # 排除 mface
    safe_stickers = [s for s in stickers if s.get("sticker_type") != "mface"]
    if not safe_stickers:
        safe_stickers = stickers

    # 优先匹配标签
    if tags:
        for s in safe_stickers:
            if any(t in s.get("tags", []) for t in tags):
                use_sticker(s["id"])
                return _build_sticker_cq(s)

    # 随机选
    import random
    weights = [s.get("use_count", 0) + 1 for s in safe_stickers]
    choice = random.choices(safe_stickers, weights=weights, k=1)[0]
    use_sticker(choice["id"])
    return _build_sticker_cq(choice)

def _build_sticker_cq(sticker: dict) -> str:
    """构建表情 CQ 码，优先本地缓存，回退远程 URL；坏记录返回空串"""
    sid = sticker["id"]
    stype = sticker.get("sticker_type", "")

    # face 类型直接用原始 CQ 码（QQ内置，永不过期）
    if stype == "face":
        return sticker.get("cq_code", f"[CQ:face,id={sid}]")

    # image/mface → 优先本地缓存文件
    cached = get_cached_path(sid)
    if cached:
        return f"[CQ:image,file={cached}]"

    cq = html.unescape(sticker.get("cq_code", ""))
    mfile = re.search(r"file=([^,\]]+)", cq)
    if mfile:
        fv = mfile.group(1)
        if fv.startswith("http"):
            return cq  # 远程直发形态，原样用
        local = fv[5:] if fv.startswith("file:") else fv
        if local.startswith("/"):
            # 存量坏记录：cq_code 指向本地缓存文件但文件已不存在
            # （2026-10-11 实录 ENOENT sticker_cache/89.gif——当年下载失败那批）
            if os.path.exists(local) and os.path.getsize(local) > 100:
                return f"[CQ:image,file={local}]"
            # 本地文件丢了 → 远程 URL 兜底直发，顺手后台补缓存（下次能发本地文件）
            m = re.search(r"url=(https?://[^,\]]+)", cq)
            if m:
                _schedule_cache_repair(sid, m.group(1))
                return f"[CQ:image,file={m.group(1)}]"
            # 没有任何远程地址：判死返回空串，让调用方如实报"链接失效"，
            # 别把必然 ENOENT 的本地路径发给 NapCat（AI 会误转述成"网络问题"）
            return ""
    # file 不是本地路径（QQ 内部文件名等）→ 原样返回，NapCat 会自己按 url= 拉图
    return cq


def _schedule_cache_repair(sid: int, url: str):
    """运行中事件循环里后台补下载缓存；失败静默，下次发送再试"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _dl():
        try:
            await download_to_cache(sid, url)
        except Exception as e:
            logger.debug(f"表情缓存修复失败 #{sid}: {e}")

    loop.create_task(_dl())

# ═══════════════════════════════════════════════════════════
#  AI 自动收藏
# ═══════════════════════════════════════════════════════════

def maybe_collect_sticker(message: str, group_id: int, user_qq: int) -> Optional[int]:
    """
    从消息中自动学习收藏表情。
    去重：相同 CQ 码只存一份，重复发送增加收藏计数。
    仅收录有意义的表情（有 id 的 QQ 表情、有 summary 的商城表情、图片表情）。
    """
    codes = extract_sticker_codes(message)
    if not codes:
        return None

    data = _load_stickers()
    collected = None

    for c in codes:
        # 构建规范化 CQ 码（只用关键参数做唯一键）
        cq_type = c["type"]
        params = c["params"]

        if cq_type == "face":
            face_id = params.get("id", "")
            if not face_id:
                continue
            unique_key = f"face:{face_id}"
            cq_code = build_face_cq(int(face_id)) if face_id.isdigit() else c["raw"]
            desc = f"QQ表情 {face_id}"
            tags = ["qq表情"]
        elif cq_type == "mface":
            # 商城表情 URL 会过期，且选择逻辑（pick_emotion_face/pick_context_sticker）
            # 始终排除 mface，收进库也基本永不复用——直接跳过，不白占 MAX_STICKERS 名额
            # （对齐 maybe_collect_with_ai「商城表情不收藏」的立场；
            #   存量坏数据交给 _auto_prune_stickers 按 use_count=0 优先淘汰，不做迁移）
            continue
        elif cq_type == "image":
            file_id = params.get("file", "")
            # URL 反转义：raw 里的 &amp; 原样入库会导致下载/发送全部失败
            url = html.unescape(params.get("url", ""))
            if not file_id:
                continue
            unique_key = f"image:{file_id}"
            cq_code = build_image_cq(file_id, url) if file_id else c["raw"]
            desc = "图片表情"
            tags = ["图片"]
        else:
            continue

        # 去重检查
        found = None
        for s in data.values():
            if s.get("unique_key") == unique_key:
                found = s
                break

        if found:
            # 已存在，增加收藏计数
            favorite_sticker(found["id"])
            collected = found["id"]
        else:
            # 新表情
            sid = add_sticker(
                sticker_type=cq_type,
                cq_code=cq_code,
                tags=tags,
                group_id=group_id,
                added_by=user_qq,
                description=desc,
            )
            # 补充 unique_key
            data2 = _load_stickers()
            if str(sid) in data2:
                data2[str(sid)]["unique_key"] = unique_key
                _save_stickers(data2)
            collected = sid

    # 超过上限自动清理
    data3 = _load_stickers()
    if len(data3) > MAX_STICKERS:
        _auto_prune_stickers(data3)

    return collected

def _auto_prune_stickers(data: dict) -> int:
    """自动清理低质量表情，返回清除数量"""
    overflow = len(data) - MAX_STICKERS
    if overflow <= 0:
        return 0

    stickers = list(data.values())
    # 排序: 未使用 > 使用少 > 收藏时间早的排前面（优先删除）
    stickers.sort(key=lambda s: (
        s.get("use_count", 0),
        s.get("favorite_count", 0),
        -int(s.get("id", 0)),  # 老的优先删
    ))

    removed = 0
    for s in stickers[:overflow]:
        remove_sticker(s["id"])
        removed += 1

    logger.info(f"表情自动清理: 删除 {removed} 个（{len(data)} → {len(data) - removed}）")
    return removed

# ═══════════════════════════════════════════════════════════
#  AI 驱动的表情策展（MiniMax M3 Vision）
# ═══════════════════════════════════════════════════════════

MAX_STICKERS = 300  # 表情上限，超过自动清理（用户希望 AI 多收藏喜欢的）

STICKER_EVAL_PROMPT = """你是一个表情包策展助手。请评估这张图片是否值得收藏为表情包。

评分标准（1-10分）：
- 搞笑/有趣程度
- 画面质量（清晰度、构图）
- 实用性（能否在聊天中复用）
- 可爱/有情绪表达力（可爱的、搞笑的、有梗的都算）
- 是否为广告/违规内容（如果是，打0分）

判断 keep 的标准要宽松：只要这张图有一点点意思（搞笑/可爱/有梗/能用），就 keep=true。
只有明显是广告、二维码、纯文字截图、低质量垃圾才 keep=false。
你是个喜欢收藏表情的人，宁可多收也不要错过好表情。

返回 JSON（只输出 JSON）：
{"score": 1-10, "tags": ["标签1", "标签2"], "reason": "简短理由(15字内)", "keep": true/false}"""

async def evaluate_sticker_with_ai(image_url: str, description: str = "") -> Optional[dict]:
    """
    用 MiniMax M3 vision 评估一张图片是否值得收藏。

    Returns:
        {"score": 7, "tags": ["搞笑", "猫"], "keep": True, "reason": "..."}
        或 None（无法评估时）
    """
    from . import memory as _mem
    ms = _mem.load_model_settings()
    if not ms.get("api_key"):
        return {"score": 5, "tags": [], "keep": True, "reason": "无AI"}

    try:
        import httpx, base64 as _b64
        from . import llm_call

        # 下载图片
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(image_url)
            if resp.status_code != 200:
                return None
            img_data = _b64.b64encode(resp.content).decode("utf-8")
            content_type = resp.headers.get("content-type", "image/jpeg")

        prompt = STICKER_EVAL_PROMPT
        if description:
            prompt += f"\n\n补充描述: {description}"

        response = await llm_call.llm_create(
            priority="P3",
            model=ms["model"],
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": img_data}},
                    {"type": "text", "text": prompt},
                ]
            }],
            temperature=0.3,
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rstrip("```")
        import json
        result = json.loads(text)
        logger.info(f"AI 表情评估: score={result.get('score')} keep={result.get('keep')} reason={result.get('reason', '')}")
        return result

    except Exception as e:
        logger.debug(f"AI 表情评估失败: {e}")
        return None

async def maybe_collect_with_ai(
    cq_type: str, cq_code: str, params: dict, group_id: int, user_qq: int,
) -> Optional[int]:
    """
    AI 驱动的表情收藏入口：
    - image/mface 类型 → 调 MiniMax M3 vision 评估
    - face 类型 → 直接收藏
    - 超过上限 → AI 自动淘汰
    """
    # 入口反转义：实参来自 handler 扫描的 c["raw"]（URL 带 &amp; 等转义），
    # 不还原则并行入库路径会持续写入坏 URL
    cq_code = html.unescape(cq_code)
    data = _load_stickers()
    current_count = len(data)

    # face 类型直接收藏
    if cq_type == "face":
        return None  # 由 maybe_collect_sticker 处理
    if cq_type == "mface":
        return None  # 商城表情 URL 会过期，不收藏

    # image → AI 评估（url 同样带 HTML 转义，先还原再评估/下载）
    img_url = html.unescape(params.get("url", ""))
    if not img_url and cq_type == "mface":
        img_url = f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{params.get('file', '')}/0"

    if not img_url:
        return None

    eval_result = await evaluate_sticker_with_ai(img_url, params.get("summary", ""))

    # AI 判断不收藏
    if eval_result and not eval_result.get("keep", True):
        logger.info(f"AI 拒绝收藏: score={eval_result.get('score')} reason={eval_result.get('reason', '')}")
        return None

    # 超过上限 → AI 淘汰
    if current_count >= MAX_STICKERS:
        to_remove = await _ai_pick_worst_sticker(data)
        if to_remove:
            remove_sticker(to_remove)
            logger.info(f"AI 淘汰表情 #{to_remove}（已满{MAX_STICKERS}个）")

    # 收藏
    tags = eval_result.get("tags", []) if eval_result else []
    desc = eval_result.get("reason", params.get("summary", "表情")) if eval_result else params.get("summary", "表情")
    unique_key = f"{cq_type}:{params.get('file', params.get('summary', ''))}"

    sid = add_sticker(
        sticker_type=cq_type,
        cq_code=cq_code,
        tags=tags,
        group_id=group_id,
        added_by=user_qq,
        description=desc[:40],
    )
    data2 = _load_stickers()
    if str(sid) in data2:
        data2[str(sid)]["unique_key"] = unique_key
        _save_stickers(data2)

    logger.info(f"AI 收藏表情 #{sid}: {desc[:30]} tags={tags}")

    # 异步下载到本地缓存（永不过期）
    if img_url and cq_type in ("image", "mface"):
        import asyncio as _asyncio
        try:
            _asyncio.create_task(download_to_cache(sid, img_url))
        except RuntimeError:
            # 不在 async 上下文中，同步尝试
            pass

    return sid

async def _ai_pick_worst_sticker(data: dict) -> Optional[int]:
    """
    用 AI 选出最该被淘汰的表情。

    策略：优先淘汰 use_count 最低、最后一次使用最久远的。
    如果数据不够，用 AI 综合判断。
    """
    # 简单规则：优先淘汰从未被使用过的
    stickers = list(data.values())
    stickers.sort(key=lambda s: (s.get("use_count", 0), s.get("favorite_count", 0)))

    # 如果有从未使用过的，直接淘汰
    unused = [s for s in stickers if s.get("use_count", 0) == 0]
    if unused:
        # 在未使用的中去掉最近收藏的（保留给了机会的）
        unused.sort(key=lambda s: s.get("created_at", ""))
        return unused[0]["id"]

    # 都使用过 → 淘汰使用最少的
    return stickers[0]["id"] if stickers else None

