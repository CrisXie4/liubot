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
