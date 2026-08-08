# QQ Bot 拟人化优化 — 设计文档

**日期**: 2026-08-07
**状态**: 已批准，实施中
**模型**: glm-5.2-free @ https://api.ccode.vip （Anthropic 格式，已验证）

## 背景

Bot 存在三类"人机感"问题：(1) 必须被 @ 才回；(2) 聊着聊着就忘；(3) 对新闻等群体内容没有情绪反应。代码审计后又发现一个**隐藏的根因**：群级人设自动学习模块在持续向系统提示词投毒，导致身份错乱（自称"丛雨丸"而非"小六"）和极易被群友误导。

## 根因分析

### 根因 A：人设投毒（最高优先级，此前未暴露）
- `bot/group_personality.py:244-327` `auto_learn_group_style` 的 LLM 分支把群聊消息喂给 LLM 提取"群口头禅/群氛围"
- LLM 把群友的挑逗话（`丛雨丸是你妹妹`、`光屁股追老母鸡`、`你二爷`、`连你二爷都不认了？`）当成"群专属习惯"返回
- `quirks` 只增不减（315 行切片 bug），列表膨胀到几百条垃圾
- `bot_name` 也被群聊污染（319-320 行）—— 群里叫"丛雨丸"，就被写死成 bot_name
- 这些垃圾通过 `build_group_system_prompt` 回灌成"本群专属习惯"，覆盖了 custom_prompt 的防误导指令
- **结果**：Bot 等于被群友远程改写人格。这就是"自称丛雨丸 + 被骗喝酒和路灯比武"的真正源头

### 根因 B：不 @ 就基本不回
- `decision_engine.py:148` 默认主动回复概率 12%，加全局冷却 + 8% 已读不回
- followup 窗口太短/太脆，单用户跟踪，多说几句没 @ 就断

### 根因 C：聊着忘
- followup 断了之后，上下文压缩（COMPRESS_THRESHOLD=120）把旧消息压成摘要，细节丢失
- 长期记忆检索靠关键词命中，不一定捞到相关条目

### 根因 D：情绪系统是"自恋"的
- `consciousness.py` 只对"针对 Bot 的事件"反应：被夸/被骂/被@/被忽视
- `_detect_sentiment_and_react` 只在被 @ 时跑，且只看夸/骂关键词
- **没有任何机制**让 Bot "读到悲伤消息→自己变难过"。这就是"对新闻没情绪"的源头

### 根因 E：429 无重试
- 所有 `client.messages.create` 裸调，429 直接抛异常丢弃回复
- 用户要求："如果提示429，先存着，过一会儿再发送"

## 实施方案

### 阶段 0：止血 — 修复人设投毒（最高优先级）

**改动文件**: `bot/group_personality.py`, `data/group_personalities.json`

1. **bot_name 永不自动学习**：身份只来自 `personality.json`（全局权威）。代码移除 `auto_learn_group_style` 里 319-320 行设置 bot_name 的逻辑；群级 `bot_name` 为空时回退到全局 name
2. **禁用 LLM 版 quirk 自动学习**：`auto_learn_group_style` 的 LLM 分支整体废弃，只保留 `_rule_based_learn`（纯语气词检测：awa/www/草/hhhh/绷不住了）。理由：让 Bot 从正在调戏它的人群里学口头禅，本质是开了个被远程改写人格的后门
3. **清理脏数据**：清空群 1084012604 的 quirks；移除其 bot_name 字段（回退到全局"小六"）
4. **防御性过滤**：即使规则版 quirk，也过滤含身份词/关系词（妹/弟/爷/叔/爸/分身/哥/姐）的条目；quirks 上限 8 条，FIFO 淘汰

### 阶段 1：健壮性 — 429 重试 + 延迟补发

**改动文件**: 新建 `bot/llm_call.py`；修改 `bot/chat.py`, `bot/consciousness.py`, `bot/group_personality.py`, `bot/memory.py` 等

1. **统一 LLM 调用包装** `llm_call.py`：
   - `async def llm_create(**kwargs)` 包装 `client.messages.create`
   - 捕获 429 / RateLimitError / APIConnectionError → 指数退避重试（5s → 15s → 40s，最多 3 次）
   - 区分：可重试错误（429/超时/连接）vs 永久错误（400/401/403）
2. **延迟补发队列**：重试仍失败 → 回复存入 `data/pending_replies.json`（含 group_id/text/时间戳）；后台定时器每分钟扫一次，成功就补发，超时 1 小时丢弃
3. **缓存日志降噪**：GLM 不支持 prompt caching，cache_read=0 是常态，把 cache 日志降为 DEBUG

### 阶段 2：人味 — 内容驱动情绪 + 对话会话跟踪

**改动文件**: `bot/consciousness.py`, `bot/handler.py`, `bot/chat.py`, `bot/decision_engine.py`

1. **内容驱动情绪管线**（打根因 D）：
   - 即时层：关键词门控（免费，每条都跑）—— 悲伤词/开心词/震惊词命中 → `on_read_emotional_content(valence, arousal)`
   - 周期层：每 ~10 分钟或 20 条 LLM 批量 digest（兜底，抓关键词漏的）
   - `consciousness` 新增 `sadness` 维度 + `on_read_emotional_content` 事件钩子
2. **对话会话跟踪**（打根因 B、C）：
   - 单用户 followup → 多用户会话（TTL 8-15 分钟，随情绪伸缩）
   - 会话期内：不 @ 也有高回复概率（40-60%）；相关上下文当一条线保留
3. **回复决策调优**：
   - `decision_engine` 的 mood 改为只读自 `consciousness`（消除两套情绪打架）
   - 主动概率按内容驱动情绪缩放

## 范围外
- 不统一两套情绪系统的存储（方案 C 的重构，风险高，留待后续）
- 不改自动审核 / 违规 / 黑名单逻辑
- 不改 Web UI

## 验证
- 阶段 0：重启后 Bot 不再自称丛雨丸；quirks 列表干净
- 阶段 1：模拟 429 时回复延迟补发
- 阶段 2：群里发悲伤内容 → Bot 情绪变 sad；不 @ 也能接上对话
