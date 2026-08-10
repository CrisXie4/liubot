# 多内容审核与入群放行设计

**日期**: 2026-08-10
**状态**: 已批准（待写实现计划）
**作者**: brainstorming 流程产出

## 1. 问题陈述

当前 QQ bot 存在三个生产问题：

### 1.1 合并转发消息无法识别（高优先级）

群成员把消息用"合并转发"功能打包发出时，OneBot 协议下发的是 `[CQ:forward,id=<resId>]`，机器人拿到的是空的 CQ 码，**内部真实内容完全不可见**。后果：

- 自动审核管道对转发包放行（无文本信号）
- 主人手动 `@Bot 违规` 标记时，`extract_keywords` 在空文本上提不到任何词
- 违规者可用"合并转发"绕过所有审核

### 1.2 违规图片无法识别（高优先级）

GLM-5.2 模型支持 Vision 图片输入，代码里 `image_mod.analyze_image_message` 已写好多模态审核能力，但**从未被自动审核管道调用**。两个断裂点：

- **自动审核**（`handler.py`）：只把 `raw_message` 纯文本喂给 `automod` 和 `llm_mod`，对 `[CQ:image,...]` 消息剥 CQ 码后文本为空，置信度恒为 0 → 全部放行。
- **手动标记**（`violation.py`）：判断图片分支的条件 `raw_message == "[图片]"` 永远不匹配实际的 `[CQ:image,file=...,url=...]`，分支进不去。即使进了，也只取 `ocr_text` 丢弃了 Vision 已经做出的 `is_violation / confidence / violation_type` 判定。

### 1.3 入群放行能力缺失（中优先级）

当前加群验证流程（`verify.py`）：新人入群 → AI 出题 → 答错/超时即踢。主人无法：

- 主动放行某个"正在待验证"的新人（卡在验证环节进不来）
- 预先把某 QQ 加白名单，下次入群直接跳过出题

---

## 2. 设计目标

1. **统一内容提取**：自动审核和手动标记两条路径共享同一份内容提取逻辑，消除"不对称 bug"。
2. **并行审核**：文本审核与图片 Vision 审核 `asyncio.gather` 并行执行，延迟最优。
3. **优雅降级**：图片 Vision 失败 → OCR 提取文字 → 文本 LLM 审核，三级降级链。
4. **主人放行**：新增专用命令放行待验证者与维护免验证白名单。
5. **零破坏**：所有新调用包 try/except，绝不崩主消息流；现有审核管道签名不变。

---

## 3. 架构总览

```
群消息事件
    │
    ▼
┌─────────────────────────────────────────┐
│ content_reader.extract_full_content()   │  ← 新增（统一内容提取器）
│   - 解析 [CQ:forward,id=xxx]            │
│   - 解析 [CQ:image,...]                 │
│   - 返回 {text, images, forward, ...}   │
└─────────────────────────────────────────┘
    │
    ├──────────────── 自动审核路径 ────────────────┐
    │                                              ▼
    │  ┌──────────────────────────────────────────┐
    │  │ asyncio.gather(                          │
    │  │   llm_mod.llm_moderate(effective_text),  │  ← 已有
    │  │   image_mod.review_image_with_ai(...),   │  ← 已有，现在被调用
    │  │ )                                       │
    │  │ → 融合判定                               │
    │  └──────────────────────────────────────────┘
    │
    └──────────────── 手动标记路径 ────────────────┐
                                                   ▼
        violation.handle_violation():
          - 用 extract_full_content 拿真实内容
          - 图片走 review_image_with_ai（保留 is_violation 等结构化字段）
          - Vision 失败 → OCR 提文字 → 喂 llm_moderate
          - 提关键词存库
```

---

## 4. 详细设计

### 4.1 新增：统一内容提取器 `bot/content_reader.py`

**职责**：把任意 OneBot 消息（纯文本 / 图片 / 合并转发 / 混合）统一解析成结构化 payload。

**接口**：
```python
async def extract_full_content(
    bot: OneBotClient,
    raw_message: str,
    segments: list[dict],
) -> dict:
    """
    返回:
    {
      "text": str,                                  # 本条消息可见文本
      "images": [{"url": str, "file_id": str}],     # 本条消息直带的图片（含 GIF）
      "stickers": [{"url": str, "summary": str}],   # 本条消息的 mface 商城表情
      "has_forward": bool,
      "forward": {                                  # 仅当存在合并转发
        "text": str,                                # 所有子消息文本拍平拼接
        "images": [{"url": str}],                   # 所有子消息里的图片（含 GIF）
        "stickers": [{"url": str, "summary": str}], # 所有子消息里的 mface
        "count": int,                               # 子消息条数
        "nested": bool,                             # 是否含嵌套转发
      } | None,
      "errors": [str],                              # 解析过程中的非致命错误
    }
    """
```

**注意 — 不审核的内容类型**：
- `face`（QQ 基础小表情如 😊，只有 id 无 url，不可能含广告）→ 提取器跳过。
- `record`（语音）→ 本期不审（API 不支持音频，未来可考虑 ASR）。
- `video`（视频）→ 沿用现有 `analyze_video` 走缩略图审核，不在本期扩展。

**转发解析逻辑**：
1. 用正则识别 `[CQ:forward,id=<resId>]`，可能多条。
2. 对每个 resId 调 `bot.get_forward_msg(id=resId)`（`onebot_client.py:465`，目前**未被任何代码调用**）。
3. NapCat 返回 `{"messages": [{message:[segments], nickname, user_id, time, ...}, ...]}`，遍历每个子消息提取 text/image。
4. **嵌套转发**：子消息里若再含 `[CQ:forward,...]`，递归展开；**深度上限 3 层**，超出时记入 `errors` 并停止递归。
5. **子消息条数上限 50**（防超大转发包拖垮 API），超出部分忽略并在 `errors` 标注。
6. `get_forward_msg` 失败（resId 过期、NapCat 异常）→ `forward.text="[合并转发-无法解析]"`，错误进 `errors`。

**缓存**：
- 转发包按 resId 缓存解析结果（同一 resId 5 分钟内不重复请求 NapCat）。
- 图片 URL 复用现有 `media_reader._media_cache`（按 URL 缓存 Vision 结果，已存在）。

**幂等性**：
- 提取器只做读取，不修改任何状态。同一个 `(raw_message, segments)` 多次调用返回一致结果（缓存内）。

### 4.2 自动审核接入（`bot/handler.py`）

在 `handle_group_message`（`handler.py:855`）现有 `raw_message` 拿到之后，**替换**当前的 `media_ctx = await media_reader.extract_media_context(raw_message)` 调用：

```python
# 新增：统一内容提取（替代旧的 media_reader.extract_media_context 调用）
content = await content_reader.extract_full_content(bot, raw_message, message)

# 构造"等效文本"喂给现有文本审核管道（不改 automod/llm_mod 签名）
effective_text = content["text"]
if content["has_forward"]:
    fwd = content["forward"]
    effective_text += "\n[合并转发]\n" + fwd["text"]

# 并行跑：文本审核 + 全量图片/表情/GIF 审核
# 收集所有待审媒体：本条消息的 + 合并转发包内的
all_images = list(content["images"])                       # 普通 image（含 GIF）
all_stickers = list(content["stickers"])                   # mface 商城表情
if content["has_forward"]:
    all_images += content["forward"]["images"]
    all_stickers += content["forward"]["stickers"]

# mface（商城表情）按图片处理（带 summary 作为 caption）
for s in all_stickers:
    if s.get("url"):
        all_images.append({
            "url": s["url"], "file_id": "",
            "caption_hint": s.get("summary", ""),  # 表情自带的文字标签
            "is_sticker": True,
        })

# 排除主人和官方账号
owner_or_official = _is_owner(user_id) or _is_official_account(user_id)

text_task = llm_mod.llm_moderate(
    effective_text, group_id=group_id, user_id=user_id,
    force_llm=should_recall,
)
image_task = _audit_all_media(all_images) if (all_images and not owner_or_official) else _no_media()
text_result, image_results = await asyncio.gather(
    text_task, image_task, return_exceptions=True
)
```

**`_audit_all_media` 行为（新增辅助函数，定义在 `content_reader.py` 或 `image_mod.py`）**：
- 把所有图片当作独立审核单元，**全部都审**（无"只审第一张"的偷工）。
- 用 `asyncio.Semaphore(5)` 控并发，避免一条消息 10 张图打爆 API。
- 单条消息媒体单元硬上限 20（极端刷屏保护）。超出按出现顺序取前 20 个，记日志。
- 每个单元返回 `Optional[dict]`；最终过滤 `None`，得 `list[dict]`（可能为空）。
- GIF 在 `review_image_with_fallback` 内部拆关键帧（见 4.3.GIF），对调用方透明。
- 每个媒体结果独立缓存（按 URL），同一张广告图 24h 内不重审。

```python
# 图片 Vision 结果融合（image_results 是 list[dict]）
if isinstance(image_results, list):
    for image_result in image_results:
        if not image_result:
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
                ocr_keywords = violation.extract_keywords(ocr_text, top_n=5)
                combined_hits = list(set(combined_hits + img_keywords + ocr_keywords))
            break  # 任一媒体判违规即整体撤回，不再叠加
```

**撤回策略**：
- 合并转发是**一条消息对应一个 message_id**。检测到转发包内任何子消息违规 → 整体撤回这条转发消息（QQ 协议不支持选择性撤回子消息）。
- 多图消息：一张违规即整体撤回（同一 message_id 下的所有图）。

### 4.3 图片审核失败三级降级（`bot/image_mod.py`）

新增包装函数 `review_image_with_fallback`：

```python
async def review_image_with_fallback(
    bot, file_id: str, image_url: str = "", caption: str = ""
) -> Optional[dict]:
    """
    三级降级:
      1. Vision API 直接审核 (review_image_with_ai，已有)
         ↓ 失败/超时/无 api_key
      2. OCR 提取文字 (Tesseract)
         ↓ 拿到 ocr_text
      3. 把 ocr_text 喂 GLM-5.2 文本审核 (llm_mod.llm_moderate)
    任何一级成功即返回；全部失败返回 None。
    """
```

**降级触发条件**：
- 第 1 级返回 `None`（无 api_key 或网络异常）
- 第 1 级抛 `APIConnectionError` / `APITimeoutError`
- 第 1 级返回的 JSON 解析失败

**第 2 级 OCR**：
- Tesseract 安装路径检测沿用 `image_mod.py:354` 的 `check_ocr()`（需把已弃用的函数重新启用，路径优先级：`E:\ocr\tesseract.exe` > `C:\Program Files\Tesseract-OCR\` > `C:\Program Files (x86)\Tesseract-OCR\`）。
- 图片通过 URL 下载（`aiohttp`，已有）或 NapCat `get_image` API 拿 base64，PIL 打开后 `pytesseract.image_to_string`，语言 `chi_sim+eng`。
- OCR 也失败 → 返回 `None`，记日志 "该图无法分析"。

**第 3 级文本审核**：
- 把第 2 级拿到的 `ocr_text` 喂给 `llm_mod.llm_moderate(ocr_text, ...)`。
- 把 `llm_moderate` 的结果改造成和第 1 级同 schema：`{is_violation, confidence, violation_type, ocr_text, reason, keywords_matched}`。

**集成**：4.2 节的 `image_task` 改调 `review_image_with_fallback` 而非 `review_image_with_ai`。

**GIF 动图关键帧提取（新增）**：

OneBot 把 GIF 当 `[CQ:image,file=xxx.gif,url=...]` 下发，无法从 CQ 码区分静态图与动图。`review_image_with_fallback` 在下载图片后用 PIL 探测：

```python
from PIL import Image
import io

async def _extract_gif_frames(raw_bytes: bytes) -> list[bytes]:
    """GIF → 多帧 PNG bytes。非 GIF 返回 [raw_bytes]。
    帧数策略：≤3 帧全提；>3 帧取首帧 + 中间帧 + 末帧（覆盖率高、API 调用克制）。
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        n = getattr(img, "n_frames", 1)
        if n <= 1:
            return [raw_bytes]  # 静态图
        picks = [0] if n <= 3 else [0, n // 2, n - 1]
        frames = []
        for idx in picks:
            img.seek(idx)
            png = io.BytesIO()
            img.convert("RGB").save(png, format="PNG")
            frames.append(png.getvalue())
        return frames
    except Exception:
        return [raw_bytes]
```

**GIF 审核策略**：
- 每帧独立送 Vision；任一帧判违规 → 整张 GIF 判违规（取最高 confidence）。
- 多帧结果按 OR 聚合（`is_violation` 任一 True 即 True，`ocr_text` 拼接，`reason` 取违规帧的）。
- 关键帧 ≤ 3，避免一个 50 帧 GIF 烧 50 次 API。
- 探测失败（PIL 无法解码）→ 降级走静态图路径（直接送原始 bytes 给 Vision）。

**mface（商城表情）审核**：
- `mface` 的 `url` 指向表情图（PNG/WebP），直接当图片审。
- `summary`（表情文字标签，如 "[委屈]"）作为 caption 喂给 Vision，帮助识别"文字+图"组合的广告。

**24h 缓存**：
- 新增 `IMAGE_MOD_CACHE_PATH = data/image_mod_cache.json`，key = image_url，value = `{result, analyzed_at, source}`。
- TTL 24 小时，避免对同一张被反复传播的广告图重复调 Vision。
- 与 `media_reader._media_cache` 解耦：那个缓存"描述"（用于 AI 聊天上下文），这个缓存"审核结果"（用于违规判定），语义不同。

### 4.4 手动标记修复（`bot/violation.py`）

替换 `handle_violation`（`violation.py:217`）里失效的图片分支（`violation.py:256-272`）：

```python
# --- 统一内容提取（新）---
content = await content_reader.extract_full_content(bot, raw_message, message_segments)

# 组装用于存库/提关键词的 effective_text
effective_text = content["text"]
if content["has_forward"]:
    effective_text += "\n[合并转发]\n" + content["forward"]["text"]

# --- 图片+转发图+表情：走 Vision（三级降级），全审 ---
all_images = list(content["images"])
all_stickers = list(content["stickers"])
if content["has_forward"]:
    all_images += content["forward"]["images"]
    all_stickers += content["forward"]["stickers"]
for s in all_stickers:
    if s.get("url"):
        all_images.append({"url": s["url"], "file_id": "",
                           "caption_hint": s.get("summary", "")})

if all_images:
    # 复用自动审核路径的 _audit_all_media（同 semaphore + 同缓存）
    from .image_mod import _audit_all_media
    vision_results = await _audit_all_media(all_images) or []
    for vr in vision_results:
        if not vr:
            continue
        ocr = vr.get("ocr_text", "")
        if ocr:
            effective_text += "\n[图片内容] " + ocr

# --- 分析特征 ---
analysis = analyze_violation_message(effective_text)

# 把 Vision 违规类型并入 keywords
for vr in vision_results:
    if vr.get("violation_type") not in ("none", None):
        analysis["keywords"] = list(set(
            analysis["keywords"] + [vr.get("violation_type", "")]
        ))[:10]
```

后续 `db.add_violation` / `learner.learn_from_violation` / `bot.delete_msg` 逻辑保持不变，但这次它们拿到的是**真正有内容的 `effective_text`**，"手动标记也没有提取到任何关键词"的问题消失。

### 4.5 入群放行：命令与白名单

**命令设计**（避开与现有 `同意 <审批ID>` 的歧义；审批 ID 是 8 位字母数字，QQ 号是纯数字，但仍走专用命令更直观）：

| 命令 | 范围 | 作用 |
|------|------|------|
| `@bot 同意入群 <QQ号>` | 本群当前待验证 | 立刻清理该 QQ 在本群的 `_pending` 记录，标记 `manual_approved`，发欢迎消息。若不在待验证列表，提示"未找到该 QQ 的待验证记录" |
| `@bot 免验证 <QQ号>` | 持久化白名单 | 加入免验证白名单。若该 QQ 当前正在本群待验证，**顺带立即放行**（覆盖"已在群但过不了认证"场景） |
| `@bot 取消免验证 <QQ号>` | 持久化白名单 | 从白名单移除 |
| `@bot 免验证列表` | 查询 | 列出全局白名单 + 本群白名单 |

**实现位置**：
- 命令分发：`handler.py` 现有的主人命令区（`handler.py:1600+` 那一片 `if cmd in (...)` 链）。
- 业务函数：`bot/verify.py` 新增：
  ```python
  def is_whitelisted(user_id: int, group_id: int) -> bool
  def add_whitelist(user_id: int, group_id: int = 0) -> bool  # group_id=0 表示全局
  def remove_whitelist(user_id: int, group_id: int = 0) -> bool
  def list_whitelist(group_id: int = 0) -> dict
  def approve_pending(bot, group_id: int, user_id: int) -> bool
      # 立即放行：清理 _pending[group_id][user_id]，取消 _timeout_kick 任务（通过 _cleanup_user），
      # 记录 _log_verification(..., "manual_approved", ...)，
      # 发送欢迎消息 + 加积分（与验证通过路径一致）。
      # 返回 True 表示该 QQ 确实处于待验证状态并已被放行；False 表示没找到记录。
  ```

**白名单持久化**：`data/verify_whitelist.json`
```json
{
  "global": [123456, 789012],
  "by_group": {
    "987654321": [111111, 222222]
  }
}
```
- `is_whitelisted` 同时查 `global` 和 `by_group[group_id]`，命中任一即放行。
- 写入时加文件锁（沿用现有 `_log_verification` 的简单写法，无需引入新依赖）。

**集成进现有流程**：

`verify.py:99` `handle_member_increase` 入口加在最前面（黑名单检查之后）：
```python
# 黑名单 → 踢（已有）
# 免验证白名单 → 直接放行 + 发欢迎（新）
if is_whitelisted(user_id, group_id):
    _log_verification(user_id, user_name, group_id, "auto_whitelist", "", "", "免验证白名单")
    logger.info(f"免验证白名单: {user_name}({user_id}) 群{group_id} → 直接放行")
    from .humanizer import send_human_like
    from . import welcome as _welcome
    try:
        welcome_msg = _welcome.get_welcome_message(group_id, user_name, user_id)
        if welcome_msg:
            await send_human_like(bot, group_id, welcome_msg)
    except Exception:
        pass
    try:
        _welcome.add_points(user_id, group_id, user_name, 10, "免验证入群")
    except Exception:
        pass
    return  # 不出题、不踢
# 其他 → AI 出题（已有）
```

### 4.6 失败安全与边界

所有新增调用严格遵循现有项目的容错风格：
- 每个新增 `await` 调用包在 try/except 里，失败时降级而非崩溃。
- `extract_full_content` 内部任何子步骤失败 → 把错误进 `errors` 列表，返回的部分结果仍可用。
- Vision/OCR 全失败 → 不制造误判（让文本审核结果生效），图片维度记为"未检测"。
- 转发 resId 过期 → 友好降级，文本审核管道仍能跑（虽然文本是占位符）。
- 新增命令只能由 `OWNER_QQ` 调用（沿用现有 `_is_owner` 判断）。
- 白名单写文件失败 → 日志告警，内存中仍生效（下次启动丢失）。

---

## 5. 数据与配置变更

### 5.1 新增文件
- `bot/content_reader.py` — 统一内容提取器
- `data/verify_whitelist.json` — 免验证白名单（运行时自动创建）
- `data/image_mod_cache.json` — 图片审核结果缓存（运行时自动创建）

### 5.2 修改文件
- `bot/handler.py` — `handle_group_message` 接入新提取器与并行审核；新增 4 个主人命令分发
- `bot/violation.py` — `handle_violation` 替换图片分支为统一提取器
- `bot/verify.py` — `handle_member_increase` 加白名单检查；新增 5 个白名单/放行业务函数
- `bot/image_mod.py` — 启用 Tesseract OCR；新增 `review_image_with_fallback` 三级降级包装；新增 24h 缓存
- `bot/__init__.py` — 注册 `content_reader` 模块
- `requirements.txt` — 加 `pytesseract>=0.3.10`、`Pillow>=10.0.0`
- `.env.example` — 加 `IMAGE_OCR_FALLBACK=true`
- `README.md` — 新增命令说明 + Tesseract 安装链接 + 新功能介绍

### 5.3 依赖
- **Python**: `pytesseract>=0.3.10`、`Pillow>=10.0.0`
- **系统**: Tesseract OCR 二进制（Windows: [Tesseract-OCR](https://github.com/UB-Mannheim/tesseract/wiki)），需含 `chi_sim` 中文语言包。默认检测路径覆盖 `E:\ocr\` 和两个 `Program Files` 目录。

---

## 6. 测试策略

`tests/` 目录基本为空，本次用 `pytest` + `unittest.mock` 新建以下单元测试：

| 测试文件 | 覆盖点 |
|---------|--------|
| `tests/test_content_reader.py` | 1. 从 `[CQ:forward,id=xxx]` 能拿到子消息（mock `bot.get_forward_msg`）<br>2. 嵌套 forward 深度上限 3（构造 5 层嵌套的 mock 数据，验证到第 3 层停止）<br>3. 子消息条数上限 50（mock 60 条子消息）<br>4. resId 过期时返回 `errors` 且 `forward.text` 含占位符<br>5. 图片 URL 提取正确<br>6. 多 forward 包混合文本的正确拼接<br>7. mface 表情被归入 `stickers`（含 url + summary）<br>8. `face` 类型被跳过（不出现在 images/stickers） |
| `tests/test_verify_whitelist.py` | 1. `is_whitelisted` global / by_group 两层匹配<br>2. `add_whitelist(group_id=0)` 写 global<br>3. `add_whitelist(group_id=N)` 写 by_group<br>4. `remove_whitelist` 后查询返回 False<br>5. 持久化文件格式符合 schema |
| `tests/test_image_fallback.py` | 1. Vision 成功 → 直接返回，不调 OCR<br>2. Vision 抛超时 → 触发 OCR（mock）<br>3. OCR 拿到文字 → 触发 llm_moderate<br>4. 全失败 → 返回 None<br>5. 缓存命中 → 不重复调 API<br>6. **GIF 5 帧时只抽 3 帧**（首/中/末）；**GIF 2 帧时全抽**<br>7. **`_audit_all_media` 11 张图时并发上限 5**（用 mock 验证同时 in-flight ≤ 5）<br>8. **`_audit_all_media` 25 张图时截到前 20**（防刷屏保护） |
| `tests/test_handler_integration.py` | 1. 图片消息走并行审核路径（gather 被调用）<br>2. **多图消息：所有图都被审**（mock 验证 review_image_with_fallback 被调 N 次）<br>3. **任一图判违规 → `final_violation` 被覆盖为 True**<br>4. **mface 消息：url 被当作图片送审**<br>5. 转发消息 effective_text 含子消息文本 |

**手动验证脚本**：`scripts/smoke_test.py`（新增，可选）— 用真实事件样本（脱敏后的 raw_message + segments）跑一遍 `extract_full_content`，打印输出供人工核对。

**不做的测试**：
- 不跑真实 NapCat / 真实 Vision API（单元测试只用 mock）
- 不做 Web 控制面板 E2E（本次范围外）

---

## 7. 实现顺序建议

按依赖关系分阶段（每阶段可独立验证）：

1. **阶段 1：基础提取器** — `content_reader.py` + 测试。无外部依赖，可立即验证。
2. **阶段 2：图片审核降级** — `image_mod.review_image_with_fallback` + Tesseract 集成 + 测试。
3. **阶段 3：自动审核接入** — `handler.py` 改造（依赖阶段 1、2）。
4. **阶段 4：手动标记修复** — `violation.py` 改造（依赖阶段 1、2）。
5. **阶段 5：入群放行** — `verify.py` 白名单 + `handler.py` 命令分发（独立于其他阶段）。
6. **阶段 6：依赖与文档** — requirements / env / README。

---

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| GLM-5.2 的 Anthropic 端点不支持图片输入透传 | 中 | 高 | 阶段 2 第一步先用一张测试图验证；若不行则在该处切到 GLM 原生图片格式，不影响整体架构 |
| Tesseract 在 Windows 找不到中文语言包 | 中 | 中 | 检测时同时查 `chi_sim.traineddata`；缺失时降级用英文 OCR + 记日志告警 |
| 合并转发 resId 过期导致大量"无法解析" | 低 | 中 | 5 分钟缓存 + 错误日志；过期属正常情况，不阻塞主流程 |
| **多图消息审 N 张导致延迟堆积** | 中 | 中 | `Semaphore(5)` 并发；24h URL 缓存去重；单条消息媒体上限 20；GIF 仅取 ≤3 关键帧；并行 `gather` 与文本审核同时跑 |
| **GIF 关键帧漏判（违规只在中间某帧）** | 低 | 中 | 取首+中+末三帧覆盖绝大多数情况；命中即整体违规，不再细化 |
| **mface url 失效或防盗链** | 中 | 低 | 下载失败 → 记日志降级；不阻塞其他图审核；不影响文本审核 |
| 现有"同意 <审批ID>"命令被误用为"同意入群" | 低 | 低 | 命令分叉：`同意 <ID>` 走 guardian 审批，`同意入群 <QQ>` 走放行；文档明确区分 |
| 白名单 JSON 文件损坏 | 低 | 低 | 读取时 try/except，损坏时重建空文件 + 日志告警 |

---

## 9. 非目标（本次不做）

- 不重写 `automod` 关键词引擎签名（保持兼容）
- 不引入新的向量数据库或复杂存储（沿用 JSON 文件 + SQLite）
- 不做 Web 控制面板 UI 改造（白名单管理暂只走 QQ 命令，未来可加 Web 入口）
- 不做图片视频流分析（视频仍走缩略图，已有）
- 不改变加群验证的出题/判题逻辑（只在入口加白名单短路）

---

## 10. 验收标准

实现完成后，以下场景必须全部通过：

1. 群里发合并转发包含广告文本 → 自动撤回 + 撤回公告含关键词
2. 群里发合并转发包含广告图片 → 自动撤回（图片走 Vision）
3. 群里发广告图片（纯图无文字）→ 自动撤回
4. Vision API 故障时发广告图片 → OCR 拿到文字 → 文本审核撤回
5. **群里发一条消息含多张广告图（如 5 张）→ 5 张都被审，至少一张命中即撤回**（不能只审第一张）
6. **群里发 GIF 动图广告（违规内容在中间帧）→ 首中末关键帧抽帧审核能命中**
7. **群里发广告类 mface 商城表情 → url 当图片送审，命中即撤回**
8. 手动 `@bot 违规` 回复一条合并转发 → 特征词列表非空，存库成功
9. 手动 `@bot 违规` 回复一条广告图片 → 特征词含图片识别的类型，存库成功
10. **手动 `@bot 违规` 回复一条多图消息 → 所有图的 OCR/识别文字并入存库内容**
11. `@bot 免验证 <QQ号>` → 该 QQ 重新入群时不被出题，直接欢迎
12. 新人正在待验证时 `@bot 同意入群 <QQ号>` → 立即放行，不再触发超时踢人
13. `@bot 免验证列表` → 正确显示全局 + 本群白名单
14. 所有新增功能失败时不影响主消息流（已有聊天/审核命令照常工作）

---

## 附录 A：现有代码索引

| 文件 | 关键位置 | 说明 |
|------|---------|------|
| `bot/onebot_client.py:465` | `get_forward_msg` | API 已实现但未被调用 |
| `bot/handler.py:855` | `handle_group_message` | 自动审核主入口 |
| `bot/handler.py:918` | `media_ctx = await media_reader.extract_media_context(...)` | 将被 content_reader 替换 |
| `bot/handler.py:1620-1673` | 主人命令同意/拒绝区 | 命令分发模板 |
| `bot/violation.py:217` | `handle_violation` | 手动标记主入口 |
| `bot/violation.py:256-272` | 失效的图片分支 | 将被替换 |
| `bot/image_mod.py:148` | `review_image_with_ai` | Vision 审核（已存在，未接入） |
| `bot/image_mod.py:275` | `analyze_image_message` | 高层包装（未接入自动审核） |
| `bot/image_mod.py:345-366` | `check_ocr` | 已弃用的 OCR 检测，将重新启用 |
| `bot/verify.py:99` | `handle_member_increase` | 入群事件入口 |
| `bot/verify.py:154` | `_pending` 字典 | 待验证记录，放行时清理 |
| `bot/llm_mod.py:245` | `llm_moderate` | 文本 LLM 审核（OCR 降级会调它） |
| `bot/llm_call.py:77` | `llm_create` | 统一 LLM 调用 + 429 重试 |

---

## 附录 B：术语表

- **OneBot v11**: QQ bot 协议规范，NapCatQQ 是其实现
- **CQ 码**: OneBot 文本协议中 `[CQ:type,key=value]` 格式的消息段
- **合并转发**: QQ 的"聊天记录"功能，把多条消息打包成一条 `[CQ:forward,id=<resId>]` 消息
- **resId**: NapCat 给合并转发包分配的资源 ID，用于 `get_forward_msg` 反查内容
- **Vision**: 多模态 LLM 的图片理解能力
- **OCR**: 光学字符识别，从图片提取文字
