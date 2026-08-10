"""
图片审核模块 v2 — MiniMax M3 多模态视觉识别
- 使用 MiniMax M3 的 vision 能力替代本地 OCR
- 自动检测图片中的广告、色情、违规内容
- 返回结构化的审核结果
"""
import asyncio
import base64
import json
import logging
import re
from io import BytesIO
from typing import Optional

from .config import config

logger = logging.getLogger("qqbot")

def _extract_json(text: str) -> dict | None:
    """从 AI 返回的文本中鲁棒提取 JSON（处理截断、markdown、多余文字）"""
    if not text:
        return None

    # 方法1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 方法2: 尝试从文本中提取 {...} 块（最外层）
    start = text.find("{")
    if start == -1:
        return None
    # 从 start 开始找匹配的 }
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    # 方法3: 修复被截断的 JSON — 补全缺失的结尾
    json_text = text[start:]
    # 补缺失的 } 和 "
    open_braces = json_text.count("{") - json_text.count("}")
    open_brackets = json_text.count("[") - json_text.count("]")
    # 检查最后一个 key 的 value 是否未闭合
    # 简单策略：追加缺失的闭合符号
    fixed = json_text.rstrip()
    if fixed.endswith(","):
        fixed = fixed[:-1]
    # 检查字符串是否未闭合
    in_string = False
    escape = False
    for ch in fixed:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        fixed += '"'
    # 补 missing ] and }
    fixed += "]" * open_brackets
    fixed += "}" * open_braces
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # 方法4: 正则兜底 — 提取关键字段
    result = {"is_violation": False, "confidence": 0, "violation_type": "none",
              "ocr_text": "", "reason": "解析失败"}
    for field, pattern in [
        ("is_violation", r'"is_violation"\s*:\s*(true|false)'),
        ("confidence", r'"confidence"\s*:\s*([\d.]+)'),
        ("violation_type", r'"violation_type"\s*:\s*"([^"]*)"'),
        ("ocr_text", r'"ocr_text"\s*:\s*"([^"]*)"'),
        ("reason", r'"reason"\s*:\s*"([^"]*)"'),
    ]:
        m = re.search(pattern, text)
        if m:
            val = m.group(1)
            if field == "is_violation":
                result[field] = val == "true"
            elif field == "confidence":
                try: result[field] = float(val)
                except: pass
            else:
                result[field] = val
    return result

# MiniMax M3 vision 审核系统提示词（静态 → 高缓存命中）
VISION_MODERATION_PROMPT = """你是一个QQ群内容审核助手。请仔细检查这张图片：

1. 先逐字读出图片中所有文字（包括截图、聊天记录、代码报错、网页内容等）
2. 再判断是否包含违规内容：广告引流/色情/赌博诈骗/政治敏感

【重要：非广告内容排除】
以下内容属于正常技术交流，**不是广告**，请判为 normal：
- API 文档截图、API Key 使用教程、开发者工具界面
- 代码编辑器截图、终端/命令行截图、报错信息
- 开源项目页面（GitHub/GitLab/Gitee）、技术博客
- AI 模型/服务的正常讨论（GPT、Claude、DeepSeek 等）
- 编程语言/框架的官方文档截图
- 任何人分享"如何使用某个工具/服务"的教程截图

以下才是真正的广告（is_violation=true）：
- 明显售卖/推广：加群/加微信引流、付费推广链接
- 金钱诱惑：日赚/返利/刷单/兼职招聘
- 带有价格和购买方式的商品推销
- 大量群号/微信号刷屏

请以 JSON 格式回复（只输出 JSON）：
{
  "is_violation": true/false,
  "confidence": 0.0-1.0,
  "violation_type": "advertising/nudity/gambling/political/none",
  "ocr_text": "图片中所有文字内容，逐字读出，不要省略",
  "reason": "简短判断理由（20字以内）"
}"""

# ═══════════════════════════════════════════════════════════
#  MiniMax 多模态审核
# ═══════════════════════════════════════════════════════════

async def _get_mm_client():
    """返回模型名（客户端由 llm_call 共享管理）。无 api_key 返回 None。"""
    from bot import memory
    from . import llm_call
    if llm_call.get_client() is None:
        return None
    ms = memory.load_model_settings()
    return ms.get("model", "glm-5.2-free")

async def review_image_with_ai(
    bot, file_id: str, image_url: str = "", caption: str = ""
) -> Optional[dict]:
    """
    使用 MiniMax M3 vision 审核图片内容。

    Args:
        bot: OneBotClient 实例
        file_id: QQ 图片 file_id
        image_url: QQ 图片 URL（备用）
        caption: 图片附带的文字说明

    Returns:
        {"is_violation": bool, "confidence": float, "violation_type": str,
         "ocr_text": str, "reason": str, "keywords_matched": list}
        或 None（无法分析时）
    """
    model = await _get_mm_client()
    if model is None:
        logger.debug("图片审核跳过: 无 API key")
        return None

    # 获取图片 base64 数据
    img_b64 = None
    media_type = "image/jpeg"

    if file_id and bot and bot._ws:
        try:
            r = await bot._call("get_image", {"file": file_id})
            if r.get("status") == "ok":
                data = r.get("data", {})
                b64 = data.get("data", data.get("file", ""))
                if b64:
                    # 修复 padding
                    padding = 4 - len(b64) % 4
                    if padding != 4:
                        b64 += "=" * padding
                    # 验证是否为有效 base64
                    try:
                        base64.b64decode(b64)
                        img_b64 = b64
                    except Exception:
                        # 可能是 URL 而不是 base64
                        image_url = b64 if b64.startswith("http") else image_url
        except Exception as e:
            logger.warning(f"获取图片数据失败: {e}")

    # 备用: 通过 URL 下载（先修复 HTML 实体: &amp; → &）
    if not img_b64 and image_url:
        import html as _html
        clean_url = _html.unescape(image_url)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        img_b64 = base64.b64encode(raw).decode("utf-8")
                        # 检测 content-type
                        ct = resp.headers.get("Content-Type", "")
                        if "png" in ct:
                            media_type = "image/png"
                        elif "gif" in ct:
                            media_type = "image/gif"
                        elif "webp" in ct:
                            media_type = "image/webp"
        except Exception as e:
            logger.warning(f"图片下载失败: {e}")

    if not img_b64:
        logger.debug("无法获取图片数据用于 AI 审核")
        return None

    # 构建审核请求
    user_content = []
    user_content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": img_b64},
    })
    text_prompt = VISION_MODERATION_PROMPT
    if caption:
        text_prompt += f"\n\n图片附带的文字说明: {caption}"
    user_content.append({"type": "text", "text": text_prompt})

    try:
        from . import llm_call
        response = await llm_call.llm_create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": user_content}],
            temperature=0.1,
        )

        text = response.content[0].text.strip()
        # 清理 markdown 代码块
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

        result = _extract_json(text)
        if result is None:
            logger.warning(f"AI 图片审核返回非 JSON: {text[:100]}")
            return {"is_violation": False, "confidence": 0, "violation_type": "none",
                    "ocr_text": text[:200], "reason": "解析失败"}

        logger.info(
            f"AI 图片审核: violation={result.get('is_violation')} "
            f"type={result.get('violation_type')} "
            f"conf={result.get('confidence', 0):.0%} "
            f"reason={result.get('reason', '')}"
        )
        return result

    except Exception as e:
        logger.error(f"AI 图片审核异常: {e}")
        return None

# ═══════════════════════════════════════════════════════════
#  兼容旧接口
# ═══════════════════════════════════════════════════════════

async def extract_text_from_image(bot, file_id: str, image_url: str = "") -> str:
    """通过 AI 视觉提取图片文字（替代 OCR）"""
    result = await review_image_with_ai(bot, file_id, image_url)
    if result:
        return result.get("ocr_text", "")
    return ""

async def analyze_image_message(bot, event: dict) -> dict | None:
    """
    分析图片消息（AI 多模态审核 + 学习引擎评分）
    返回与旧接口兼容的结果字典
    """
    message: list = event.get("message", [])

    image_segs = [seg for seg in message if seg.get("type") == "image"]
    if not image_segs:
        return None

    file_id = image_segs[0]["data"].get("file", "")
    image_url = image_segs[0]["data"].get("url", "")

    # 附带的文字
    text_parts = []
    for seg in message:
        if seg.get("type") == "text":
            text_parts.append(seg["data"].get("text", ""))
    caption = "".join(text_parts).strip()

    # AI 多模态审核
    ai_result = await review_image_with_ai(bot, file_id, image_url, caption)

    if ai_result is None:
        # AI 不可用时回退到学习引擎
        from . import automod, learner
        score = learner.score_with_learning(caption or "[图片]")
        score["is_image"] = True
        score["ocr_text"] = ""
        score["caption"] = caption[:300]
        score["keywords_matched"] = []
        return score

    # 构建兼容的返回结果
    ocr_text = ai_result.get("ocr_text", "")
    full_text = f"{caption} {ocr_text}".strip()

    # 用学习引擎对识别的文字做关键词匹配
    from . import automod, learner, violation
    analysis = violation.analyze_violation_message(full_text) if full_text else {"keywords": []}

    keywords_matched = analysis.get("keywords", [])
    if ai_result.get("violation_type") not in ("none", None):
        keywords_matched = list(set(keywords_matched + [ai_result.get("violation_type", "")]))

    return {
        "is_violation": ai_result.get("is_violation", False),
        "confidence": ai_result.get("confidence", 0),
        "keywords_matched": keywords_matched,
        "is_image": True,
        "ocr_text": ocr_text[:300],
        "caption": caption[:300],
        "ai_reason": ai_result.get("reason", ""),
        "violation_type": ai_result.get("violation_type", "none"),
    }

def extract_image_info(message: list) -> tuple[str, str]:
    """从消息中提取图片 file_id 和 url"""
    for seg in message:
        if seg.get("type") == "image":
            return seg["data"].get("file", ""), seg["data"].get("url", "")
    return "", ""

# ═══════════════════════════════════════════════════════════
#  本地 OCR 检查（保留兼容，不再使用）
# ═══════════════════════════════════════════════════════════

_ocr_available = None

def check_ocr():
    """检查本地 OCR 是否可用（已弃用，改用 AI 视觉）"""
    global _ocr_available
    if _ocr_available is not None:
        return _ocr_available
    try:
        import pytesseract
        from PIL import Image
        import os
        tesseract_paths = [
            r"E:\ocr\tesseract.exe",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for p in tesseract_paths:
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
                break
        _ocr_available = True
    except ImportError:
        _ocr_available = False
    return _ocr_available


# ═══════════════════════════════════════════════════════════
#  三级降级 + GIF 抽帧 + 并发调度（Task 2 新增）
# ═══════════════════════════════════════════════════════════

import io
import os
import time as _time

# 24h 审核结果缓存: url -> {"result": dict, "ts": float}
_mod_cache: dict[str, dict] = {}
IMAGE_MOD_CACHE_TTL = 86400  # 24 小时

# 并发上限
_AUDIT_SEMAPHORE = asyncio.Semaphore(5)
# 单条消息媒体上限
AUDIT_MEDIA_CAP = 20


def _load_mod_cache() -> dict:
    """从 data/image_mod_cache.json 读缓存"""
    from .config import config
    path = os.path.join(config.DATA_DIR, "image_mod_cache.json")
    if not os.path.exists(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_mod_cache(cache: dict):
    """写缓存到 data/image_mod_cache.json"""
    from .config import config
    path = os.path.join(config.DATA_DIR, "image_mod_cache.json")
    try:
        import json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 清过期
        now = _time.time()
        cleaned = {k: v for k, v in cache.items() if now - v.get("ts", 0) < IMAGE_MOD_CACHE_TTL}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"图片审核缓存写入失败: {e}")


def _init_mod_cache():
    """启动时加载（懒加载）"""
    global _mod_cache
    if not _mod_cache:
        _mod_cache = _load_mod_cache()


async def review_image_with_fallback(bot, file_id: str, image_url: str = "", caption: str = "") -> Optional[dict]:
    """
    图片审核三级降级链:
      1. Vision API (review_image_with_ai)
      2. OCR (Tesseract)
      3. OCR 文字喂 llm_moderate

    Returns:
        {is_violation, confidence, violation_type, ocr_text, reason, keywords_matched}
        或 None（全失败）
    """
    _init_mod_cache()

    # 缓存命中
    if image_url and image_url in _mod_cache:
        entry = _mod_cache[image_url]
        if _time.time() - entry.get("ts", 0) < IMAGE_MOD_CACHE_TTL:
            return entry["result"]

    # ── 第 1 级: Vision ──
    try:
        vision = await review_image_with_ai(bot, file_id, image_url, caption)
        if vision is not None:
            _cache_result(image_url, vision)
            return vision
    except Exception as e:
        logger.info(f"Vision 审核失败，降级 OCR: {str(e)[:80]}")

    # ── 第 2 级: OCR ──
    if not _ocr_available_or_init():
        logger.debug("OCR 不可用，跳过 OCR 降级")
        return None

    img_bytes = await _download_image_bytes(image_url) if image_url else None
    if not img_bytes:
        return None

    # GIF 拆帧（任一帧违规即违规）
    frames = _extract_gif_frames(img_bytes)
    all_ocr_texts = []
    any_violation = None
    for frame_bytes in frames:
        ocr_text = await _ocr_image_bytes(frame_bytes)
        if not ocr_text.strip():
            continue
        all_ocr_texts.append(ocr_text)
        # ── 第 3 级: OCR 文字喂 llm_moderate ──
        try:
            from . import llm_mod
            llm_r = await llm_mod.llm_moderate(ocr_text, force_llm=True)
            if llm_r and llm_r.get("is_violation"):
                any_violation = {
                    "is_violation": True,
                    "confidence": llm_r.get("confidence", 0.7),
                    "violation_type": llm_r.get("category", "ad"),
                    "ocr_text": " | ".join(all_ocr_texts),
                    "reason": f"OCR识别: {llm_r.get('reason', '')[:30]}",
                    "keywords_matched": llm_r.get("keywords_matched", []),
                }
                break  # 任一帧命中即停
        except Exception as e:
            logger.warning(f"OCR 文字喂 LLM 审核失败: {e}")

    if any_violation:
        _cache_result(image_url, any_violation)
        return any_violation

    # 全部 OCR 文字都没有命中违规
    if all_ocr_texts:
        result = {
            "is_violation": False, "confidence": 0.3,
            "violation_type": "none",
            "ocr_text": " | ".join(all_ocr_texts),
            "reason": "OCR 后判定正常",
        }
        _cache_result(image_url, result)
        return result

    return None


def _cache_result(url: str, result: dict):
    if not url:
        return
    _mod_cache[url] = {"result": result, "ts": _time.time()}
    try:
        _save_mod_cache(_mod_cache)
    except Exception:
        pass


def _ocr_available_or_init() -> bool:
    """检测 Tesseract 是否可用（沿用 check_ocr 路径检测）"""
    global _ocr_available
    if _ocr_available is None:
        check_ocr()
    return _ocr_available


async def _download_image_bytes(url: str) -> Optional[bytes]:
    """下载图片原始 bytes（修复 HTML 实体）"""
    if not url:
        return None
    import html as _html
    clean = _html.unescape(url)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(clean, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        logger.info(f"图片下载失败: {e}")
    return None


async def _ocr_image_bytes(raw_bytes: bytes) -> str:
    """用 Tesseract OCR 提取图片文字（chi_sim + eng）"""
    if not _ocr_available_or_init():
        return ""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(raw_bytes))
        # chi_sim 找不到会抛错，try eng 兜底
        try:
            return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except Exception:
            return pytesseract.image_to_string(img, lang="eng")
    except Exception as e:
        logger.info(f"OCR 失败: {e}")
        return ""


def _extract_gif_frames(raw_bytes: bytes) -> list[bytes]:
    """
    GIF → 多帧 PNG bytes。非 GIF 返回 [raw_bytes]。
    ≤3 帧全提；>3 帧取首/中/末 3 帧。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        n = getattr(img, "n_frames", 1)
        if n <= 1:
            return [raw_bytes]
        picks = list(range(n)) if n <= 3 else [0, n // 2, n - 1]
        frames = []
        for idx in picks:
            img.seek(idx)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            frames.append(buf.getvalue())
        return frames
    except Exception as e:
        logger.debug(f"GIF 抽帧失败，按静态图处理: {e}")
        return [raw_bytes]


async def _audit_all_media(bot, images: list[dict], caption: str = "") -> list[dict]:
    """
    并发审核所有图片。Semaphore(5) 控并发，单条上限 20。
    返回每个图片的审核结果（可能含 None 元素，调用方需过滤）。
    """
    if not images:
        return []
    # 截断
    to_audit = images[:AUDIT_MEDIA_CAP]
    if len(images) > AUDIT_MEDIA_CAP:
        logger.info(f"媒体超上限 {AUDIT_MEDIA_CAP}，截断（共 {len(images)} 个）")

    async def _one(img: dict) -> Optional[dict]:
        async with _AUDIT_SEMAPHORE:
            extra_caption = img.get("caption_hint", "")
            full_caption = f"{caption} {extra_caption}".strip()
            try:
                return await review_image_with_fallback(
                    bot, img.get("file_id", ""), img.get("url", ""), caption=full_caption
                )
            except Exception as e:
                logger.warning(f"单图审核失败 ({img.get('url','')[:50]}): {e}")
                return None

    return await asyncio.gather(*[_one(im) for im in to_audit])
