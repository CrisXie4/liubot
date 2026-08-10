# 多内容审核与入群放行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 bot 能审核合并转发消息、识别违规图片（含 GIF/表情）、Vision 失败时 OCR 降级，并支持主人放行入群。

**Architecture:** 新建统一内容提取器 `bot/content_reader.py`，把 OneBot 消息（文本/图片/合并转发/表情）拍平成结构化 payload；自动审核和手动标记两条路径共享它。图片审核走 Vision→OCR→文本 LLM 三级降级。入群放行通过 `verify.py` 的持久化白名单 + 4 个主人命令实现。

**Tech Stack:** Python 3.10+, anthropic SDK（已有）, Pillow + pytesseract（新增）, asyncio.Semaphore 并发, unittest（项目现有测试风格）

## Global Constraints

- **Python 版本**: 3.10+（代码用 `list[dict]` / `dict | None` 等新语法，已有代码也是这样）
- **测试运行方式**: `python tests/test_xxx.py`（项目不用 pytest CLI，用 unittest 直接跑，参考 `tests/test_group_events.py:1`）
- **不引入 pytest 为依赖**: 新测试文件用 `unittest.TestCase` + 末尾 `if __name__ == "__main__": unittest.main()`
- **LLM 调用统一走 `bot.llm_call.llm_create`**（已有 429 重试包装），不要直接 `client.messages.create`
- **所有新增 `await` 调用必须 try/except 包裹**，失败降级而非崩主消息流（spec 4.6）
- **不修改 `automod.score_message` / `llm_mod.llm_moderate` 的函数签名**（保持兼容）
- **新命令只能由 `OWNER_QQ` 调用**（沿用 `handler._is_owner`）
- **CQ 码格式**: `[CQ:type,key=value,key2=value2]`；NapCat API 走 `bot._call(action, params)` 或已有包装方法
- **Tesseract 默认检测路径**（`image_mod.py:354` 已有）: `E:\ocr\tesseract.exe` > `C:\Program Files\Tesseract-OCR\` > `C:\Program Files (x86)\Tesseract-OCR\`
- **数据目录**: `data/`，运行时自动创建（`config.DATA_DIR`）
- **中文 commit message**，匹配现有 `git log` 风格（如 `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`, `test:`）

---

## File Structure

**新增文件**:
- `bot/content_reader.py` — 统一内容提取器（合并转发/图片/表情解析）
- `tests/test_content_reader.py` — content_reader 单元测试
- `tests/test_image_fallback.py` — 三级降级 + GIF + 并发上限测试
- `tests/test_verify_whitelist.py` — 白名单 + 放行测试
- `tests/test_handler_moderation.py` — 自动审核集成测试
- `tests/test_violation_image.py` — 手动标记图片/转发测试
- `data/verify_whitelist.json` — 白名单持久化（运行时创建，不手写）
- `data/image_mod_cache.json` — 图片审核结果缓存（运行时创建）
- `scripts/smoke_test_forward.py` — 可选手动验证脚本

**修改文件**:
- `bot/image_mod.py` — 新增 `review_image_with_fallback` + `_extract_gif_frames` + `_audit_all_media` + 24h 缓存；重新启用 `check_ocr` 路径检测
- `bot/handler.py` — `handle_group_message` 接入 content_reader + 并行审核 + 结果融合；新增 4 个主人命令分发
- `bot/violation.py` — `handle_violation` 替换失效图片分支为 content_reader
- `bot/verify.py` — `handle_member_increase` 加白名单短路；新增 `is_whitelisted` / `add_whitelist` / `remove_whitelist` / `list_whitelist` / `approve_pending`
- `bot/__init__.py` — 注册 `content_reader` 模块
- `requirements.txt` — 加 `pytesseract>=0.3.10`、`Pillow>=10.0.0`
- `.env.example` — 加 `IMAGE_OCR_FALLBACK=true`
- `README.md` — 新增命令说明 + Tesseract 安装链接

**任务依赖**:
```
Task 1 (content_reader)  ─┬→ Task 3 (handler 自动审核)  ─┐
Task 2 (image_mod 降级)  ─┘                              │
                                                          ├→ Task 6 (依赖与文档)
Task 4 (violation 手动) ──→ 依赖 Task 1, 2               │
Task 5 (verify 白名单) ──→ 独立                          ┘
```

---

## Task 1: 统一内容提取器 content_reader.py

**Files:**
- Create: `bot/content_reader.py`
- Create: `tests/test_content_reader.py`
- Modify: `bot/__init__.py` (注册模块)

**Interfaces:**
- Consumes: `bot.onebot_client.OneBotClient.get_forward_msg(id)` (已存在，`onebot_client.py:465`)
- Produces: `async def extract_full_content(bot, raw_message: str, segments: list[dict]) -> dict`，返回 schema:
  ```python
  {
    "text": str,
    "images": [{"url": str, "file_id": str}],
    "stickers": [{"url": str, "summary": str}],
    "has_forward": bool,
    "forward": {"text": str, "images": [...], "stickers": [...], "count": int, "nested": bool} | None,
    "errors": [str],
  }
  ```

- [ ] **Step 1: 写失败测试 — 合并转发文本提取**

Create `tests/test_content_reader.py`:

```python
"""content_reader 单元测试 — 用 python tests/test_content_reader.py 直接跑"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import content_reader


def _run(coro):
    """同步跑 async 测试"""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestExtractForward(unittest.TestCase):

    def setUp(self):
        # 每次清缓存
        content_reader._forward_cache.clear()

    def test_forward_text_extraction(self):
        """合并转发能提取子消息文本"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [
                {"nickname": "Alice", "message": [{"type": "text", "data": {"text": "加微信 vx123"}}]},
                {"nickname": "Bob", "message": [{"type": "text", "data": {"text": "免费领取"}}]},
            ]
        })
        raw = "[CQ:forward,id=abc123]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertTrue(result["has_forward"])
        self.assertEqual(result["forward"]["count"], 2)
        self.assertIn("加微信 vx123", result["forward"]["text"])
        self.assertIn("免费领取", result["forward"]["text"])

    def test_forward_resid_expired(self):
        """resId 过期：返回 errors + 占位符，不抛异常"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value=None)
        raw = "[CQ:forward,id=expired_id]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertTrue(result["has_forward"])
        self.assertEqual(result["forward"]["text"], "[合并转发-无法解析]")
        self.assertTrue(len(result["errors"]) > 0)

    def test_nested_forward_depth_limit(self):
        """嵌套 forward 深度上限 3"""
        bot = MagicMock()
        # 构造 5 层嵌套：每层 forward 包含一个文本 + 下一层 forward
        call_count = [0]
        async def fake_get_forward(*args, **kwargs):
            call_count[0] += 1
            depth = call_count[0]
            if depth > 5:
                return None
            return {"messages": [{"nickname": "u", "message": [
                {"type": "text", "data": {"text": f"L{depth}"}},
                {"type": "forward", "data": {"id": f"nest{depth+1}"}},
            ]}]}
        bot.get_forward_msg = fake_get_forward

        raw = "[CQ:forward,id=nest1]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        # 深度上限 3 → 最多展开 3 层（根 + 2 层嵌套）
        self.assertTrue(result["has_forward"])
        self.assertTrue(result["forward"]["nested"])
        # 第 4 层不该被展开（errors 里有深度告警）
        self.assertTrue(any("深度" in e or "depth" in e.lower() for e in result["errors"]))

    def test_forward_submessage_count_limit(self):
        """子消息条数上限 50"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [{"nickname": "u", "message": [{"type": "text", "data": {"text": f"msg{i}"}}]} for i in range(60)]
        })
        raw = "[CQ:forward,id=bigpack]"
        result = _run(content_reader.extract_full_content(bot, raw, []))
        self.assertEqual(result["forward"]["count"], 50)
        self.assertIn("msg49", result["forward"]["text"])
        self.assertNotIn("msg59", result["forward"]["text"])


class TestExtractImagesAndStickers(unittest.TestCase):

    def test_image_extraction_from_segments(self):
        """从消息段提取图片 url/file_id"""
        bot = MagicMock()
        segments = [
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"url": "http://x/a.jpg", "file": "abc.gif"}},
        ]
        result = _run(content_reader.extract_full_content(bot, "看图[CQ:image,url=http://x/a.jpg]", segments))
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["url"], "http://x/a.jpg")
        self.assertEqual(result["images"][0]["file_id"], "abc.gif")
        self.assertEqual(result["text"], "看图")

    def test_mface_extraction_as_sticker(self):
        """mface 商城表情归入 stickers，含 url + summary"""
        bot = MagicMock()
        segments = [{"type": "mface", "data": {"url": "http://x/sticker.png", "summary": "[委屈]"}}]
        result = _run(content_reader.extract_full_content(bot, "[CQ:mface,url=http://x/sticker.png,summary=[委屈]]", segments))
        self.assertEqual(len(result["stickers"]), 1)
        self.assertEqual(result["stickers"][0]["url"], "http://x/sticker.png")
        self.assertEqual(result["stickers"][0]["summary"], "[委屈]")

    def test_face_small_emoji_skipped(self):
        """face 基础表情被跳过（不进 images/stickers）"""
        bot = MagicMock()
        segments = [{"type": "face", "data": {"id": 96}}]
        result = _run(content_reader.extract_full_content(bot, "[CQ:face,id=96]", segments))
        self.assertEqual(result["images"], [])
        self.assertEqual(result["stickers"], [])


class TestMixedMessage(unittest.TestCase):

    def test_text_plus_image_plus_forward(self):
        """文本 + 图片 + 转发混合"""
        bot = MagicMock()
        bot.get_forward_msg = AsyncMock(return_value={
            "messages": [{"nickname": "u", "message": [{"type": "text", "data": {"text": "子消息"}}]}]
        })
        segments = [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": "http://x/a.jpg", "file": "a.jpg"}},
        ]
        raw = "看这个[CQ:image,file=a.jpg,url=http://x/a.jpg][CQ:forward,id=mix1]"
        result = _run(content_reader.extract_full_content(bot, raw, segments))
        self.assertEqual(result["text"], "看这个")
        self.assertEqual(len(result["images"]), 1)
        self.assertTrue(result["has_forward"])
        self.assertIn("子消息", result["forward"]["text"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python tests/test_content_reader.py`
Expected: ImportError — `cannot import name 'content_reader' from 'bot'`

- [ ] **Step 3: 实现 content_reader.py**

Create `bot/content_reader.py`:

```python
"""
统一内容提取器 — 把任意 OneBot 消息拍平成结构化 payload。

合并转发（[CQ:forward,id=xxx]）通过 NapCat get_forward_msg API 反查；
嵌套转发递归展开（深度上限 3）；图片/表情从 segments 提取。

自动审核（handler）和手动标记（violation）两条路径共享此提取器，
消除"不对称 bug"。
"""
import asyncio
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("qqbot")

# ─── 常量 ───
FORWARD_DEPTH_LIMIT = 3
FORWARD_SUBMSG_LIMIT = 50
FORWARD_CACHE_TTL = 300  # 5 分钟

# resId → (result_text, ts)
_forward_cache: dict[str, tuple[str, list[dict], list[dict], bool]] = {}

# ─── CQ 码解析（与 media_reader 一致的 regex）───
_CQ_FORWARD_RE = re.compile(r"\[CQ:forward,id=([^\],]+)\]")


async def extract_full_content(bot, raw_message: str, segments: list[dict]) -> dict:
    """
    把任意 OneBot 消息拍平。

    Args:
        bot: OneBotClient（用其 get_forward_msg）
        raw_message: 原始 CQ 字符串
        segments: 消息段数组（event["message"]）

    Returns: 见模块 docstring 的 schema
    """
    text_parts: list[str] = []
    images: list[dict] = []
    stickers: list[dict] = []
    errors: list[str] = []
    has_forward = False
    forward_block: Optional[dict] = None

    # 1. 从 segments 提取文本/图片/表情
    for seg in segments or []:
        t = seg.get("type", "")
        data = seg.get("data", {})
        if t == "text":
            text_parts.append(data.get("text", ""))
        elif t == "image":
            url = data.get("url", "")
            file_id = data.get("file", "")
            if url:
                images.append({"url": url, "file_id": file_id})
        elif t == "mface":
            url = data.get("url", "")
            if url:
                stickers.append({"url": url, "summary": data.get("summary", "")})
        # face / record / video 等：本期 images/stickers 不收

    # 2. 兜底：如果 segments 没有 text，从 raw_message 剥 CQ 码取纯文本
    if not text_parts and raw_message:
        clean = re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
        if clean:
            text_parts.append(clean)

    # 3. 检测合并转发
    forward_ids = _CQ_FORWARD_RE.findall(raw_message or "")
    if forward_ids:
        has_forward = True
        fwd_text_parts: list[str] = []
        fwd_images: list[dict] = []
        fwd_stickers: list[dict] = []
        total_count = 0
        nested_seen = False

        for fid in forward_ids:
            sub_text, sub_imgs, sub_stks, sub_nested, sub_count, sub_errors = await _unfold_forward(
                bot, fid, depth=1
            )
            fwd_text_parts.append(sub_text)
            fwd_images.extend(sub_imgs)
            fwd_stickers.extend(sub_stks)
            if sub_nested:
                nested_seen = True
            total_count += sub_count
            errors.extend(sub_errors)

        forward_block = {
            "text": "\n".join(p for p in fwd_text_parts if p),
            "images": fwd_images,
            "stickers": fwd_stickers,
            "count": total_count,
            "nested": nested_seen,
        }

    return {
        "text": "".join(text_parts).strip(),
        "images": images,
        "stickers": stickers,
        "has_forward": has_forward,
        "forward": forward_block,
        "errors": errors,
    }


async def _unfold_forward(bot, forward_id: str, depth: int) -> tuple:
    """
    递归展开合并转发。返回 (text, images, stickers, nested, count, errors)。

    深度超过 FORWARD_DEPTH_LIMIT 时停止递归，记错误。
    """
    text_parts: list[str] = []
    images: list[dict] = []
    stickers: list[dict] = []
    errors: list[str] = []
    nested = False
    count = 0

    # 深度上限检查
    if depth > FORWARD_DEPTH_LIMIT:
        errors.append(f"合并转发嵌套深度超限 (id={forward_id}, depth={depth})")
        return ("", images, stickers, nested, count, errors)

    # 缓存命中
    cached = _forward_cache.get(forward_id)
    if cached and time.time() - cached[3] < FORWARD_CACHE_TTL:
        # 注意：缓存只存扁平内容（不递归），简化处理
        return (cached[0], cached[1], cached[2], nested, count, errors)

    # 调 NapCat API
    try:
        data = await bot.get_forward_msg(id=forward_id)
    except Exception as e:
        errors.append(f"get_forward_msg 异常 (id={forward_id}): {str(e)[:80]}")
        return ("[合并转发-无法解析]", images, stickers, nested, count, errors)

    if not data or not isinstance(data, dict):
        errors.append(f"合并转发无法解析 (id={forward_id})")
        return ("[合并转发-无法解析]", images, stickers, nested, count, errors)

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    # 子消息条数上限
    truncated = False
    if len(messages) > FORWARD_SUBMSG_LIMIT:
        messages = messages[:FORWARD_SUBMSG_LIMIT]
        truncated = True

    for sub in messages:
        count += 1
        nickname = sub.get("nickname", "")
        sub_segs = sub.get("message", [])
        if not isinstance(sub_segs, list):
            sub_segs = []
        sub_text_parts: list[str] = []
        for seg in sub_segs:
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                sub_text_parts.append(d.get("text", ""))
            elif t == "image":
                url = d.get("url", "")
                if url:
                    images.append({"url": url, "file_id": d.get("file", "")})
            elif t == "mface":
                url = d.get("url", "")
                if url:
                    stickers.append({"url": url, "summary": d.get("summary", "")})
            elif t == "forward":
                nested = True
                nested_id = d.get("id", "")
                if nested_id and depth < FORWARD_DEPTH_LIMIT:
                    n_text, n_imgs, n_stks, n_nested, n_count, n_errors = await _unfold_forward(
                        bot, nested_id, depth + 1
                    )
                    sub_text_parts.append(n_text)
                    images.extend(n_imgs)
                    stickers.extend(n_stks)
                    if n_nested:
                        nested = True
                    count += n_count
                    errors.extend(n_errors)
                elif nested_id:
                    errors.append(f"嵌套转发未展开 (depth={depth+1}, id={nested_id})")
        sub_text = "".join(sub_text_parts).strip()
        if sub_text:
            prefix = f"[{nickname}] " if nickname else ""
            text_parts.append(f"{prefix}{sub_text}")

    if truncated:
        errors.append(f"子消息超过 {FORWARD_SUBMSG_LIMIT} 条上限 (id={forward_id})")

    combined = "\n".join(text_parts)
    # 写缓存
    _forward_cache[forward_id] = (combined, images, stickers, time.time())
    return (combined, images, stickers, nested, count, errors)
```

- [ ] **Step 4: 注册模块**

Modify `bot/__init__.py` — 找到现有的 `from . import media_reader` 那一行（约 `__init__.py:19`），在它后面加一行 `from . import content_reader`。

- [ ] **Step 5: 运行测试验证通过**

Run: `python tests/test_content_reader.py`
Expected: `OK` (all tests pass)

- [ ] **Step 6: Commit**

```bash
git add bot/content_reader.py bot/__init__.py tests/test_content_reader.py
git commit -m "feat: 新增 content_reader 统一内容提取器

把 OneBot 消息（文本/图片/合并转发/表情）拍平成结构化 payload
自动审核与手动标记两路共享提取逻辑
合并转发递归展开（深度上限 3、子消息上限 50）"
```

---

## Task 2: 图片审核三级降级 + GIF 抽帧 + 并发调度

**Files:**
- Modify: `bot/image_mod.py` (新增 `review_image_with_fallback` / `_extract_gif_frames` / `_audit_all_media` / 24h 缓存；重新启用 `check_ocr`)
- Create: `tests/test_image_fallback.py`

**Interfaces:**
- Consumes: `bot.llm_call.llm_create` (已有), `bot.image_mod.review_image_with_ai` (已有，`image_mod.py:148`), `bot.llm_mod.llm_moderate` (已有)
- Produces:
  - `async def review_image_with_fallback(bot, file_id: str, image_url: str = "", caption: str = "") -> Optional[dict]` — 三级降级，返回与 `review_image_with_ai` 同 schema 的 dict 或 None
  - `def _extract_gif_frames(raw_bytes: bytes) -> list[bytes]` — GIF 关键帧提取（≤3 帧）
  - `async def _audit_all_media(bot, images: list[dict], caption: str = "") -> list[dict]` — 并发审所有图（Semaphore(5)，上限 20）

- [ ] **Step 1: 写失败测试 — Vision 成功不调 OCR**

Create `tests/test_image_fallback.py`:

```python
"""image_mod 三级降级 + GIF + 并发调度测试"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import image_mod


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestReviewImageWithFallback(unittest.TestCase):

    def setUp(self):
        image_mod._mod_cache.clear()

    def test_vision_success_skips_ocr(self):
        """Vision 返回结果 → 不调 OCR"""
        bot = MagicMock()
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(return_value={
            "is_violation": True, "confidence": 0.9, "violation_type": "ad",
            "ocr_text": "加微信", "reason": "广告",
        })), patch.object(image_mod, "_ocr_image_bytes", new=AsyncMock(return_value="")) as mock_ocr:
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/a.jpg"))
            self.assertTrue(result["is_violation"])
            mock_ocr.assert_not_called()

    def test_vision_failure_triggers_ocr(self):
        """Vision 抛超时 → 触发 OCR → OCR 拿到文字 → llm_moderate"""
        bot = MagicMock()
        ocr_result = "加微信 vx123 免费领取"
        llm_result = {
            "is_violation": True, "confidence": 0.85, "violation_type": "ad",
            "ocr_text": ocr_result, "reason": "OCR 后判定广告",
        }
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(side_effect=TimeoutError("API down"))), \
             patch.object(image_mod, "_ocr_image_bytes", new=AsyncMock(return_value=ocr_result)), \
             patch("bot.llm_mod.llm_moderate", new=AsyncMock(return_value=llm_result)):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/a.jpg"))
            self.assertIsNotNone(result)
            self.assertTrue(result["is_violation"])
            self.assertIn("加微信", result.get("ocr_text", ""))

    def test_all_fail_returns_none(self):
        """Vision + OCR + LLM 全失败 → 返回 None"""
        bot = MagicMock()
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock(return_value=None)), \
             patch.object(image_mod, "_ocr_image_bytes", new=AsyncMock(return_value="")):
            result = _run(image_mod.review_image_with_fallback(bot, "fid", "http://x/a.jpg"))
            self.assertIsNone(result)

    def test_cache_hit_skips_api(self):
        """缓存命中 → 不调 Vision"""
        bot = MagicMock()
        image_mod._mod_cache["http://x/cached.jpg"] = {"result": {"is_violation": False}, "ts": __import__("time").time()}
        with patch.object(image_mod, "review_image_with_ai", new=AsyncMock()) as mock_v:
            result = _run(image_mod.review_image_with_fallback(bot, "", "http://x/cached.jpg"))
            mock_v.assert_not_called()
            self.assertFalse(result["is_violation"])


class TestGifFrameExtraction(unittest.TestCase):

    def test_static_image_returns_as_is(self):
        """静态图（1 帧）→ 返回原 bytes"""
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buf, format="PNG")
        raw = buf.getvalue()
        frames = image_mod._extract_gif_frames(raw)
        self.assertEqual(len(frames), 1)

    def test_gif_5_frames_picks_3(self):
        """5 帧 GIF → 抽首/中/末 3 帧"""
        from PIL import Image
        import io
        frames_in = []
        for i in range(5):
            buf = io.BytesIO()
            Image.new("RGB", (10, 10), color=(i * 50, 0, 0)).save(buf, format="GIF")
            frames_in.append(buf.getvalue())
        # 拼成多帧 GIF
        imgs = [Image.open(io.BytesIO(f)) for f in frames_in]
        buf_all = io.BytesIO()
        imgs[0].save(buf_all, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
        gif_bytes = buf_all.getvalue()
        frames = image_mod._extract_gif_frames(gif_bytes)
        self.assertEqual(len(frames), 3)

    def test_gif_2_frames_picks_all(self):
        """2 帧 GIF → 全提"""
        from PIL import Image
        import io
        imgs = [Image.new("RGB", (10, 10), color=(c, 0, 0)) for c in (0, 255)]
        buf = io.BytesIO()
        imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
        frames = image_mod._extract_gif_frames(buf.getvalue())
        self.assertEqual(len(frames), 2)


class TestAuditAllMedia(unittest.TestCase):

    def setUp(self):
        image_mod._mod_cache.clear()

    def test_concurrency_limit_5(self):
        """11 张图时同时 in-flight ≤ 5"""
        import time
        call_times = []
        async def fake_review(bot, fid, url, caption=""):
            call_times.append(time.time())
            await asyncio.sleep(0.05)
            return {"is_violation": False, "confidence": 0, "ocr_text": "", "reason": ""}
        images = [{"url": f"http://x/{i}.jpg", "file_id": str(i)} for i in range(11)]
        with patch.object(image_mod, "review_image_with_fallback", new=fake_review):
            _run(image_mod._audit_all_media(MagicMock(), images))
        # 验证前 5 个调用时间几乎同时，后 6 个明显晚于前 5 个结束
        first_batch = sorted(call_times[:5])
        self.assertTrue(all(t - first_batch[0] < 0.02 for t in first_batch))

    def test_cap_at_20_images(self):
        """25 张图 → 截到 20"""
        async def fake_review(bot, fid, url, caption=""):
            return {"is_violation": False}
        images = [{"url": f"http://x/{i}.jpg", "file_id": str(i)} for i in range(25)]
        with patch.object(image_mod, "review_image_with_fallback", new=fake_review) as mock_r:
            results = _run(image_mod._audit_all_media(MagicMock(), images))
            self.assertEqual(mock_r.call_count, 20)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python tests/test_image_fallback.py`
Expected: AttributeError — `module 'bot.image_mod' has no attribute 'review_image_with_fallback'`

- [ ] **Step 3: 实现 review_image_with_fallback + GIF + _audit_all_media + 缓存**

Append to `bot/image_mod.py` (在文件末尾追加，不动现有代码):

```python


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
    # 异步写盘（这里同步写，调用频率不高）
    try:
        _save_mod_cache(_mod_cache)
    except Exception:
        pass


def _ocr_available_or_init() -> bool:
    """检测 Tesseract 是否可用（沿用 check_ocr 路径检测）"""
    global _ocr_available
    if _ocr_available is None:
        check_ocr()  # 复用已有检测逻辑（设置 _ocr_available）
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python tests/test_image_fallback.py`
Expected: all tests pass (`OK`)

注意：`test_gif_*` 测试需要 Pillow，跑前确认已 `pip install Pillow`（暂可临时安装；Task 6 会正式加到 requirements）。

- [ ] **Step 5: Commit**

```bash
git add bot/image_mod.py tests/test_image_fallback.py
git commit -m "feat: 图片审核三级降级 + GIF 抽帧 + 并发调度

Vision → OCR → 文本 LLM 三级降级链
GIF 关键帧提取（≤3帧全提，>3帧取首/中/末）
_audit_all_media 用 Semaphore(5) 并发，单条消息媒体上限 20
24h URL 缓存避免重复审核同一张图"
```

---

## Task 3: 自动审核接入 handler.py

**Files:**
- Modify: `bot/handler.py` (在 `handle_group_message` 内替换 `media_ctx` 那段；接入并行审核 + 融合)
- Create: `tests/test_handler_moderation.py`

**Interfaces:**
- Consumes: `bot.content_reader.extract_full_content` (Task 1), `bot.image_mod._audit_all_media` + `review_image_with_fallback` (Task 2)
- Produces: 修改 `handle_group_message` 的内部行为（无新公开接口）

- [ ] **Step 1: 写失败测试 — 多图都被审**

Create `tests/test_handler_moderation.py`:

```python
"""handler 自动审核集成测试 — 并行审核路径与融合判定"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHandlerAutoModeration(unittest.TestCase):

    def _make_event(self, raw_message: str, segments: list = None, user_id: int = 999):
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": 111,
            "user_id": user_id,
            "message_id": 222,
            "raw_message": raw_message,
            "message": segments or [{"type": "text", "data": {"text": raw_message}}],
            "sender": {"card": "", "nickname": "Tester"},
        }

    def test_multiple_images_all_audited(self):
        """5 张图都被审（不只第一张）"""
        from bot import handler, content_reader
        bot = MagicMock()
        bot.bot_qq = "1000"
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.delete_msg = AsyncMock(return_value=True)
        bot.get_group_member_info = AsyncMock(return_value=None)

        images = [{"url": f"http://x/{i}.jpg", "file_id": str(i)} for i in range(5)]
        content = {"text": "", "images": images, "stickers": [],
                   "has_forward": False, "forward": None, "errors": []}

        audit_calls = []
        async def fake_audit(b, imgs, caption=""):
            audit_calls.append(len(imgs))
            return [{"is_violation": False, "confidence": 0, "ocr_text": "", "reason": ""} for _ in imgs]

        with patch.object(content_reader, "extract_full_content", new=AsyncMock(return_value=content)), \
             patch("bot.image_mod._audit_all_media", new=fake_audit), \
             patch("bot.llm_mod.llm_moderate", new=AsyncMock(return_value={
                 "is_violation": False, "confidence": 0, "keywords_matched": [], "reason": "",
                 "source": "keyword_only", "category": "normal", "suggested_action": "none",
                 "latency_ms": 0, "llm_confidence": 0, "llm_reason": "",
             })), \
             patch.object(handler, "_is_bot_admin", new=AsyncMock(return_value=True)), \
             patch.object(handler, "_is_owner", new=MagicMock(return_value=False)), \
             patch("bot.config.config.is_admin_mode", new=MagicMock(return_value=True)):
            _run(handler.handle_group_message(bot, self._make_event("[CQ:image]", [
                {"type": "image", "data": {"url": "http://x/0.jpg", "file": "0"}}
            ])))
        self.assertEqual(sum(audit_calls), 5)

    def test_any_image_violation_triggers_recall(self):
        """任一图判违规 → final_violation 被覆盖为 True"""
        from bot import handler, content_reader
        bot = MagicMock()
        bot.bot_qq = "1000"
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.delete_msg = AsyncMock(return_value=True)
        bot.get_group_member_info = AsyncMock(return_value=None)

        content = {"text": "", "images": [{"url": "http://x/a.jpg", "file_id": "a"}],
                   "stickers": [], "has_forward": False, "forward": None, "errors": []}
        audit_result = [{"is_violation": True, "confidence": 0.9, "violation_type": "ad",
                         "ocr_text": "加微信", "reason": "广告", "keywords_matched": []}]

        with patch.object(content_reader, "extract_full_content", new=AsyncMock(return_value=content)), \
             patch("bot.image_mod._audit_all_media", new=AsyncMock(return_value=audit_result)), \
             patch("bot.llm_mod.llm_moderate", new=AsyncMock(return_value={
                 "is_violation": False, "confidence": 0, "keywords_matched": [], "reason": "",
                 "source": "keyword_only", "category": "normal", "suggested_action": "none",
                 "latency_ms": 0, "llm_confidence": 0, "llm_reason": "",
             })), \
             patch.object(handler, "_is_bot_admin", new=AsyncMock(return_value=True)), \
             patch.object(handler, "_is_owner", new=MagicMock(return_value=False)), \
             patch("bot.config.config.is_admin_mode", new=MagicMock(return_value=True)):
            _run(handler.handle_group_message(bot, self._make_event("[CQ:image]", [
                {"type": "image", "data": {"url": "http://x/a.jpg", "file": "a"}}
            ])))
        # 撤回被调
        bot.delete_msg.assert_called()

    def test_forward_text_feeds_llm_moderate(self):
        """转发子消息文本被并入 effective_text 喂审核"""
        from bot import handler, content_reader
        bot = MagicMock()
        bot.bot_qq = "1000"
        bot.send_group_msg = AsyncMock(return_value=1)
        bot.delete_msg = AsyncMock(return_value=True)
        bot.get_group_member_info = AsyncMock(return_value=None)

        content = {"text": "看这个", "images": [], "stickers": [],
                   "has_forward": True,
                   "forward": {"text": "子消息:加微信vx123", "images": [], "stickers": [],
                               "count": 1, "nested": False},
                   "errors": []}
        captured_text = []
        async def fake_llm_mod(raw, **kwargs):
            captured_text.append(raw)
            return {"is_violation": False, "confidence": 0, "keywords_matched": [],
                    "reason": "", "source": "keyword_only", "category": "normal",
                    "suggested_action": "none", "latency_ms": 0,
                    "llm_confidence": 0, "llm_reason": ""}

        with patch.object(content_reader, "extract_full_content", new=AsyncMock(return_value=content)), \
             patch("bot.llm_mod.llm_moderate", new=fake_llm_mod), \
             patch.object(handler, "_is_bot_admin", new=AsyncMock(return_value=True)), \
             patch.object(handler, "_is_owner", new=MagicMock(return_value=False)), \
             patch("bot.config.config.is_admin_mode", new=MagicMock(return_value=True)):
            _run(handler.handle_group_message(bot, self._make_event("[CQ:forward,id=x]")))
        # llm_moderate 收到的文本包含子消息文本
        self.assertTrue(any("子消息:加微信vx123" in t for t in captured_text))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python tests/test_handler_moderation.py`
Expected: 失败（当前代码不会对所有图调 `_audit_all_media`，也不调 content_reader）

- [ ] **Step 3: 修改 handler.py 接入新审核管道**

Modify `bot/handler.py` — 找到 `handle_group_message` 中 `media_ctx = await media_reader.extract_media_context(raw_message) if raw_message else []` 那一行（约 `handler.py:918`），**替换**整段直到 `has_sticker_code = ...` 行（约 `handler.py:923`）为：

```python
    # ── 提取消息中的媒体内容（用于 AI 上下文）──
    # 新增：统一内容提取（合并转发 + 图片 + 表情）
    try:
        from . import content_reader
        content = await content_reader.extract_full_content(bot, raw_message, message)
    except Exception as e:
        logger.debug(f"content_reader 异常: {e}")
        content = {"text": _extract_text(message), "images": [], "stickers": [],
                   "has_forward": False, "forward": None, "errors": []}

    # 保留旧 media_ctx 给"自动收藏表情"/影响力追踪用（不破坏现有功能）
    try:
        media_ctx = await media_reader.extract_media_context(raw_message) if raw_message else []
    except Exception:
        media_ctx = []
    has_image = any(m.get("type") == "image" for m in media_ctx)
    has_video = any(m.get("type") == "video" for m in media_ctx)
    has_sticker_code = any(m.get("type") == "sticker" for m in media_ctx)

    # ── 并行审核：文本 + 全量图片/表情/GIF ──
    # 构造 effective_text（含合并转发子消息）
    effective_text = content["text"]
    if content["has_forward"] and content["forward"]:
        effective_text = (effective_text + "\n[合并转发]\n" + content["forward"]["text"]).strip()

    # 收集所有待审媒体（本条 + 转发包内）
    audit_images = list(content["images"])
    for s in content["stickers"]:
        if s.get("url"):
            audit_images.append({"url": s["url"], "file_id": "", "caption_hint": s.get("summary", "")})
    if content["has_forward"] and content["forward"]:
        audit_images += content["forward"]["images"]
        for s in content["forward"]["stickers"]:
            if s.get("url"):
                audit_images.append({"url": s["url"], "file_id": "", "caption_hint": s.get("summary", "")})

    # 异步并行：文本审核 + 图片审核（仅非主人/非官方）
    _owner_or_official = _is_owner(user_id) or _is_official_account(user_id)

    async def _no_media() -> list:
        return []

    async def _audit_media() -> list:
        from . import image_mod
        try:
            return await image_mod._audit_all_media(bot, audit_images, caption=effective_text)
        except Exception as e:
            logger.warning(f"图片审核异常: {e}")
            return []

    # 文本审核在下面原有 automod/llm_mod 块里跑（不改那段）
    # 这里只启动图片审核任务，等文本审核跑完后再 gather 结果
    # 用 create_task 让图片审核与文本审核并行
    image_audit_task = asyncio.create_task(_audit_media()) if (audit_images and not _owner_or_official) else None
```

然后在现有的 `final_violation` 判定完之后（约 `handler.py:1003` 的 `final_reason = "关键词引擎判定"` 之后），追加融合逻辑：

```python
    # ── 图片审核结果融合（Task 3 新增）──
    image_results = []
    if image_audit_task is not None:
        try:
            image_results = await image_audit_task
        except Exception as e:
            logger.warning(f"图片审核 await 异常: {e}")
            image_results = []
    for image_result in (image_results or []):
        if not image_result or not isinstance(image_result, dict):
            continue
        img_is_v = image_result.get("is_violation", False)
        img_conf = float(image_result.get("confidence", 0))
        if img_is_v and img_conf >= 0.7:
            final_violation = True
            final_confidence = max(final_confidence, img_conf)
            final_source = "image_vision"
            final_reason = image_result.get("reason", "") or final_reason
            img_keywords = [image_result.get("violation_type", "")]
            ocr_text = image_result.get("ocr_text", "")
            if ocr_text:
                try:
                    ocr_keywords = violation.extract_keywords(ocr_text, top_n=5)
                    combined_hits = list(set(combined_hits + img_keywords + ocr_keywords))
                except Exception:
                    combined_hits = list(set(combined_hits + img_keywords))
            break  # 任一媒体判违规即整体撤回
```

**注意**：`combined_hits` 变量在现有代码 `handler.py:1015` 才定义，需要把上面的融合块**移到** `combined_hits` 定义之后。检查现有代码顺序：`combined_hits` 在 `if final_violation and raw_message:` 块内定义（约 `handler.py:1015`）。所以融合块应该放在 `if final_violation and raw_message:` 块**内部**、`combined_hits` 定义之后、`if friend_mode:` 之前。

更稳的做法：在 `if final_violation and raw_message:` 块**之前**追加一个独立块，只把图片命中合并到 `final_violation` 判定上：

```python
    # ── 图片审核结果融合（Task 3 新增）──
    image_results = []
    if image_audit_task is not None:
        try:
            image_results = await image_audit_task
        except Exception as e:
            logger.warning(f"图片审核 await 异常: {e}")
            image_results = []
    _image_hits = []  # 收集图片维度命中词，后面并入 combined_hits
    for image_result in (image_results or []):
        if not image_result or not isinstance(image_result, dict):
            continue
        img_is_v = image_result.get("is_violation", False)
        img_conf = float(image_result.get("confidence", 0))
        if img_is_v and img_conf >= 0.7:
            final_violation = True
            final_confidence = max(final_confidence, img_conf)
            final_source = "image_vision"
            final_reason = image_result.get("reason", "") or final_reason
            _image_hits.append(image_result.get("violation_type", ""))
            ocr_text = image_result.get("ocr_text", "")
            if ocr_text:
                try:
                    _image_hits.extend(violation.extract_keywords(ocr_text, top_n=5))
                except Exception:
                    pass
            break
```

然后在现有 `combined_hits = list(set(all_hits + learner_hits + pattern_hits))` 那行**之后**，追加：

```python
        if _image_hits:
            combined_hits = list(set(combined_hits + [h for h in _image_hits if h]))[:8]
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python tests/test_handler_moderation.py`
Expected: all tests pass

- [ ] **Step 5: 跑现有测试确保无回归**

Run: `python tests/test_group_events.py`
Expected: 仍 OK

- [ ] **Step 6: Commit**

```bash
git add bot/handler.py tests/test_handler_moderation.py
git commit -m "feat: 自动审核接入 content_reader + 全量图片并行审核

合并转发子消息文本并入 effective_text 喂审核
所有图片/mface/GIF 都过 Vision 三级降级
任一图判违规即覆盖 final_violation，撤回公告含 OCR 关键词"
```

---

## Task 4: 手动标记修复 violation.py

**Files:**
- Modify: `bot/violation.py` (`handle_violation` 替换失效图片分支)
- Create: `tests/test_violation_image.py`

**Interfaces:**
- Consumes: `bot.content_reader.extract_full_content` (Task 1), `bot.image_mod._audit_all_media` (Task 2)
- Produces: 修改 `handle_violation` 内部行为（无新公开接口）

- [ ] **Step 1: 写失败测试 — 手动标记图片能提关键词**

Create `tests/test_violation_image.py`:

```python
"""violation 手动标记 — 图片/转发内容提取测试"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestViolationImage(unittest.TestCase):

    def test_image_violation_extracts_keywords(self):
        """手动标记广告图 → 特征词列表非空"""
        from bot import violation, content_reader
        bot = MagicMock()
        bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:image,file=abc,url=http://x/ad.jpg]",
            "message": [{"type": "image", "data": {"file": "abc", "url": "http://x/ad.jpg"}}],
            "user_id": 888,
            "sender": {"nickname": "AdSpammer"},
        })
        bot.get_group_info = AsyncMock(return_value={"group_name": "测试群"})
        bot.delete_msg = AsyncMock(return_value=True)
        bot.set_group_ban = AsyncMock(return_value=True)
        bot.send_group_msg = AsyncMock(return_value=1)

        content = {"text": "", "images": [{"url": "http://x/ad.jpg", "file_id": "abc"}],
                   "stickers": [], "has_forward": False, "forward": None, "errors": []}
        audit_result = [{"is_violation": True, "confidence": 0.9, "violation_type": "advertising",
                         "ocr_text": "加微信vx123 免费领取", "reason": "广告", "keywords_matched": []}]

        with patch.object(content_reader, "extract_full_content", new=AsyncMock(return_value=content)), \
             patch("bot.image_mod._audit_all_media", new=AsyncMock(return_value=audit_result)):
            _run(violation.handle_violation(bot, 111, 999, 222, "广告"))

        # 验证 send_group_msg 被调用且包含特征词（不是"无"）
        send_calls = bot.send_group_msg.call_args_list
        # 找包含"特征词"的发送
        feature_msg = [c for c in send_calls if "特征词" in str(c)]
        self.assertTrue(len(feature_msg) > 0)
        # 不应包含 "特征词: 无"
        self.assertNotIn("特征词: 无", str(feature_msg[0]))

    def test_forward_violation_extracts_submessage_keywords(self):
        """手动标记合并转发 → 子消息文本进关键词"""
        from bot import violation, content_reader
        bot = MagicMock()
        bot.get_msg = AsyncMock(return_value={
            "raw_message": "[CQ:forward,id=fwd1]",
            "message": [{"type": "forward", "data": {"id": "fwd1"}}],
            "user_id": 888,
            "sender": {"nickname": "Spammer"},
        })
        bot.get_group_info = AsyncMock(return_value={"group_name": "测试群"})
        bot.delete_msg = AsyncMock(return_value=True)
        bot.set_group_ban = AsyncMock(return_value=True)
        bot.send_group_msg = AsyncMock(return_value=1)

        content = {"text": "", "images": [], "stickers": [],
                   "has_forward": True,
                   "forward": {"text": "加微信vx123 免费领取", "images": [], "stickers": [],
                               "count": 1, "nested": False},
                   "errors": []}

        with patch.object(content_reader, "extract_full_content", new=AsyncMock(return_value=content)), \
             patch("bot.image_mod._audit_all_media", new=AsyncMock(return_value=[])):
            _run(violation.handle_violation(bot, 111, 999, 222))

        feature_msg = [c for c in bot.send_group_msg.call_args_list if "特征词" in str(c)]
        self.assertTrue(len(feature_msg) > 0)
        self.assertNotIn("特征词: 无", str(feature_msg[0]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python tests/test_violation_image.py`
Expected: 失败（现有代码图片分支进不去，特征词会显示"无"）

- [ ] **Step 3: 修改 violation.py 替换图片分支**

Modify `bot/violation.py` — 找到 `handle_violation` 中 `# --- 图片消息: 尝试 OCR ---` 那段（约 `violation.py:256-272`），**替换**为：

```python
    # --- 统一内容提取（合并转发 + 图片 + 表情）---
    from . import content_reader, image_mod
    try:
        content = await content_reader.extract_full_content(bot, raw_message, message_segments)
    except Exception as e:
        logger.warning(f"content_reader 提取失败，降级: {e}")
        content = {"text": raw_message if isinstance(raw_message, str) else "",
                   "images": [], "stickers": [], "has_forward": False,
                   "forward": None, "errors": []}

    # 构造 effective_text（含合并转发子消息）
    effective_text = content["text"]
    if content["has_forward"] and content["forward"]:
        effective_text = (effective_text + "\n[合并转发]\n" + content["forward"]["text"]).strip()

    # 收集所有图片（本条 + 转发包内 + 表情）
    all_images = list(content["images"])
    for s in content["stickers"]:
        if s.get("url"):
            all_images.append({"url": s["url"], "file_id": "", "caption_hint": s.get("summary", "")})
    if content["has_forward"] and content["forward"]:
        all_images += content["forward"]["images"]
        for s in content["forward"]["stickers"]:
            if s.get("url"):
                all_images.append({"url": s["url"], "file_id": "", "caption_hint": s.get("summary", "")})

    # 走 Vision 三级降级（全审）
    vision_results = []
    if all_images:
        try:
            vision_results = await image_mod._audit_all_media(bot, all_images, caption=effective_text) or []
        except Exception as e:
            logger.warning(f"手动标记图片审核异常: {e}")
            vision_results = []
        for vr in vision_results:
            if not vr:
                continue
            ocr = vr.get("ocr_text", "")
            if ocr:
                effective_text = effective_text + "\n[图片内容] " + ocr

    # 用 effective_text 替代 raw_message 做后续分析
    raw_message = effective_text
```

**注意**：这里复用变量名 `raw_message`，让下面的 `analyze_violation_message(raw_message)` / `db.add_violation(raw_message=...)` / `learner.learn_from_violation(raw_message, ...)` 不用改，都拿到真实内容。

然后找到 `# --- 3. 分析特征 ---` 段，把 `vision_results` 的违规类型并入 keywords（在 `analysis = analyze_violation_message(raw_message)` 之后）：

```python
    # --- 3. 分析特征 ---
    analysis = analyze_violation_message(raw_message)

    # 把图片 Vision 违规类型并入 keywords（Task 4）
    for vr in vision_results:
        if vr and vr.get("violation_type") not in ("none", None, ""):
            analysis["keywords"] = list(set(
                analysis["keywords"] + [vr.get("violation_type", "")]
            ))[:10]
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python tests/test_violation_image.py`
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add bot/violation.py tests/test_violation_image.py
git commit -m "fix: 手动标记接入 content_reader 修复图片/转发提取

替换失效的 raw_message=='[图片]' 判断
图片走 Vision 三级降级，合并转发子消息进 effective_text
'手动标记提不到关键词'问题修复"
```

---

## Task 5: 入群放行白名单 + 4 个主人命令

**Files:**
- Modify: `bot/verify.py` (新增 `is_whitelisted` / `add_whitelist` / `remove_whitelist` / `list_whitelist` / `approve_pending`；`handle_member_increase` 加白名单短路)
- Modify: `bot/handler.py` (新增 4 个主人命令分发)
- Create: `tests/test_verify_whitelist.py`

**Interfaces:**
- Consumes: `bot.verify._pending` (已有), `bot.verify._cleanup_user` (已有), `bot.verify._log_verification` (已有), `bot.welcome.get_welcome_message` / `add_points` (已有)
- Produces:
  - `def is_whitelisted(user_id: int, group_id: int) -> bool`
  - `def add_whitelist(user_id: int, group_id: int = 0) -> bool` — `group_id=0` 表示全局
  - `def remove_whitelist(user_id: int, group_id: int = 0) -> bool`
  - `def list_whitelist(group_id: int = 0) -> dict` — 返回 `{"global": [...], "by_group": {...}}`
  - `async def approve_pending(bot, group_id: int, user_id: int) -> bool` — 立即放行待验证者

- [ ] **Step 1: 写失败测试 — 白名单匹配与持久化**

Create `tests/test_verify_whitelist.py`:

```python
"""verify 白名单 + 放行业务函数测试"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestVerifyWhitelist(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        # 用临时文件隔离测试
        self._orig_file = verify._WHITELIST_FILE
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        verify._WHITELIST_FILE = self.tmp.name
        # 清内存
        verify._whitelist_cache = {}

    def tearDown(self):
        self.verify._WHITELIST_FILE = self._orig_file
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_global_whitelist_match(self):
        """加 global 白名单 → is_whitelisted 命中"""
        self.assertTrue(self.verify.add_whitelist(123456))
        self.assertTrue(self.verify.is_whitelisted(123456, 999))

    def test_by_group_whitelist_match(self):
        """加 by_group 白名单 → 仅该群命中"""
        self.assertTrue(self.verify.add_whitelist(111, group_id=888))
        self.assertTrue(self.verify.is_whitelisted(111, 888))
        self.assertFalse(self.verify.is_whitelisted(111, 999))

    def test_remove_whitelist(self):
        """移除后不再命中"""
        self.verify.add_whitelist(222)
        self.assertTrue(self.verify.is_whitelisted(222, 1))
        self.verify.remove_whitelist(222)
        self.assertFalse(self.verify.is_whitelisted(222, 1))

    def test_list_whitelist_format(self):
        """list_whitelist 返回正确 schema"""
        self.verify.add_whitelist(100)
        self.verify.add_whitelist(200, group_id=555)
        result = self.verify.list_whitelist()
        self.assertIn("global", result)
        self.assertIn("by_group", result)
        self.assertIn(100, result["global"])
        self.assertIn(200, result["by_group"].get("555", []))

    def test_persistence_after_reload(self):
        """写盘后重新加载，白名单仍生效"""
        self.verify.add_whitelist(333, group_id=777)
        # 模拟重启：清内存缓存
        self.verify._whitelist_cache = {}
        self.assertTrue(self.verify.is_whitelisted(333, 777))


class TestApprovePending(unittest.TestCase):

    def setUp(self):
        from bot import verify
        self.verify = verify
        self.verify._pending.clear()

    def tearDown(self):
        self.verify._pending.clear()

    def test_approve_pending_user_in_queue(self):
        """待验证中的用户被放行"""
        import time
        self.verify._pending[111] = {222: {
            "question": "1+1", "answer": "2", "user_name": "Newbie",
            "created_at": time.time(), "wrong_attempts": 0,
        }}
        bot = MagicMock()
        bot.send_group_msg = AsyncMock(return_value=1)
        ok = _run(self.verify.approve_pending(bot, 111, 222))
        self.assertTrue(ok)
        # _pending 已清理
        self.assertNotIn(222, self.verify._pending.get(111, {}))

    def test_approve_pending_user_not_in_queue(self):
        """不在待验证 → 返回 False"""
        bot = MagicMock()
        ok = _run(self.verify.approve_pending(bot, 999, 888))
        self.assertFalse(ok)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python tests/test_verify_whitelist.py`
Expected: AttributeError — `module 'bot.verify' has no attribute 'add_whitelist'`

- [ ] **Step 3: 实现 verify.py 白名单函数**

Append to `bot/verify.py` (在文件末尾，`get_verification_history` 之后):

```python


# ═══════════════════════════════════════════════════════════
#  免验证白名单 + 立即放行（Task 5 新增）
# ═══════════════════════════════════════════════════════════

import json as _json

# 白名单持久化文件（沿用 data/ 目录）
_WHITELIST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "verify_whitelist.json"
)

# 内存缓存: {"global": [int], "by_group": {str(group_id): [int]}}
_whitelist_cache: dict | None = None


def _load_whitelist() -> dict:
    """加载白名单（懒加载，损坏时重建）"""
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    default = {"global": [], "by_group": {}}
    if not os.path.exists(_WHITELIST_FILE):
        _whitelist_cache = default
        return _whitelist_cache
    try:
        with open(_WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            data = default
        data.setdefault("global", [])
        data.setdefault("by_group", {})
        _whitelist_cache = data
    except Exception as e:
        logger.warning(f"白名单文件损坏，重建: {e}")
        _whitelist_cache = default
        try:
            _save_whitelist()
        except Exception:
            pass
    return _whitelist_cache


def _save_whitelist():
    """写盘"""
    global _whitelist_cache
    if _whitelist_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_WHITELIST_FILE), exist_ok=True)
        with open(_WHITELIST_FILE, "w", encoding="utf-8") as f:
            _json.dump(_whitelist_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"白名单写入失败: {e}")


def is_whitelisted(user_id: int, group_id: int) -> bool:
    """检查是否在免验证白名单（global 或 by_group[group_id]）"""
    data = _load_whitelist()
    if user_id in data.get("global", []):
        return True
    return user_id in data.get("by_group", {}).get(str(group_id), [])


def add_whitelist(user_id: int, group_id: int = 0) -> bool:
    """
    加入白名单。group_id=0 表示全局；非 0 表示指定群。
    Returns: True 表示新增；False 表示已存在。
    """
    data = _load_whitelist()
    if group_id == 0:
        gl = data.setdefault("global", [])
        if user_id in gl:
            return False
        gl.append(user_id)
    else:
        key = str(group_id)
        lst = data.setdefault("by_group", {}).setdefault(key, [])
        if user_id in lst:
            return False
        lst.append(user_id)
    _save_whitelist()
    logger.info(f"免验证白名单 += {user_id} (group={group_id})")
    return True


def remove_whitelist(user_id: int, group_id: int = 0) -> bool:
    """从白名单移除。Returns: True 表示有移除；False 表示本来就不在。"""
    data = _load_whitelist()
    removed = False
    if group_id == 0:
        # 从 global 移除 + 所有 by_group 移除（彻底清）
        if user_id in data.get("global", []):
            data["global"].remove(user_id)
            removed = True
        for k, lst in data.get("by_group", {}).items():
            if user_id in lst:
                lst.remove(user_id)
                removed = True
    else:
        key = str(group_id)
        lst = data.get("by_group", {}).get(key, [])
        if user_id in lst:
            lst.remove(user_id)
            removed = True
    if removed:
        _save_whitelist()
        logger.info(f"免验证白名单 -= {user_id} (group={group_id})")
    return removed


def list_whitelist(group_id: int = 0) -> dict:
    """返回白名单字典。group_id 非 0 时附加该群的列表。"""
    data = _load_whitelist()
    result = {"global": list(data.get("global", [])),
              "by_group": dict(data.get("by_group", {}))}
    if group_id:
        result["current_group"] = list(data.get("by_group", {}).get(str(group_id), []))
    return result


async def approve_pending(bot, group_id: int, user_id: int) -> bool:
    """
    立即放行某个正在待验证的用户。
    清理 _pending，记录 manual_approved，发欢迎消息 + 加积分。
    Returns: True 表示该用户在待验证中并已被放行；False 表示没找到。
    """
    pending = _pending.get(group_id, {}).get(user_id)
    if not pending:
        return False
    user_name = pending.get("user_name", str(user_id))
    question = pending.get("question", "")
    _cleanup_user(group_id, user_id)
    _log_verification(user_id, user_name, group_id, "manual_approved",
                      question, "", "主人手动放行")
    logger.info(f"主人放行: {user_name}({user_id}) 群{group_id}")
    try:
        from .humanizer import send_human_like
        from . import welcome as _welcome
        await send_human_like(bot, group_id, f"✅ [CQ:at,qq={user_id}] 主人已放行，欢迎入群~ :96:")
        welcome_msg = _welcome.get_welcome_message(group_id, user_name, user_id)
        if welcome_msg:
            await send_human_like(bot, group_id, welcome_msg)
        _welcome.add_points(user_id, group_id, user_name, 10, "主人放行")
    except Exception as e:
        logger.warning(f"放行后发欢迎/加积分失败: {e}")
    return True
```

- [ ] **Step 4: 修改 verify.py 的 handle_member_increase 加白名单短路**

Modify `bot/verify.py` — 找到 `handle_member_increase` 函数（约 `verify.py:99`），在 `handle_member_increase` 内部、黑名单检查**之后**（即 `# 🧠 意识：新人来了` 之后、`# ── 群内验证 ──` 之前的位置——实际是 `handler.py` 里的 `handle_notice` 处理 `group_increase` 时调 `verify.handle_member_increase`），**在 `handle_member_increase` 函数的最开头**（紧接现有的 `if not group_id or not user_id or user_id == bot_qq: return` 之后），追加：

```python
    # 免验证白名单 → 直接放行（Task 5 新增）
    if is_whitelisted(user_id, group_id):
        _log_verification(user_id, str(user_id), group_id, "auto_whitelist",
                          "", "", "免验证白名单")
        logger.info(f"免验证白名单: {user_id} 群{group_id} → 直接放行")
        try:
            from .humanizer import send_human_like
            from . import welcome as _welcome
            welcome_msg = _welcome.get_welcome_message(group_id, str(user_id), user_id)
            if welcome_msg:
                await send_human_like(bot, group_id, welcome_msg)
            _welcome.add_points(user_id, group_id, str(user_id), 10, "免验证入群")
        except Exception as e:
            logger.debug(f"白名单放行发欢迎失败: {e}")
        return  # 不出题、不踢
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python tests/test_verify_whitelist.py`
Expected: all tests pass

- [ ] **Step 6: 在 handler.py 加 4 个主人命令分发**

Modify `bot/handler.py` — 找到主人命令区（约 `handler.py:1676` 的 `# --- 发布公告 ---` 之前），插入 4 个新命令：

```python
        # --- 同意入群（立即放行待验证者）---
        if cmd in ("同意入群", "approvejoin"):
            if not arg or not arg.strip().isdigit():
                await bot.send_group_msg(group_id, "用法: @我 同意入群 <QQ号>")
                return
            target_qq = int(arg.strip())
            from . import verify as _verify
            ok = await _verify.approve_pending(bot, group_id, target_qq)
            if ok:
                await bot.send_group_msg(group_id, f"✅ 已放行 {target_qq}")
            else:
                # 该 QQ 不在待验证 → 顺手加入白名单，下次入群免验证
                _verify.add_whitelist(target_qq, group_id)
                await bot.send_group_msg(
                    group_id,
                    f"ℹ️ {target_qq} 当前不在待验证列表，已加入本群免验证白名单，下次入群直接通过"
                )
            return

        # --- 免验证白名单 ---
        if cmd in ("免验证", "whitelist"):
            if not arg:
                await bot.send_group_msg(group_id, "用法: @我 免验证 <QQ号>  (或: 取消免验证 <QQ号> / 免验证列表)")
                return
            target_qq = int(arg.strip()) if arg.strip().isdigit() else 0
            if not target_qq:
                await bot.send_group_msg(group_id, "QQ号必须是数字")
                return
            from . import verify as _verify
            added = _verify.add_whitelist(target_qq, group_id)
            # 如果当前正在待验证 → 顺带放行
            await _verify.approve_pending(bot, group_id, target_qq)
            await bot.send_group_msg(
                group_id,
                f"✅ {target_qq} 已加入本群免验证白名单" + ("" if added else "（原本就在白名单）")
            )
            return

        if cmd in ("取消免验证", "unwhitelist"):
            if not arg or not arg.strip().isdigit():
                await bot.send_group_msg(group_id, "用法: @我 取消免验证 <QQ号>")
                return
            target_qq = int(arg.strip())
            from . import verify as _verify
            removed = _verify.remove_whitelist(target_qq, group_id)
            await bot.send_group_msg(
                group_id,
                f"✅ {target_qq} 已从免验证白名单移除" if removed
                else f"ℹ️ {target_qq} 本来就不在白名单"
            )
            return

        if cmd in ("免验证列表", "whitelistlist"):
            from . import verify as _verify
            data = _verify.list_whitelist(group_id)
            lines = ["📋 **免验证白名单**"]
            gl = data.get("global", [])
            lines.append(f"🌐 全局 ({len(gl)}): {', '.join(str(x) for x in gl) or '无'}")
            cg = data.get("current_group", [])
            lines.append(f"👥 本群 ({len(cg)}): {', '.join(str(x) for x in cg) or '无'}")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

```

- [ ] **Step 7: 跑现有 verify 相关代码确认无回归**

Run: `python -c "from bot import verify; print('OK')"`
Expected: 打印 `OK`（导入无异常）

- [ ] **Step 8: Commit**

```bash
git add bot/verify.py bot/handler.py tests/test_verify_whitelist.py
git commit -m "feat: 入群放行 — 免验证白名单 + 4 个主人命令

verify.py 新增 is_whitelisted/add_whitelist/remove_whitelist/list_whitelist/approve_pending
handle_member_increase 入口加白名单短路（不出题直接欢迎）
handler.py 新增: 同意入群/免验证/取消免验证/免验证列表
持久化 data/verify_whitelist.json (global + by_group)"
```

---

## Task 6: 依赖、配置、文档

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: 加依赖**

Modify `requirements.txt` — 在末尾追加：

```
pytesseract>=0.3.10
Pillow>=10.0.0
```

- [ ] **Step 2: 加环境变量样例**

Modify `.env.example` — 在 `# --- 审核 ---` 区块追加（`LLM_MOD_ENABLED=true` 之后）：

```
# 图片审核 Vision 失败时降级到 OCR（需要系统安装 Tesseract）
IMAGE_OCR_FALLBACK=true
```

- [ ] **Step 3: 更新 README**

Modify `README.md` — 找到 "### 主人命令" 表格（约 `README.md:140`），在表格内追加 4 行：

```markdown
| `@Bot 同意入群 <QQ号>` | 立即放行该 QQ 在本群的待验证状态（若不在待验证则加入免验证白名单） |
| `@Bot 免验证 <QQ号>` | 加入本群免验证白名单（若当前在待验证则顺带放行） |
| `@Bot 取消免验证 <QQ号>` | 从免验证白名单移除 |
| `@Bot 免验证列表` | 查看全局 + 本群免验证白名单 |
```

在 "## 常见问题" 区块之前，加一节：

```markdown
---

## 图片审核与 OCR 降级

机器人会对群里的**所有图片、GIF 动图、商城表情**自动做违规识别：

1. **Vision API 直接审核**（主路径，需配置 `LLM_API_KEY`）
2. **OCR 文字识别**（Vision 失败时降级，需安装 Tesseract）
3. **文字结果送 LLM 审核**（OCR 拿到文字后再判断）

### 安装 Tesseract（可选，但推荐）

Vision API 故障时依赖 Tesseract 做 OCR 降级。

- **Windows**: 下载 [Tesseract-OCR](https://github.com/UB-Mannheim/tesseract/wiki)，安装时勾选 **Chinese (Simplified)** 语言包
- 默认检测路径：`E:\ocr\tesseract.exe`、`C:\Program Files\Tesseract-OCR\`、`C:\Program Files (x86)\Tesseract-OCR\`
- **Linux**: `sudo apt install tesseract-ocr tesseract-ocr-chi-sim`

未安装 Tesseract 时，Vision 失败的图片会被跳过（不制造误判），但不影响文本审核。

---
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt .env.example README.md
git commit -m "chore: 加 Pillow/pytesseract 依赖 + OCR 配置 + README 命令说明"
```

---

## 验收清单（全部完成后核对）

实现完所有 6 个任务后，对照 spec 的 14 项验收标准逐条核对：

- [ ] 1. 合并转发含广告文本 → 自动撤回（Task 1+3）
- [ ] 2. 合并转发含广告图片 → 自动撤回（Task 1+2+3）
- [ ] 3. 纯广告图片 → 自动撤回（Task 2+3）
- [ ] 4. Vision 故障 → OCR → 文本审核撤回（Task 2）
- [ ] 5. 多图消息全审（Task 2+3）
- [ ] 6. GIF 中间帧违规被命中（Task 2）
- [ ] 7. mface 商城表情被审（Task 1+3）
- [ ] 8. 手动标记合并转发 → 关键词非空（Task 1+4）
- [ ] 9. 手动标记图片 → 关键词含类型（Task 2+4）
- [ ] 10. 手动标记多图 → 所有 OCR 并入（Task 2+4）
- [ ] 11. 免验证白名单入群不出题（Task 5）
- [ ] 12. 同意入群立即放行（Task 5）
- [ ] 13. 免验证列表正确显示（Task 5）
- [ ] 14. 所有新功能失败时不影响主消息流（所有 Task 的 try/except）

**全量回归测试**:
```bash
python tests/test_content_reader.py
python tests/test_image_fallback.py
python tests/test_handler_moderation.py
python tests/test_violation_image.py
python tests/test_verify_whitelist.py
python tests/test_group_events.py   # 既有
python tests/test_timeutil.py       # 既有
```
全部应 `OK`。
