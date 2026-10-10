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
- 个人支付/转账凭证截图：微信/支付宝/银行的付款页、扫码支付页、转账成功页、
  账单详情、红包记录——群友分享**自己的**支付凭证（特征：截图形式、含浏览器
  界面/金额/订单号/支付确认 UI）是正常聊天内容。即使支付页面周围有广告横幅
  或可疑域名，也不是发图者在打广告

以下才是真正的广告（is_violation=true）：
- 明显售卖/推广：加群/加微信引流、付费推广链接
- 金钱诱惑：日赚/返利/刷单/兼职招聘
- 带有价格和购买方式的商品推销
- 大量群号/微信号刷屏
- 收款二维码本身作为广告发布，并配引流话术（加微信、私聊、扫码进群、
  套现代办等），目的是让群友扫码付钱——注意与上面"分享自己的支付凭证截图"
  区分：凭证是已完成的交易记录，广告是招揽别人付款

请以 JSON 格式回复（只输出 JSON）：
{
  "is_violation": true/false,
  "confidence": 0.0-1.0,
  "violation_type": "advertising/nudity/gambling/political/none",
  "ocr_text": "图片中所有文字内容，逐字读出，不要省略",
  "reason": "简短判断理由（20字以内）"
}"""

# ── 个人支付凭证防误判 ──
# 线上案例(2026-08-17): 群友分享自己的微信支付截图被判 advertising(82%) → 撤回+禁言1h；
# 同类截图另几次又判 normal，说明模型在这类图上随机摇摆。
# 防护：AI 判 advertising，但依据文本呈现"支付凭证特征"且无引流话术 → 降级为 normal。
_PAYMENT_RECEIPT_EVIDENCE = re.compile(
    r"扫码支付|支付页面|付款页面|收款页面|支付成功|付款成功|转账成功|转账记录|"
    r"交易单号|商户单号|订单号|账单|红包|零钱明细|支付凭证|支付报错|支付页"
)
_AD_SOLICIT_WORDS = re.compile(
    r"加微信|加群|加[vv]|私聊|联系我|联系方式|日赚|佣金|兼职|刷单|返利|套现|"
    r"招代理|代理加盟|低价出售|名额有限|扫码进群|秒到账|秒到|代付|回收|招商"
)


def _payment_receipt_guard(result: dict, text_basis: str) -> dict:
    """个人支付凭证截图防误判：支付凭证特征明显 + 无引流话术 → advertising 降级为 normal。"""
    if not isinstance(result, dict) or not result.get("is_violation"):
        return result
    if result.get("violation_type") != "advertising":
        return result
    text = text_basis or ""
    if _PAYMENT_RECEIPT_EVIDENCE.search(text) and not _AD_SOLICIT_WORDS.search(text):
        logger.info(f"图片审核防误判: 个人支付凭证截图 → 降级为 normal（依据: {text[:50]}）")
        return {**result,
                "is_violation": False,
                "confidence": 0.3,
                "reason": "个人支付凭证截图，非广告"}
    return result


# ═══════════════════════════════════════════════════════════
#  MiniMax 多模态审核
# ═══════════════════════════════════════════════════════════

async def _get_mm_client():
    """返回图片审核要用的模型名（客户端由 llm_call 共享管理）。无 api_key 返回 None。

    固定走 vision_model（已验证支持图片输入）—— 主模型 gemini-3.5-flash-free
    通过此 API 网关时不支持 vision，所以图片审核不能用 ms['model']。
    """
    from bot import memory
    from . import llm_call
    if llm_call.get_client() is None:
        return None
    ms = memory.load_model_settings()
    return ms.get("vision_model") or ms.get("fallback_model") or "minimax-m3-free"

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
    # model 现在只是占位，llm_create 会用 provider 自己的 model 覆盖

    # ★ 先查 media_reader 的图片描述缓存：命中则直接做文本审核，
    # 不再下载图片、不再调 vision（同图去重，节省额度和带宽）
    cached_desc = None
    if image_url:
        try:
            from . import media_reader as _mr
            cached_desc = _mr.get_cached_description(image_url)
        except Exception:
            pass

    # 获取图片 base64 数据（缓存命中时跳过）
    img_b64 = None
    media_type = "image/jpeg"

    if not cached_desc:
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
    text_prompt = VISION_MODERATION_PROMPT
    if caption:
        text_prompt += f"\n\n图片附带的文字说明: {caption}"

    if cached_desc:
        # 基于已有描述做审核（文本输入，比再发一次图片省额度）
        text_prompt += f"\n\n已有图片描述（基于此判断）: {cached_desc}"
        user_content.append({"type": "text", "text": text_prompt})
    else:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_b64},
        })
        user_content.append({"type": "text", "text": text_prompt})

    try:
        from . import llm_call
        response = await llm_call.llm_create(
            priority="P3",
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

        # 个人支付凭证防误判（确定性复核，覆盖缓存描述与 vision 两条路径）
        text_basis = " ".join(filter(None, [
            caption, cached_desc,
            result.get("ocr_text", ""), result.get("reason", ""),
        ]))
        result = _payment_receipt_guard(result, text_basis)

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
#  Vision 审核 + GIF 抽帧 + 并发调度（OCR 降级已废弃 — minimax-m3-free 的 Vision 自带 OCR）
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
    图片审核：Vision API 主路径 + 一次重试。
    不再依赖本地 OCR（minimax-m3-free 的 Vision 已能读图中文字）。
    GIF 仍走关键帧抽取后逐帧送 Vision。

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

    # ── 第 1 次 Vision ──
    try:
        vision = await review_image_with_ai(bot, file_id, image_url, caption)
        if vision is not None:
            _cache_result(image_url, vision)
            return vision
    except Exception as e:
        logger.info(f"Vision 首次失败，准备重试: {str(e)[:80]}")

    # ── 重试一次（GIF 先抽帧，避免网关拒绝动图）──
    try:
        # 对 GIF：下载后抽帧，第一帧作为静态图送审
        if image_url and image_url.lower().endswith(".gif"):
            img_bytes = await _download_image_bytes(image_url)
            if img_bytes:
                frames = _extract_gif_frames(img_bytes)
                if len(frames) > 1:
                    # 把第一帧转成 base64 塞回 file_id 参数走静态图审核
                    import tempfile, os as _os, base64 as _b64
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.write(frames[0])
                    tmp.close()
                    try:
                        vision = await review_image_with_ai(bot, tmp.name, "", caption)
                    finally:
                        try: _os.unlink(tmp.name)
                        except Exception: pass
                    if vision is not None:
                        _cache_result(image_url, vision)
                        return vision

        # 非 GIF 或 GIF 抽帧失败 → 直接重试一次原参数
        vision = await review_image_with_ai(bot, file_id, image_url, caption)
        if vision is not None:
            _cache_result(image_url, vision)
            return vision
    except Exception as e:
        logger.warning(f"Vision 重试仍失败: {str(e)[:80]}")

    return None


def _cache_result(url: str, result: dict):
    if not url:
        return
    _mod_cache[url] = {"result": result, "ts": _time.time()}
    try:
        _save_mod_cache(_mod_cache)
    except Exception:
        pass


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
