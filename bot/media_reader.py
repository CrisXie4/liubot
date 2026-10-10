"""
AI 图片/视频阅读理解模块
- 图片 OCR 文字识别（已有 image_mod 基础）
- 图片内容描述（通过 LLM Vision）
- 视频信息提取
- 多媒体消息的上下文理解
"""
import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Optional

from . import db
from .config import config
from .memory import load_model_settings

logger = logging.getLogger("qqbot")

# --- 图片分析缓存 ---
MEDIA_CACHE_PATH = os.path.join(config.DATA_DIR, "media_cache.json")
_media_cache: dict = {}
_cache_loaded = False

def _load_cache():
    global _media_cache, _cache_loaded
    if _cache_loaded:
        return
    if os.path.exists(MEDIA_CACHE_PATH):
        try:
            with open(MEDIA_CACHE_PATH, "r", encoding="utf-8") as f:
                _media_cache = json.load(f)
        except Exception:
            _media_cache = {}
    _cache_loaded = True

def _save_cache():
    os.makedirs(os.path.dirname(MEDIA_CACHE_PATH), exist_ok=True)
    with open(MEDIA_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_media_cache, f, ensure_ascii=False, indent=2)


def get_cached_description(image_url: str) -> Optional[str]:
    """供其他模块（如 image_mod）复用已缓存的图片描述。
    命中则返回描述文本，否则 None。避免对同一张图重复调 vision。
    """
    if not image_url:
        return None
    _load_cache()
    cache_key = f"img:{image_url}"
    cached = _media_cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("description"):
        # 过期清理（24 小时）
        analyzed = cached.get("analyzed_at", "")
        if analyzed:
            try:
                analyzed_dt = datetime.strptime(analyzed, "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - analyzed_dt).total_seconds() > 86400:
                    return None
            except Exception:
                pass
        return cached["description"]
    return None

# ═══════════════════════════════════════════════════════════
#  CQ 码解析
# ═══════════════════════════════════════════════════════════

CQ_RE = re.compile(r"\[CQ:(\w+),([^\]]*?)\]")

def parse_media_from_message(message: str) -> list[dict]:
    """从消息中提取所有媒体 CQ 码"""
    results = []
    for m in CQ_RE.finditer(message):
        cq_type = m.group(1)
        params_str = m.group(2)
        params = {}
        for part in params_str.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
        if cq_type in ("image", "video", "record", "mface", "face"):
            # face → sticker type for collection
            display_type = "sticker" if cq_type in ("mface", "face") else cq_type
            results.append({"type": display_type, "params": params, "raw": m.group(0), "cq_type": cq_type})
    return results

def get_image_url(message: str) -> Optional[str]:
    """从消息中提取图片 URL"""
    media = parse_media_from_message(message)
    for m in media:
        if m["type"] == "image" and "url" in m["params"]:
            return m["params"]["url"]
    return None

def get_video_info(message: str) -> Optional[dict]:
    """从消息中提取视频信息"""
    media = parse_media_from_message(message)
    for m in media:
        if m["type"] == "video":
            return m["params"]
    return None

# ═══════════════════════════════════════════════════════════
#  图片理解（通过 LLM Vision）
# ═══════════════════════════════════════════════════════════

async def describe_image(image_url: str, context: str = "") -> Optional[str]:
    """
    通过 LLM Vision 描述图片内容。
    优先用主模型，不支持 Vision 时自动切换 MiniMax M3。
    """
    ms = load_model_settings()
    api_key = ms.get("api_key", "")
    if not api_key:
        return _basic_image_description(image_url)

    # 检查缓存
    _load_cache()
    cache_key = f"img:{image_url}"
    if cache_key in _media_cache:
        cached = _media_cache[cache_key]
        if isinstance(cached, dict) and cached.get("description"):
            return cached["description"]

    # 下载图片 — 先修复 URL 里的 HTML 实体（&amp; → &）
    import base64 as b64
    import html as _html
    clean_url = _html.unescape(image_url)
    img_data = None
    content_type = "image/jpeg"
    is_gif = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            resp = await http.get(clean_url, headers={"User-Agent": "QQBot/5.0"})
            if resp.status_code == 200:
                is_gif = _is_gif_url(clean_url, resp.headers.get("content-type", ""))
                if is_gif:
                    # GIF：抽帧拼成一张图（vision 按静态图理解，prompt 说明是动图分帧）
                    frames_png = await asyncio.to_thread(
                        _gif_to_frames_image, resp.content)
                    if frames_png:
                        img_data = b64.b64encode(frames_png).decode("utf-8")
                        content_type = "image/png"
                    else:
                        img_data = b64.b64encode(resp.content).decode("utf-8")
                        content_type = resp.headers.get("content-type", "") or "image/gif"
                else:
                    img_data = b64.b64encode(resp.content).decode("utf-8")
                    ct = resp.headers.get("content-type", "")
                    if ct: content_type = ct
            else:
                logger.info(f"图片下载失败 HTTP {resp.status_code}: {image_url[:60]}")
                return _basic_image_description(image_url)
    except Exception as e:
        logger.info(f"图片下载异常: {e}")
        return _basic_image_description(image_url)

    if not img_data:
        return _basic_image_description(image_url)

    if is_gif:
        prompt = (
            "这是同一个GIF动图按时间顺序抽取的几帧画面（从左到右）。"
            "请描述这个动图整体在表现什么：画面主体、动作/变化过程、"
            "想传达的情绪或笑点。50字左右。"
        ) + (f"\n对话上下文: {context}" if context else "")
    else:
        prompt = (
            "请详细描述这张图片的内容。"
            "如果是截图/报错/代码：逐行读出所有文字内容，不要省略。"
            "如果是聊天记录截图：读出每条消息的发送者和内容。"
            "如果是表情包：描述画面、文字和情绪。"
            "如果是照片：描述场景、人物、关键细节。"
            "至少50字，信息越多越好。"
        ) + (f"\n对话上下文: {context}" if context else "")

    # 图片理解：让 llm_create 自动选支持 vision 的 provider（如 Claude）
    # 传一个占位 model，llm_create 会用 provider 自己的 model 覆盖
    from . import llm_call
    try:
        response = await llm_call.llm_create(
            priority="P1",
            model="vision-placeholder",
            max_tokens=400,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": img_data}},
                {"type": "text", "text": prompt},
            ]}],
            temperature=0.3,
        )
        description = response.content[0].text.strip()
        if description:
            _media_cache[cache_key] = {"description": description, "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            _save_cache()
            logger.info(f"图片描述: {description[:60]}")
            return description
    except Exception as e:
        logger.warning(f"图片描述 Vision 失败: {str(e)[:100]}")

    return _basic_image_description(image_url)

def _basic_image_description(url: str) -> Optional[str]:
    """简单图片描述（无 Vision 时）"""
    # 只能根据 URL 特征猜测
    if "emoji" in url.lower() or "sticker" in url.lower() or "face" in url.lower():
        return "[表情图片]"
    if "qlogo" in url.lower() or "avatar" in url.lower():
        return "[头像图片]"
    return "[图片] - 建议开启 Vision 功能以获取内容描述"


def _is_gif_url(url: str, content_type: str = "") -> bool:
    """判断是否 GIF 动图（URL 后缀或响应 content-type）。"""
    ct = (content_type or "").lower()
    if "gif" in ct:
        return True
    return ".gif" in (url or "").lower().split("?")[0]


def _gif_to_frames_image(img_bytes: bytes, max_frames: int = 3) -> Optional[bytes]:
    """GIF 抽帧拼图：取首/中/尾最多 3 帧，横向拼成一张 PNG（vision 模型不直接吃多帧 GIF）。

    返回 PNG 字节；失败返回 None（调用方退回原图直传）。
    """
    try:
        import io as _io
        from PIL import Image, ImageSequence
        im = Image.open(_io.BytesIO(img_bytes))
        frames = []
        for frame in ImageSequence.Iterator(im):
            frames.append(frame.convert("RGB"))
            if len(frames) >= max_frames:
                break
        # 只有 1 帧说明近似静态图，直接原样单帧
        target_w = 480
        tiles = []
        for f in frames:
            ratio = target_w / max(f.width, 1)
            tiles.append(f.resize((target_w, max(int(f.height * ratio), 1))))
        gap = 8
        total_w = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
        total_h = max(t.height for t in tiles)
        canvas = Image.new("RGB", (total_w, total_h), (255, 255, 255))
        x = 0
        for t in tiles:
            canvas.paste(t, (x, 0))
            x += t.width + gap
        out = _io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.debug(f"GIF 抽帧失败: {e}")
        return None

# ═══════════════════════════════════════════════════════════
#  视频理解
# ═══════════════════════════════════════════════════════════

async def analyze_video(video_url: str, video_params: dict, context: str = "") -> Optional[str]:
    """
    尝试理解视频内容。
    由于无法直接分析视频流，通过以下方式：
    1. 提取文件名/大小等元信息
    2. 如果有点赞/分享时的描述文字，使用它
    3. 通过视频缩略图（如果有）分析
    """
    info_parts = []

    # 元信息
    file_name = video_params.get("file", "")
    file_size = video_params.get("file_size", "")
    if file_name:
        info_parts.append(f"文件名: {file_name}")
    if file_size:
        try:
            size_mb = int(file_size) / (1024 * 1024)
            info_parts.append(f"大小: {size_mb:.1f}MB")
        except ValueError:
            info_parts.append(f"大小: {file_size}")

    # 尝试通过缩略图分析
    thumb_url = video_params.get("thumb_url", "") or video_params.get("url", "")
    if thumb_url and thumb_url != video_url:
        try:
            thumb_desc = await describe_image(thumb_url, context=f"这是视频的缩略图。{context}")
            if thumb_desc:
                info_parts.append(f"缩略图: {thumb_desc}")
        except Exception:
            pass

    if not info_parts:
        return "[视频消息]"
    return "📹 视频 | " + " | ".join(info_parts)

# ═══════════════════════════════════════════════════════════
#  消息中的媒体内容提取
# ═══════════════════════════════════════════════════════════

async def extract_media_context(message: str, context: str = "") -> list[dict]:
    """
    从一条消息中提取所有媒体上下文。
    返回: [{"type": "image", "description": "...", "url": "..."}, ...]
    """
    results = []
    media = parse_media_from_message(message)

    for m in media:
        if m["type"] == "image":
            url = m["params"].get("url", "")
            if url:
                desc = await describe_image(url, context)
                results.append({
                    "type": "image",
                    "url": url,
                    "description": desc or "[图片]",
                    "raw_cq": m["raw"],
                })

        elif m["type"] == "video":
            url = m["params"].get("url", "")
            desc = await analyze_video(url, m["params"], context)
            results.append({
                "type": "video",
                "url": url,
                "description": desc or "[视频]",
                "raw_cq": m["raw"],
                "params": m["params"],
            })

        elif m["type"] == "mface":
            summary = m["params"].get("summary", "表情")
            results.append({
                "type": "sticker",
                "description": f"[表情: {summary}]",
                "raw_cq": m["raw"],
            })

    return results

# ═══════════════════════════════════════════════════════════
#  图片引用内容提取（用于引用回复）
# ═══════════════════════════════════════════════════════════

async def get_quoted_media_description(quoted_message: str) -> str:
    """获取被引用消息中媒体内容的描述"""
    media = await extract_media_context(quoted_message)
    if not media:
        return ""
    return " ".join(m["description"] for m in media)
