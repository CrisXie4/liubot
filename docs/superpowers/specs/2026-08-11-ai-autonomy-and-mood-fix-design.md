# AI 自主权扩大 + 旧新闻修复 + 心情/睡眠系统重做

- **日期**: 2026-08-11
- **状态**: 设计完成，待评审
- **作者**: 协作产出（用户 + opencode）
- **相关模块**: `bot/web_search.py`, `bot/ai_brain.py`, `bot/consciousness.py`,
  `bot/ai_tools.py`, `bot/chat.py`, `bot/handler.py`, `bot/guardian.py`, `bot/settings.py`

---

## 一、背景与目标

用户反馈三个独立但相关的问题：

1. **旧新闻问题**：AI 把 2023 年的"腾讯视频夏季动画导视"当新闻发出来。
   根因是 `web_search.search()` 没有任何日期过滤，也没把发布日期喂给判断新鲜度的 LLM。
2. **权限太死，不像人**：现有冷却（2h 新闻间隔 / 30min 学习间隔）、
   深夜静默硬墙、精力门槛等让 AI 行为机械。用户希望"他想发消息就发消息"。
3. **心情卡死**：观察到 Bot 连续两天显示「一言难尽」（grumpy），
   查 `data/consciousness.json` 发现 `irritation=0.5098` 卡在 0.5 阈值上，
   而 `joy=0.57 / curiosity=0.99 / satisfaction=0.86` 本应是开心状态。
   根因有四个，详见"工作流 ④"。

外加用户追加的第 5 个需求：**AI 睡眠时间也由 AI 自己决定**，但必须有 ≥1h 睡眠底线。

### 不在本 spec 范围
- 不动 `DANGEROUS_TOOLS`（kick/ban/whole_ban 等仍走 guardian 审批）
- 不动保护名单（主人/管理员/白名单）
- 不改自动审核（违规识别）系统
- 不做"正在输入..."提示（用户确认 QQ 群里这个状态不可靠）

---

## 二、五大工作流

### 工作流 ① — 发言更像真人：拆分短消息

**问题**：当前 `chat.py` 把 LLM 整段回复作为一条消息发送，看起来像机器。
真人会用 2-4 条短消息连续表达，中间停顿。

**设计**：
- 在 `chat.py` 发送 LLM 回复前，调一个轻量切片函数 `_split_into_segments(text)`。
- 切片策略（不调 LLM，纯规则）：
  1. 优先按换行符 `\n` 切
  2. 其次按句末标点 `。！？!??` 切
  3. 长段（>40 字）再按 `,，、` 软切
  4. 每段长度 6-30 字之间最自然；过短（<3 字）合并到下一条
- 发送：逐条 `send_group_msg`，间隔 `random.uniform(1.5, 3.5)` 秒。
- **上限 4 条**：超出 4 条就把剩余合并到最后一条。
- **跳过拆分的场景**：含图片、含合并转发、含 `[CQ:forward`、单条消息含 `[CQ:at`、
  纯表情/贴纸（`[CQ:` 占比 >70%）、命令回复、纯链接。
- **可关闭**：`settings.json` 加 `chat.split_messages: bool`（默认 true）。

**风险与缓解**：
- 拆得过碎刷屏 → 上限 4 条 + 段长下限 6 字
- API 调用变多 → 不增加 LLM 调用，只增加 send 调用，可接受
- 中间被打断 → 容错：群里有新人@Bot 时，剩余段取消

---

### 工作流 ② — 自然表情表态（不说话的"参与感"）

**问题**：`emoji_reaction` 工具只在 AI 主动调用时用，绝大多数群消息 AI 是沉默的，
看起来不参与。真人看到好笑的会贴个 😂，不会每次都长篇回复。

**设计**：
- 在 `handler.py` 处理每条**非 @ Bot** 的群消息后，低概率触发表态评估：
  - 触发概率 12%（可配置 `chat.auto_emoji_rate`）
  - 排除：Bot 自己的消息、纯命令、被@的消息（这些走正常回复路径）
- 触发后调一次轻量 LLM 判断（走 `llm_call` P3 低优先级）：
  ```
  输入: <消息文本，最多 100 字>
  输出: NONE 或单个 emoji_id（128512-128591 范围）
  判断标准: 好笑/赞同/惊讶/感动 → 贴；中性/无聊/看不懂 → NONE
  ```
- 贴表情：调 `bot.set_msg_emoji_like(message_id, emoji_id)`
- **限速**：每群每小时最多 8 次自动表态（`guardian.check_auto_emoji_rate`）。
- **不重复**：同一条消息只表态一次（缓存 message_id 集合）。

**风险与缓解**：
- 每条都贴很烦 → 低概率 + 限速 + LLM 判断门槛
- 误表态尴尬内容 → LLM 看不懂就输出 NONE，保守优先
- 成本：12% 消息触发 LLM 调用 → 用 P3 优先级 + 最小 max_tokens=10

---

### 工作流 ③ — 修复旧新闻（多层防御）

**问题**：`web_search.search()` 给 SearXNG 发的请求没有任何时间过滤；
判断"够不够新鲜"的 LLM 也没拿到发布日期，全凭标题猜。多层防御缺一不可。

**改动 A — `bot/web_search.py`**：

1. `search()` 增加参数 `time_range: str = None`，取值 `day/week/month/year/None`。
   传给 SearXNG 的请求参数加 `"time_range": time_range`（None 不加这个 key）。
2. `search()` 增加参数 `max_age_days: int = None`。解析结果时，从 `publishedDate`
   字段提取发布日期，早于 `now - max_age_days` 的结果**直接丢弃**。
   - `publishedDate` 是 SearXNG JSON API 标准字段（ISO 8601 字符串）。
   - 缺失/解析失败 → 保留（不丢，因为有些引擎不返回此字段）。
3. `_format_results()` 在输出里加 `"published"` 字段（人可读的日期字符串，
   例如 `2026-08-10`，解析失败为空串）。
4. `format_search_results()` 在每条结果后面加 `发布日期: ...` 一行。
5. 新增便捷封装：`search_recent(query, max_age_days=7, max_results=5)`，
   默认 `time_range="week"` + `max_age_days=7`。

**改动 B — `bot/ai_brain.py:autonomous_learn`**：

1. 搜索改用 `web_search.search_recent(query, max_age_days=7)`（默认只搜一周内）。
2. 搜索 query 的修饰词从 `["最新消息", "今日新闻", ...]` 改为
   `["最新", "今天", "本周", "最近"]`（去掉"有趣的事""热门话题"这类不含时效的词）。
3. 选题池里把纯品类词改成带时效的：
   - `"动漫资讯"` → `"本周新番/动画"`
   - `"游戏资讯"` → `"本周游戏发布"`
   - `"科技新闻"` → `"本周科技新闻"`
4. 判断 prompt 里增加一行硬约束：
   `其中任何一条发布日期早于 7 天前都不能算"够新鲜"，必须 DECISION: no。`
5. 把搜索结果的 `published` 字段一起喂给判断 LLM，让它有事实依据。

**改动 C — 分享前新鲜度硬校验**：
在 `share_news_if_ready()` 拿到 `result` 后、发送前，调一次
`_verify_news_freshness(result["text"], result["topic"])`：
- 用搜索结果原始数据（要把 `published` 透传出来），
- 若最新一条发布日期早于 30 天，记日记"查到的都是旧闻，算了"，return None。
- 这是最后一道兜底，防止 LLM 被标题骗了。

**改动 D — `ai_tools.py` 的 `web_search` 工具描述**也加上时间提示：
`"默认只搜索最近一周的内容。用户明确问历史事件时不限制。"`（工具本身不改，
因为 AI 调用时一般就是问时效性问题，限制在 search_and_format 这层做。）

**可配置性**：`settings.json` 加 `search.default_max_age_days: 7`（默认 7 天，
可调成 30 或 0=不限）。

**风险与缓解**：
- SearXNG 偶尔不遵守 time_range → max_age_days 二次过滤兜底
- 某些正当旧话题（如"动漫发展史"）被误杀 → 默认只影响 `autonomous_learn`，
  AI 主动调用 `web_search` 工具时**不强制**加时间过滤（让 AI 自己判断）

---

### 工作流 ④ — 修复心情卡死（4 处改动）

**根因诊断**（基于 `data/consciousness.json` 实际数据）：
- `irritation=0.5098`，刚好越过 `get_dominant_mood` 第一个判断 `> 0.5 → grumpy`
- 但同时 `joy=0.57 / curiosity=0.99 / satisfaction=0.86`，明显是积极状态
- `apply_natural_decay` 让 `irritation` 以 0.5 为目标衰减，
  而 `irritation` 当前值正好略高于 0.5，衰减几乎不动
- `on_insulted` 加 irritation，但**没有任何事件减 irritation**，
  被@/被夸/被回复时只加正向情绪，烦躁只增不减（直到慢慢衰减回 0.5 中性）

**改动 1 — 负向情绪衰减目标独立化**（`consciousness.py`）：
- 当前 `apply_natural_decay` 所有情绪都以 0.5 为目标（`target = 0.5`）
- 改为按情绪分别设目标（新常量）：
  ```python
  EMOTION_BASELINE = {
      "joy": 0.55,         # 默认略偏正
      "calm": 0.60,
      "energy": 0.55,
      "curiosity": 0.55,
      "social": 0.55,
      "satisfaction": 0.55,
      "irritation": 0.12,  # ★ 衰减到低基线，不卡在 0.5
      "sadness": 0.12,     # ★ 同上
      "boredom": 0.25,     # 无聊会自然增长，但增长到 0.25 后稳定
      "loneliness": 0.25,
  }
  ```
- `apply_natural_decay` 用 `EMOTION_BASELINE[emotion]` 替代硬编码的 0.5

**改动 2 — `get_dominant_mood` 优先级调整**：
- 把 `irritation > 0.5 → grumpy` 的阈值从 0.5 提到 **0.6**
- 把积极复合判断前置：当 `joy >= 0.55 and energy >= 0.4` 时优先返回 happy/neutral_happy
- 让"明显开心但 irritation 边缘"的状态正确归到 happy

**改动 3 — 正向事件降低烦躁**：
- `on_mentioned`、`on_replied_to_bot`、`on_praised` 三个函数里，
  除了加 joy 之外，都加一行 `apply_emotion_delta(state, "irritation", -0.04)`
- `on_praised` 减 0.08（被夸很治愈）
- 这样日常社交会自然让烦躁回落，不依赖时间衰减

**改动 4 — 修复现存状态**（一次性）：
- 写一个一次性脚本 `bot/fix_state.py`（不进任何循环，运行一次即止）：
  读 `data/consciousness.json`，若 `irritation > 0.4 且 joy > 0.5`，
  把 irritation 重置为 `EMOTION_BASELINE["irritation"]` (0.12)，
  备份原文件为 `consciousness.json.bak.20260811`。
- 或更简单：在 `consciousness.py` 加一个 `_migrate_v1_to_v2(state)` 钩子，
  版本号升到 2 时执行，把异常的边缘卡死值重置（与改动 1 同步生效）。

**验收**：Bot 在被@一次后，30 分钟内主导情绪应从 grumpy 转为 happy/curious。

---

### 工作流 ⑤ — AI 自主管理睡眠

**目标**：AI 自己决定何时睡、睡多久；但每 24h 累计睡眠必须 ≥ 60 分钟（硬底线）。

**新增字段**（`consciousness.py:DEFAULT_STATE`）：
```python
"sleepiness": 0.0,            # 困意值 0-1，独立维度
"sleeping_until": "",         # ISO 时间戳，预计醒来时间；空=未睡
"last_24h_sleep_minutes": 0,  # 过去24h累计睡眠分钟（滑动累计）
"last_sleep_tracking_date": "",  # 用于每日重置滑动窗口
```

**新增情绪维度**：`emotions.sleepiness`（0-1），加入 `DECAY_RATES`：
- 不衰减（固定），由"昼夜节律 + 连续清醒时长"两个驱动器决定（见下）

**驱动 `sleepiness` 上升的因素**（在 `tick()` 里算）：
1. **昼夜节律加成**（替换原来硬触发的 `sleeping` 标志）：
   - 0-6 点：`sleepiness += 0.015/分钟`（90/分钟累积到 1.0）
   - 23-24 点：`sleepiness += 0.010/分钟`
   - 13-14 点（饭困）：`sleepiness += 0.003/分钟`
   - 其他时段：`+= 0.001/分钟`（基础疲劳积累）
2. **连续清醒时长**：每连续清醒 1 小时，`sleepiness` 累积速率 +20%
3. **情绪抑制**：被@/被夸时 `sleepiness -= 0.05`（兴奋得睡不着）；
   无聊时 `sleepiness += 0.002/分钟`

**触发睡眠的两种路径**：
1. **自主入睡**（默认）：当 `sleepiness >= 0.85` 时，
   AI 通过 volition LLM 自己决定要不要睡（在 `decide_and_speak` 的 prompt
   里加一行"你很困了，要不要去睡？要睡就回复 SLEEP: <时长分钟>"）
2. **强制兜底**：若 `last_24h_sleep_minutes < 60` 且当前连续清醒 > 23h，
   或 `sleepiness >= 0.98` → 跳过 LLM，直接强制睡 90 分钟。

**执行睡眠**（新函数 `go_to_sleep(state, minutes)`）：
- 设置 `sleeping=True, sleeping_until=<now+minutes>`
- 写日记 `f"困了，去睡 {minutes} 分钟"`
- 默认时长由 `sleepiness` 决定：0.85→30min，0.95→90min，0.99→240min
- **强制下限**：当日累计 <60 分钟时，本次最少睡到补齐 60 分钟

**醒来**（在 `tick()` 里检查 `sleeping_until`）：
- `now >= sleeping_until` → 醒来
- 醒来效果：`energy = min(1.0, energy + minutes*0.005)`，`sleepiness = 0`
- 累加 `last_24h_sleep_minutes += minutes`
- 写日记 `f"睡了 {minutes} 分钟，精神多了"`（仅当 minutes >= 30 才记）

**被吵醒**（在 `handler.py` 里被@时）：
- 若 `state.sleeping=True` 且当前在 `sleeping_until` 之前 → 醒来但带起床气
- `apply_emotion_delta(state, "irritation", +0.10)`
- `sleepiness = max(sleepiness - 0.3, 0.3)`（半醒，一会儿可能再困）
- `sleeping = False; sleeping_until = ""`
- 写日记 `f"被叫醒了…有点烦"`

**熬夜惩罚**（连续清醒 > 20h 还没睡）：
- `tick()` 检查 `awake_hours > 20`：
  - `apply_emotion_delta(state, "energy", -0.01/分钟)`（强制疲劳）
  - `apply_emotion_delta(state, "boredom", -0.005/分钟)`（困到不想找事）
  - 不直接强制睡（留给兜底机制）

**`can_speak_proactively` 调整**：
- `sleeping=True` 仍直接 return False（睡觉中不主动说话，被@才醒）
- **不再**因为 `is_quiet_hours()` 直接拒绝——改成给 `sleepiness` 加成
  （深夜 sleepiness 涨得快自然就不想说话）
- 静默时段 `quiet_hours` 保留为可选的"硬建议"：默认仍生效（1:00-7:00 不主动冒泡），
  但可由 Web UI 关闭；关闭后只靠 sleepiness 调节

**改动 `apply_circadian`**：
- 不再硬置 `state["sleeping"] = True`
- 改为给 `sleepiness` 加 delta（见上面"驱动因素"）
- 保留原来的情绪节律调整（energy/joy/social 等）

**新增工具 `set_sleep`**（`ai_tools.py`）：
```python
{
    "name": "set_sleep",
    "description": "去睡觉。当你困了、累了、或者单纯想躲起来一会儿时用。"
                   "minutes 是睡觉时长，建议 30-480。当天累计睡眠不足 60 分钟时会被强制补足。",
    "input_schema": {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "睡多少分钟，30-480"},
        },
        "required": ["minutes"],
    },
}
```
- 不在 `DANGEROUS_TOOLS` 里（睡觉是自治行为）
- `execute_tool` 里调 `consciousness.go_to_sleep(get_state(), minutes)` + 记日记

**兜底验证**：
- 写单测 `tests/test_sleep_floor.py`：模拟连续 23h 不睡，验证第 23h 触发强制睡 ≥60min
- 写单测 `tests/test_sleepiness.py`：模拟深夜时段，验证 sleepiness 累积到阈值

**与既有逻辑兼容**：
- `ai_brain.py` 所有 `state.get("sleeping")` 检查仍然有效（睡觉时确实不主动发）
- Web UI 显示状态时把 `sleepiness` 也展示出来（可选改动，不影响功能）

---

## 三、统一收尾：解除主动发言限制（B 方案边界）

**保留**：
- 昼夜节律（不再硬触发睡觉，但影响情绪/sleepiness）
- `quiet_hours`（默认 1:00-7:00，可 Web UI 关闭）
- 睡眠状态中不主动发消息（被@会醒）

**去掉**：
- `ai_brain.py:NEWS_SHARE_INTERVAL = 7200`（2h 冷却）→ 删掉
- `ai_brain.py:LEARN_INTERVAL = 1800`（30min 冷却）→ 改为 60 秒（防同一秒内连调）
- `ai_brain.py:share_news_if_ready` 里 `irritation > 0.6` 硬条件 → 删掉
- `consciousness.py:can_speak_proactively` 里 `energy < 0.12` 硬拒绝 →
  改成软建议（return True 但日志记"精力低"）

**新增软限速兜底**（`guardian.py`）：
- `check_proactive_rate(group_id)`: 每群每小时主动消息（含新闻分享 + volition SPEAK）
  上限 8 条（可配置）。超过返回 False，`maybe_proactive_action` 直接 return None。
- 这是防刷屏安全网，正常情况下不会触发。

---

## 四、验收清单

实现完成后必须验证：

- [ ] 跑 `python -c "from bot import web_search; ..."` 搜索"腾讯视频 动画"，
      返回结果中 `published` 字段都 ≤ 7 天
- [ ] 跑一次 `autonomous_learn`（mock），输出 `decision=yes` 的话题在 30 天内有发布日期
- [ ] 重启 Bot 后 30 分钟内，主导情绪不再卡在 grumpy（被@一次后应转为 happy/curious）
- [ ] 设 `consciousness.json` 故意把 irritation 调到 0.51，重启后 1 小时内自动回落到 < 0.3
- [ ] 让 AI 连续运行 24h（模拟），累计睡眠分钟数 ≥ 60
- [ ] 触发 `set_sleep(120)` 工具，验证 `sleeping_until` 正确设置 + 醒来后精力恢复
- [ ] AI 回复一条 30 字以上的群消息时，拆成 2-4 条短消息发送（看日志 send 次数）
- [ ] 群里发条明显好笑的消息，1 小时内有概率被贴表情（看 set_msg_emoji_like 日志）

---

## 五、风险登记

| 风险 | 影响 | 缓解 |
|------|------|------|
| 拆分消息被@打断 | 回复不完整 | handler 监听新@，取消未发段 |
| 自动表态误判 | 表情贴得突兀 | 低概率 + LLM 判断 + 限速 |
| 搜索时间过滤太严 | 错过正当旧话题 | 仅 autonomous_learn 强制，AI 工具调用不强制 |
| 解除冷却后刷屏 | 群体验差 | 软限速 8 条/小时兜底 |
| 睡眠自主后总不睡 | 违反"必须睡"需求 | 23h 强制睡兜底 + 60 分钟硬底线 |
| sleepiness 计算偏差 | 该睡不睡或不该睡老睡 | 上线后观察 3 天，调整累积速率常量 |

---

## 六、实施顺序建议

1. 工作流 ④（心情修复）——独立、风险最低、立即可见效果
2. 工作流 ③（旧新闻）——独立、可单测
3. 工作流 ⑤（睡眠自主）——依赖 ④ 的情绪基线调整
4. 工作流 ①（拆分短消息）——独立、用户感知最强
5. 工作流 ②（自动表态）——最后做，依赖前置改动稳定
