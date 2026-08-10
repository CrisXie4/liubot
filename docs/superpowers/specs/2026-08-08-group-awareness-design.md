# 群感知（名字→QQ 解析 + 群事件感知）设计

> 日期：2026-08-08
> 目标：让 AI 始终知道「群里在发生什么」——谁在说话、谁叫什么、谁刚被禁言/撤回/踢出，并能按名字找到对应的 QQ 号执行动作。
> 不在本次范围：自禁/双封的 bug（等本特性上线后用真实日志做 systematic-debugging，不靠猜）。

## 一、问题背景

1. **AI 工具只收 `user_id`**：`ban_member`/`kick_member`/`at_member`/`send_private` 等全部要求传数字 QQ 号。用户说「封禁嘟嘟」时 AI 没有「嘟嘟→QQ」的解析能力，只能靠解析「在场的人」花名册文本里的号码来猜，找不到就只能放弃或乱填。**导致"封发消息的人"指令被 AI 拒绝。**

2. **AI 对群事件瞎**：`bot/handler.py:handle_notice` 处理了撤回/禁言/踢人/退群等通知事件，但**只打日志**（撤回甚至 `pass`，handler.py:551-553），从未把事件喂给 AI 意识系统。整个 `handle_notice` 里唯一回调 AI 的只有新人入群（`ai_brain.on_new_member`，handler.py:383）。**导致 AI 看不到群里发生了什么，"反应慢几拍"。**

## 二、解决方案

拆成两个互相独立的小特性，共享「注入给 AI 的上下文」这一批注入点：

### 特性 A：名字 → QQ 解析（`find_member` 工具）

#### A1. 匹配策略（用户已确认）

精确 + 子串，多人时反问，绝不自动猜。

- **第一轮**：精确匹配 `card`（群名片）或 `nickname`（QQ 昵称）完全等于查询词
- **第二轮**（无精确命中时）：子串匹配——「嘟嘟」可命中「小嘟嘟」、「嘟嘟大人」
- **0 命中**：返回"群里没找到叫「嘟嘟」的人"
- **1 命中**：返回 QQ 号 + 名字，AI 直接拿去用
- **N 命中（N≥2）**：返回全部候选（封顶 5 个），按 `match_type`（精确 > 子串）排序；指令工具说明里要求 AI 反问用户确认，不要自动选

#### A2. 改动点

**`bot/identity.py`** — 新增函数 `find_members_by_name(bot, group_id, query) -> list[dict]`
- 拉成员列表走 `bot.get_group_member_list(group_id)`，命中已有的 `group_member_list_cache`（60s TTL，cache.py:142）
- 对每个成员算 `match_type`：`"exact"`（card 或 nickname 完全等于）/ `"substring"` / `""`（不命中）
- 返回 `[{qq, card, nickname, role, match_type}]`，过滤掉空 match_type，按 exact 优先排序，封顶 5 个

**`bot/ai_tools.py`** — 新增工具定义
```
find_member(name) → "按名字（群名片/昵称）查群成员的 QQ 号。
  用户只给名字（'封禁嘟嘟'/'给那个谁发私聊'）时先用这个查 QQ。
  命中多人会返回候选清单——这时你要反问用户确认是哪一个，不要自己猜。"
```
在 `execute_tool` 里加 `elif tool_name == "find_member":` 分支：
- 0 命中 → `"群里没找到叫「{name}」的人"`
- 1 命中 → `"找到了：{name} (QQ={qq})"`
- N 命中 → 逐行列出 `{card或nickname} (QQ={qq}, 角色)`，结尾提示"重名，请让用户指明具体是哪个 QQ"

**`ban_member`/`kick_member`/`at_member`/`send_private`/`set_member_card`/`set_member_title`** 的 description 各加一句："只有名字、没有 QQ 号时，先调 `find_member` 查 QQ。"

**`tests/test_identity_find.py`**（新建）— 5 个用例：精确命中、子串命中、N 命中排序、0 命中、大小写无关

---

### 特性 B：群事件感知（注入「最近发生的事」到 AI 上下文）

#### B1. 设计

新增 `bot/group_events.py` 模块，参照 `bot/identity.py` 的内存环形缓冲模式（不持久化——只要"实时感知"）：

- `_events: dict[group_id, deque[dict]]`，每群保留最近 **8** 条事件，TTL **30 分钟**
- 每条事件：`{ts, type, who_qq, who_name, detail}`
  - type ∈ `recall`（撤回）/`mute`（禁言）/`unmute`（解禁）/`kick`（被踢）/`leave`（主动退群）/`join`（入群）
  - detail 例：禁言时长 / 撤回消息摘要
- 对外提供：
  - `record(group_id, event_type, who_qq, who_name, detail="")` — 写入
  - `build_recent_events_block(group_id, limit=5) -> str` — 生成「【群里最近发生的事】」提示块；空时返回 `""`（与 `build_roster` 保持一致的约定：空块返回空串，调用方自然跳过注入）

#### B2. 接入 `handle_notice`

在 `bot/handler.py:handle_notice` 的每个事件分支里调 `group_events.record(...)`：

| 分支 | 当前行为 | 改后 |
|------|---------|------|
| `group_recall` (handler.py:551) | `pass` | `record(group_id, "recall", operator_id, name)` + 尝试拿撤回前的消息摘要塞 detail |
| `group_ban` ban (handler.py:440) | 仅日志 | `record(..., "mute", target_qq, name, f"{duration}s")` |
| `group_ban` lift_ban (handler.py:443) | 仅日志 | `record(..., "unmute", ...)` |
| `group_decrease` kick (handler.py:423) | 仅日志 | `record(..., "kick", target_qq, nickname)` |
| `group_decrease` leave (handler.py:427) | 发退群通知 | 不变 + `record(..., "leave", ...)` |
| `group_increase` (handler.py:358) | 调 `ai_brain.on_new_member` + 发欢迎 | 不变 + `record(..., "join", ...)` |

注意：`group_recall` 事件里 OneBot 一般只给 `operator_id`（谁操作的），不直接给"撤回的是谁的消息"——只记操作者即可，AI 看到事件再决定要不要追问。名字解析延迟到 `build_recent_events_block` 调用时（走 identity 缓存，避免 notice 处理路径里 await 网络）。

#### B3. 注入到 AI 上下文

跟 `roster` 走同一条路。`handler.py:_build_roster`（handler.py:152）已经在每个会被 AI 处理的消息前生成花名册。把事件块拼到 roster 字符串末尾：

```python
async def _build_roster(...) -> str:
    roster = await identity.build_roster(...)
    events_block = group_events.build_recent_events_block(group_id)
    if events_block:
        roster = f"{roster}\n{events_block}" if roster else events_block
    return roster
```

**为什么走 roster 不走 consciousness.py：**
- roster 是每次 AI 回复前现拼的、带 TTL 的实时上下文，跟"群里刚发生的事"语义对得上
- consciousness.py 的 `diary` 是 AI 的**内部独白/情绪记忆**，把外部客观事件混进去会污染人设；事件流应该是只读事实
- 改 roster 一处就同时覆盖被 @ 和自主回复两条路径（handler.py:2008、:2092），不用改多个注入点

#### B4. AI 提示词微调

`bot/personality.py` 里 roster 说明附近加一句（让模型知道有这个块、什么时候该提）：

> 每条消息前可能附一份「【群里最近发生的事】」清单，记录最近的撤回/禁言/踢人/退群。如果有人问"刚才怎么了"、"谁被禁了"，对照这份清单回答；没人问就自然带过，别主动念。

---

## 三、改动清单

| 文件 | 改动 | 新/改/删 |
|------|------|---------|
| `bot/group_events.py` | 事件环形缓冲 + record/build_recent_events_block | **新建** |
| `bot/identity.py` | + `find_members_by_name` | 改 |
| `bot/ai_tools.py` | + `find_member` 工具定义 + execute_tool 分支 + 6 个 user 类工具 description 微调 | 改 |
| `bot/handler.py` | `handle_notice` 6 个分支加 `group_events.record`；`_build_roster` 拼事件块 | 改 |
| `bot/personality.py` | roster 说明加 1 段事件块说明 | 改 |
| `tests/test_identity_find.py` | 5 用例 | **新建** |
| `tests/test_group_events.py` | 4 用例（record/build/limit/TTL） | **新建** |

依赖：全部走已有库（aiohttp 无新增；identity 已有缓存）。

## 四、验收标准

1. `python tests/test_identity_find.py` 全过
2. `python tests/test_group_events.py` 全过
3. 模拟「封禁嘟嘟」对话：AI 先调 `find_member` 拿到 QQ，再调 `ban_member`；命中重名时 AI 反问而不是乱封
4. 触发一次撤回/禁言事件，下一次 AI 回复的 prompt 里能看到「【群里最近发生的事】」块
