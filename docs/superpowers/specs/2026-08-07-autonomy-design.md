# QQ Bot 最大化自主性 — 设计文档

**日期**: 2026-08-07
**状态**: 已批准，待实施
**模型**: glm-5.2-free @ https://api.ccode.vip （Anthropic 格式，已验证）

## 背景

用户需求：把 AI 的自主性拉到最高，让它"想干嘛干嘛"，同时保留对"不合理"操作的安全闸门。七项具体能力：

1. AI 自己调节自己的 QQ 情绪（心情）
2. 想撤回谁就撤回谁，但不合理不能撤
3. 觉得谁说不合理就封禁
4. 把自主性调到最高
5. AI 有自己的心情，会主动找新闻等内容
6. 主动设置群成员的群文件、设置群精华
7. 重大消息（如悲伤新闻）可 @全体成员；不只限于此，AI 想干嘛就干嘛

同时需求里删除固定关键词指令（`违规`/`禁言`/`踢人`/`拉黑`/`精华`/`取消精华`/`全员禁言`），改为 AI 自然语言理解主人的话直接执行。

## 关键约束（实测确认，非推测）

经四轮探针实测（`urllib` 直调 `https://api.ccode.vip/v1/messages`，2026-08-07）：

1. **思考模式不可用**。账号只有 `glm-5.2-free` 一个模型；该模型静默忽略所有 thinking 参数变体：
   - Anthropic 格式 `thinking={"type":"enabled","budget_tokens":N}` → `reasoning_tokens=0`
   - `reasoning_effort="high"` → `reasoning_tokens=0`
   - `enable_thinking=True`/`do_sample=True` → `reasoning_tokens=0`
   - 其他推理模型（`glm-4.6`/`deepseek-r1`/`deepseek-reasoner`/`qwq-32b-preview`/`o1-mini`）在账号分组下均返回 `503 model_not_found`。
   - **决定：跳过思考模式**。同时删除残留的"幽灵"配置（`config.LLM_THINKING_EFFORT`、Web UI 思考强度下拉、`memory.py:684` 默认值），避免误导。

2. **LLM 调用极慢**。单次 `max_tokens=16` 的请求耗时 **57–98 秒**（免费档）。这意味着任何"同步裁判"（破坏性动作前再等一次 LLM 判定）在群聊实时场景里不可行。

3. **网关底层是 OpenAI 语义**（`billing_usage.source="oai_chat"`），但接口形态是 Anthropic `/v1/messages`。Anthropic SDK 仍是正确的客户端。

## 现状审计（来自代码探索）

| 能力 | 状态 | 关键位置 |
|---|---|---|
| OneBot 传输层 | 完整 | `bot/onebot_client.py` 已包 `delete_msg:318` / `set_group_kick:570` / `set_group_ban:335` / `set_essence_msg:377` / `upload_group_file:709` / `get_group_at_all_remain:666` / `set_group_card:354` 等 |
| 心情引擎 | 成熟 | `bot/consciousness.py:33` 十维情绪，动态变化（昼夜节律 + 对话事件 + 内容情绪 + 衰减） |
| 自主找新闻 | 成熟 | `bot/ai_brain.py:242 autonomous_learn`（每 30min）+ `share_news_if_ready:383`（每 2h 广播） |
| AI 工具集 | 57 工具 | `bot/ai_tools.py:ALL_TOOLS` 已含 `set_mood`/`at_all`/`send_essence`/`ban_member`/`kick_member`/`whole_ban` 等 |
| guardian 审批 | 半成品 | `bot/guardian.py:115 create_action_request` 有审批队列，但 `ai_tools.execute_tool` **未接入** → `kick_member`/`ban_member` 当前裸执行（`ai_tools.py:839`） |
| 自禁豁免 | 缺失 | 现有 `recall_msg` 工具仅限自己（`ai_tools.py:108`） |
| 群文件工具 | 缺失 | API 全有，但 `ai_tools` 无 `upload_group_file`/`create_group_folder` 工具定义 |
| 关键词指令 | 冗余 | `handler.py:1224-1863` 大量关键词块，需删减 |

## 架构

### 总览

```
群消息 ──► handler.handle_group_message
             │
             ├─ 非破坏性自主 ──► 直接执行
             │   （调心情 / 设精华 / 传群文件 / @全体限速 / 改自己昵称）
             │
             ├─ 破坏性·轻量 ──► 快规则护栏 ──► 当场执行 ──► 审计入库
             │   （撤回他人 / 禁言 ≤10min / 改别人昵称）
             │
             └─ 破坏性·重度 ──► guardian.request() 异步裁判
                 （踢人 / 封禁 / 禁言 >10min / 全员禁言）
                    ├─ 群里先发"让我想想…"占位
                    ├─ 后台独立 LLM 裁判（~100s）
                    │   看：AI 的理由 + 最近10条上下文 + 保护规则 + 是否主人指令
                    ├─ 通过 ─► 执行 + 审计
                    └─ 拒绝 ─► 不执行，AI 被告知原因（可转告群）

主人自然语言指令 ──► AI 听懂 ──► 带 master_instructed 标记进闸门
                                  （裁判从宽，但仍禁止动主人自己）
```

### 组件改动

#### 1. `bot/guardian.py` — 升级为通用裁判闸门（大改）

保留现有审批队列与 `secondary_check`，新增：

- **`action_severity` 表**：把每个动作映射到 `autonomous` / `light` / `heavy` / `exempt`。
- **`is_protected(target_qq, group_id)`**：查保护名单（主人 + 群主 + 群管理员 + 白名单）。当 `master_instructed=True` 时，除主人自身外其余可被覆盖。
- **`request(action, target, reason, master_instructed, severity)`**：统一入口。轻量当场返回 `allow`；重度入异步裁判。
- **`judge(action, target, reason, context)`**：重度动作裁判。独立 LLM 调用（低温度、极简 prompt，只回 JSON `{allow, verdict}`），返回裁定。
- **占位文案**：重度动作触发时，先在群里发"让我想想…"（文案在配置里可改），避免群里长时间无反馈。
- **限速器**：@全体每群每 6h 至多 1 次；自主设精华每群每天至多 N 次（在配置里）。

#### 2. `bot/ai_tools.py` — 工具路由 + 新工具（中改）

- **`execute_tool` 改造**：所有破坏性工具（撤回他人/封禁/禁言/踢人/全员禁言）执行前**统一调 `guardian.request(...)`**，按返回的 `allow/deny` 决定是否真正调 `onebot_client`。堵住 `ai_tools.py:839` 裸执行漏洞。
- **解除 `recall_msg` 仅自己限制**：改为可撤回他人，进轻量级护栏。
- **新增工具定义**：
  - `upload_group_file`：生成 .txt 摘要（或指定内容）上传到群文件
  - `create_group_folder`：建群文件文件夹
  - `set_member_card`：改他人群昵称（仅群主态下；进轻量级护栏）
  - `set_member_title`：设群头衔
- **自禁豁免**：当目标 QQ == 发起者 QQ 时（"禁我"玩笑），直接执行不进护栏。

#### 3. `bot/handler.py` — 删关键词 + 接闸门（中改）

- **删除关键词块**：`违规` / `禁言` / `踢人` / `拉黑` / `精华` / `取消精华` / `全员禁言`。这些能力改为 AI 通过工具调用 + 自然语言理解执行。
- **保留**：`待审批` / `同意` / `拒绝`（重度动作裁判不确定时可转人工兜底）、mode 切换、查询类指令。
- **主人指令识别**：当发言者 == 主人时，AI 工具调用带 `master_instructed=True` 标记（在 `execute_tool` 入参透传）。

#### 4. `main.py consciousness_loop` — 主动式自主（小改）

在现有 60s 循环里追加：
- **自主设精华 tick**：扫近 N 分钟高影响力消息（`member_influence` ≥ 阈值）且群氛围好时，AI 可主动调 `set_essence_msg`。
- **重大新闻 @全体**：`share_news_if_ready` 命中灾难/重大事件时，AI 可 @全体（先查 `get_group_at_all_remain`，受 guardian 限速器约束）。
- **自主传群文件**：每日总结/精华合集生成 .txt 后 `upload_group_file`。
- **心情自我调节**：保留现有引擎；确保 `set_mood` 工具持续可用并反映到 QQ 在线状态（`ai_brain.sync_qq_status`）。

#### 5. `bot/onebot_client.py` — 几乎不动

API 都已存在，仅在工具层暴露。

#### 6. Web UI / 配置

- `web/static/pages/model.html` + `js/model.js`：删除思考强度下拉（既然跳过思考，避免误导）。
- `web/api.py:288`：从允许字段集移除 `thinking_effort`。
- 新增自主性配置页（或并入 `humanize.html`）：保护名单、分级阈值、异步裁判文案、@全体限速参数、自禁豁免开关。

#### 7. 配置清理

- `bot/config.py:25`：删 `LLM_THINKING_EFFORT`（死代码）。
- `bot/memory.py:684`：从 `DEFAULT_MODEL_SETTINGS` 移除 `thinking_effort`。

### 数据持久化

| 文件 | 用途 |
|---|---|
| `data/ai_guard_config.json`（新增） | 保护名单、分级阈值、限速参数、裁判文案 |
| `db.py` 新表 `ai_audit` | 每条破坏性动作审计：`{ts, action, target, reason, judge_verdict, master_instructed, undone}`（用 SQLite，见下） |
| `data/consciousness.json`（现有） | 心情，不动 |
| `data/member_influence.json`（现有） | 影响力，复用作精华阈值依据 |

**审计日志用 SQLite 表**（优于 JSON）：
- 频繁追加、需按 target/action 查询撤销。
- 主人自然语言撤销时，AI 查最近 N 条对某目标的动作，调对应反向 API（如 `set_group_ban(duration=0)` 解禁、`delete_essence_msg` 撤精华）。

## 数据流（示例：主人说"把张三禁言一小时"）

1. `handle_group_message` → 识别发言者是主人 → AI 收到上下文（含主人发言）
2. AI（`chat.chat_reply`）触发 `ban_member` 工具，参数 `{target: 张三QQ, duration: 3600, reason: "主人指令"}`
3. `execute_tool` → `guardian.request(action="ban_member", target=张三QQ, severity="heavy", master_instructed=True)`
4. guardian：张三不是主人 → 不在保护名单（假设）→ 重度 → 群里发"让我想想…"→ 后台 `judge()`
5. 裁判 LLM 看主人指令 + 理由 → `allow=True`
6. `execute_tool` 调 `onebot_client.set_group_ban(gid, 张三, 3600)` → 写审计表
7. （如裁判 `deny`）AI 被告知"裁判拒绝：理由"，可转告群里

## 示例（AI 自主撤回违规消息）

1. 群友发言触发 `automod.should_auto_recall` 命中
2. `handler` 走轻量护栏：`guardian.request(action="recall", severity="light")` → 当场 `allow`
3. `bot.delete_msg` 执行 → 写审计 → 可选短禁言
4. 全程不调裁判 LLM，零额外延迟

## 风险与不做

- **不做思考模式**（已确认；代码层面也清理残留配置）。
- **裁判本身也慢且可能错**：缓解——重度动作触发频率低；裁判 prompt 极简（低 token）；不确定时可转人工审批（保留的 `同意/拒绝` 通路）。
- **误撤回/误封**：靠审计 + 主人自然语言撤销兜底。
- **@全体滥用**：硬限速（每群每 6h 1 次，先查剩余额度）。
- **不重构**现有 mood 引擎（`consciousness.py`）、news 管线（`ai_brain.autonomous_learn`/`share_news_if_ready`）、influence 体系——已成熟，复用。

## 受影响文件汇总

| 文件 | 改动规模 |
|---|---|
| `bot/guardian.py` | 大改：通用裁判 + 分级 + 异步 + 保护名单 + 限速 |
| `bot/ai_tools.py` | 中改：新工具 + `execute_tool` 路由进 guardian |
| `bot/handler.py` | 中改：删关键词块 + 接新闸门 + master_instructed 标记 |
| `bot/db.py` | 小改：加 `ai_audit` 表 |
| `main.py` | 小改：consciousness_loop 加自主 tick |
| `bot/onebot_client.py` | 几乎不动 |
| `web/api.py` | 小改：移除 thinking_effort 字段 + 自主性配置 API |
| `web/static/pages/model.html` + `js/model.js` | 小改：删思考强度 UI |
| `web/static/pages/humanize.html` 或新页 | 小改：自主性配置 UI |
| `bot/config.py` | 小改：删 `LLM_THINKING_EFFORT` |
| `bot/memory.py` | 小改：从默认设置删 `thinking_effort` |

## 时间同步（独立子需求）

### 背景

bot 全部 `.py` 文件**没有一处用时区**——所有 `datetime.now()` 都是 naive（无 tz），完全依赖 Windows 主机时钟。本机 TZ 虽对，但部署到云服务器（默认 UTC）后所有时间偏 8 小时：17:00 总结变 01:00 触发，bot 半夜主动说话、白天装睡。这是"老是搞错时间"的根因。

### 时间源（10 个 HTTP 源，随机抽样取多数）

实测可用（返回 GMT Date 头，+8 = 上海时间）。建立 10 个源池，每次同步**随机抽 3 个**并发测，取多数一致值，剔除离群：

| # | 源 |
|---|---|
| 1 | `https://www.baidu.com` |
| 2 | `https://www.qq.com` |
| 3 | `https://www.taobao.com` |
| 4 | `https://www.microsoft.com` |
| 5 | `https://www.apple.com` |
| 6 | `https://www.bing.com` |
| 7 | `https://github.com` |
| 8 | `https://www.cloudflare.com` |
| 9 | `https://www.alibaba.com` |
| 10 | `https://www.sohu.com` |

容错：3 个里若 1 个超时/失败，用剩下 2 个（要求一致）；若全失败或互相偏差 >2s，放弃本次同步，保留上一次偏移量；若启动后从未成功同步过，退回 `ZoneInfo("Asia/Shanghai")` 的系统时钟（不阻塞启动）。

### 实现：`bot/timeutil.py`（新增）

- `now_shanghai() -> datetime`：返回 `datetime.now(ZoneInfo("Asia/Shanghai")) + cached_offset`。cache 未就绪时只返回系统时钟（带 tz）。
- `today_shanghai() -> date`。
- `_sync_offset()`：async，启动后跑一次 + 每 30min 跑一次；从 10 源随机抽 3、并发 HTTP HEAD、解析 Date 头、算偏移、缓存。
- 偏移定义为 `authoritative_time - system_time`；正常同步后系统偏移应近 0，但部署到漂移/错误 TZ 的机器时会自动纠偏。
- 依赖：`tzdata`（Windows 上 `ZoneInfo` 需要），加进 `requirements.txt`；HTTP 用已有 `aiohttp`。

### 替换站点（Top 10 关键）

只改这些（喂给 LLM / 触发定时 / 对用户可见）。纯审计时间戳（`db.py` × 11、`verify.py`、`trainer.py`、`sticker.py` 等）**不动**——它们只记日志，不影响"搞错时间"。

| # | 文件:行 | 原因 |
|---|---|---|
| 1 | `bot/consciousness.py:245-253` `get_time_facts_prompt` | 注入**每条**聊天 LLM 调用 |
| 2 | `main.py:251-253` consciousness_loop `now/hour/today` | 驱动**所有**定时任务 |
| 3 | `bot/consciousness.py:134-135` `get_circadian_modifiers` | 决定睡觉/清醒 |
| 4 | `bot/consciousness.py:297` `is_quiet_hours` | 硬控 01:00-07:00 静默 |
| 5 | `bot/personality.py:254` `get_time_context` | 系统提示词里的时间 |
| 6 | `bot/consciousness.py:975` `check_daily_reset` | 日边界（计数器重置） |
| 7 | `bot/ai_brain.py:92,530,553` | "睡觉中"状态 + 对群说的"现在是{hour}点" |
| 8 | `bot/summary.py:30-31` | 17:00 总结的日期标题 |
| 9 | `bot/scheduler.py:89` `_run_schedule` | cron HH:MM 定时消息 |
| 10 | `bot/welcome.py:113,127,171` `do_checkin` | 签到日边界/连续天数 |

## 验收标准

- 删除 7 个关键词指令后，用自然语言对 AI 说"把xxx禁言10分钟"能成功执行。
- AI 主动设精华/传群文件/@全体 各至少跑通一次。
- 保护名单内成员被 AI 尝试撤回时被拒绝（日志可见）。
- 自禁请求（"禁我"）能成功执行。
- 审计表每条破坏性动作有记录；主人说"撤销最近对xxx的禁言"能解禁。
- 跳过思考模式后，无残留 `thinking_effort` 配置项或 UI。
- 重度动作触发时群里先出现"让我想想…"占位，裁判完成后有执行或拒绝反馈。
- **时间同步**：`timeutil.now_shanghai()` 启动后自动从 10 源随机抽 3 同步；bot 报给群/LLM 的时间在系统时钟偏移时仍准确；定时任务（17:00 总结/23:00 反思/03:00 扫描）按真实上海时间触发。
