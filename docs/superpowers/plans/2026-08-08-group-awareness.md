# 群感知（名字→QQ 解析 + 群事件感知）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 能按名字找群成员（"封禁嘟嘟"），并实时感知群里的撤回/禁言/踢人等事件

**Architecture:** 两个独立小特性。A: 在 `bot/identity.py` 加 `find_members_by_name` + 在 `bot/ai_tools.py` 加 `find_member` 工具，让 AI 先查 QQ 再执行动作。B: 新建 `bot/group_events.py` 内存环形缓冲，在 `handle_notice` 各分支记录事件，在 `_build_roster` 末尾拼接「【群里最近发生的事】」提示块注入 AI 上下文。

**Tech Stack:** Python 3.14, aiohttp（已有），unittest（已有测试框架，见 tests/）

## Global Constraints

- 依赖只能用已有的（aiohttp / 标准库），不引入新包
- 缓存走已有的 `bot/cache.py`（`group_member_list_cache` TTL 60s）
- 测试命令：`python tests/<文件名>.py`（不是 pytest，项目用 unittest 直接跑）
- 名字匹配策略：精确 + 子串，多人时反问，绝不自动猜（用户已确认）
- 事件缓冲不持久化（内存即可，重启清空没问题——只要"实时感知"）
- 时间相关一律走 `bot.timeutil`（刚修过的时间源）

---

## File Structure

| 文件 | 职责 | 新/改 |
|------|------|------|
| `bot/group_events.py` | 群事件内存环形缓冲 + record/build_block | **新建** |
| `bot/identity.py` | + `find_members_by_name` 名字→QQ 解析 | 改 |
| `bot/ai_tools.py` | + `find_member` 工具定义 + execute_tool 分支 + 6 个 user 类工具 description 微调 | 改 |
| `bot/handler.py` | `handle_notice` 6 个分支加 record；`_build_roster` 拼事件块 | 改 |
| `bot/personality.py` | roster 说明加 1 段事件块说明 | 改 |
| `tests/test_identity_find.py` | find_members_by_name 5 用例 | **新建** |
| `tests/test_group_events.py` | group_events 4 用例 | **新建** |

---

### Task 1: `bot/group_events.py` 事件缓冲核心模块

**Files:**
- Create: `bot/group_events.py`
- Test: `tests/test_group_events.py`

**Interfaces:**
- Produces:
  - `record(group_id: int, event_type: str, who_qq: int, who_name: str = "", detail: str = "") -> None`
  - `build_recent_events_block(group_id: int, limit: int = 5) -> str` — 空时返回 `""`
  - `clear_group(group_id: int) -> None` — 调试/重置用
  - 事件 dict 形状：`{"ts": float, "type": str, "who_qq": int, "who_name": str, "detail": str}`

- [ ] **Step 1: 写失败的测试 `tests/test_group_events.py`**

```python
"""group_events 单元测试 —— 用 python tests/test_group_events.py 直接跑"""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import group_events


class TestGroupEvents(unittest.TestCase):

    def setUp(self):
        # 每个测试清空，避免互相干扰
        group_events.clear_group(999)

    def test_record_and_build_block(self):
        """记录事件后 build_recent_events_block 返回非空、含事件信息"""
        group_events.record(999, "mute", 123456, "嘟嘟", "600s")
        block = group_events.build_recent_events_block(999)
        self.assertIn("禁言", block)
        self.assertIn("嘟嘟", block)

    def test_empty_group_returns_empty_string(self):
        """没记录过事件的群返回空串"""
        self.assertEqual(group_events.build_recent_events_block(888), "")

    def test_limit_caps_output(self):
        """limit 限制返回的事件数"""
        for i in range(10):
            group_events.record(999, "recall", 100 + i, f"用户{i}")
        block = group_events.build_recent_events_block(999, limit=3)
        # 最新的 3 条（用户9/8/7）
        self.assertIn("用户9", block)
        self.assertIn("用户7", block)
        self.assertNotIn("用户6", block)

    def test_ttl_expired_events_excluded(self):
        """超过 TTL 的事件不返回"""
        group_events.record(999, "recall", 123, "老人")
        # 篡改时间戳模拟过期
        events = group_events._events.get(999)
        if events:
            old_ts = time.time() - 3600  # 1 小时前
            events[0] = dict(events[0])
            events[0]["ts"] = old_ts
        block = group_events.build_recent_events_block(999)
        self.assertNotIn("老人", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/test_group_events.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.group_events'`

- [ ] **Step 3: 实现 `bot/group_events.py`**

```python
"""
群事件感知 —— 让 AI 知道群里刚才发生了什么
============================================
问题背景:
  handle_notice 收到撤回/禁言/踢人等通知，但以前只打日志、不喂给 AI。
  AI 对群事件"瞎"，反应慢几拍。

本模块提供内存环形缓冲（不持久化——只要实时感知），存每群最近 N 条事件，
供 _build_roster 末尾拼成「【群里最近发生的事】」注入 AI 上下文。
"""
import logging
import time
from collections import deque

logger = logging.getLogger("qqbot")

# 每群最多保留多少条事件
EVENT_KEEP = 8
# 事件保留多久（秒）—— 30 分钟外的视为过期
EVENT_TTL = 1800

# 内存状态: {group_id: deque[event_dict]}
_events: dict[int, deque] = {}

# 事件类型 → 中文描述（用于 build_block）
_TYPE_LABEL = {
    "recall": "撤回了消息",
    "mute": "被禁言",
    "unmute": "被解除禁言",
    "kick": "被踢出群",
    "leave": "退群了",
    "join": "加入了群",
}


def record(group_id: int, event_type: str, who_qq: int,
           who_name: str = "", detail: str = "") -> None:
    """记录一条群事件。

    event_type: recall / mute / unmute / kick / leave / join
    who_qq: 事件主角的 QQ（被禁言的人、撤回消息的人等）
    who_name: 主角的名字（可为空，build 时再补）
    detail: 补充信息（如禁言时长 "600s"）
    """
    if not group_id or not who_qq:
        return
    dq = _events.setdefault(group_id, deque(maxlen=EVENT_KEEP))
    dq.append({
        "ts": time.time(),
        "type": event_type,
        "who_qq": who_qq,
        "who_name": who_name or str(who_qq),
        "detail": detail,
    })


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    return f"{int(seconds // 3600)}小时前"


def build_recent_events_block(group_id: int, limit: int = 5) -> str:
    """生成「【群里最近发生的事】」提示块。无内容时返回空串。

    返回的每条形如："- 嘟嘟 (QQ=123456) 被禁言 · 600s · 3分钟前"
    按时间倒序（最新在最前），limit 限制条数。
    """
    dq = _events.get(group_id)
    if not dq:
        return ""
    now = time.time()
    lines = []
    # 从最新往回看
    for ev in reversed(dq):
        if now - ev["ts"] > EVENT_TTL:
            continue
        label = _TYPE_LABEL.get(ev["type"], ev["type"])
        name = ev["who_name"] or str(ev["who_qq"])
        age = _fmt_age(now - ev["ts"])
        detail = f" · {ev['detail']}" if ev["detail"] else ""
        lines.append(f"- {name} (QQ={ev['who_qq']}) {label}{detail} · {age}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "【群里最近发生的事】\n" + "\n".join(lines)


def clear_group(group_id: int) -> None:
    """清理某群的事件缓冲（调试/重置用）"""
    _events.pop(group_id, None)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/test_group_events.py`
Expected: 4 个测试全过

- [ ] **Step 5: 提交**

```bash
git add bot/group_events.py tests/test_group_events.py
git commit -m "feat: 新增 group_events 群事件感知缓冲模块"
```

---

### Task 2: 在 `handle_notice` 接入事件记录

**Files:**
- Modify: `bot/handler.py`（handle_notice 各分支 + import）

**Interfaces:**
- Consumes: Task 1 的 `group_events.record`
- Produces: handle_notice 现在会把撤回/禁言/踢人/退群/入群事件写入缓冲

- [ ] **Step 1: 在 handler.py 顶部 import 处加 group_events**

在已有的 `from . import ...` 区块（搜索 `from . import ai_brain` 或类似）加：

```python
from . import group_events
```

注意：handler.py 文件大，import 可能在多处延迟导入。先 grep `from \. import` 看顶部 import 块的位置，统一加到那。

- [ ] **Step 2: 改 `group_recall` 分支（handler.py:551-553）**

当前：
```python
    elif notice_type == "group_recall":
        # 消息被撤回
        pass
```

改成：
```python
    elif notice_type == "group_recall":
        # 消息被撤回 —— 记给 AI 看
        operator = event.get("operator_id", 0) or user_id
        try:
            from . import identity as _id
            op_name = _id.peek_name(group_id, operator) or str(operator)
        except Exception:
            op_name = str(operator)
        group_events.record(group_id, "recall", operator, op_name)
```

- [ ] **Step 3: 改 `group_ban` 分支（handler.py:436-444）**

当前：
```python
    elif notice_type == "group_ban":
        sub_type = event.get("sub_type", "")
        duration = event.get("duration", 0)
        target_qq = event.get("user_id", 0)
        if sub_type == "ban":
            if duration > 0:
                logger.info(f"群{group_id} {target_qq} 被禁言 {duration}s")
        elif sub_type == "lift_ban":
            logger.info(f"群{group_id} {target_qq} 被解除禁言")
```

改成（保留日志，加 record）：
```python
    elif notice_type == "group_ban":
        sub_type = event.get("sub_type", "")
        duration = event.get("duration", 0)
        target_qq = event.get("user_id", 0)
        try:
            from . import identity as _id
            tgt_name = _id.peek_name(group_id, target_qq) or str(target_qq)
        except Exception:
            tgt_name = str(target_qq)
        if sub_type == "ban":
            if duration > 0:
                logger.info(f"群{group_id} {target_qq} 被禁言 {duration}s")
                group_events.record(group_id, "mute", target_qq, tgt_name,
                                    detail=f"{duration}s")
        elif sub_type == "lift_ban":
            logger.info(f"群{group_id} {target_qq} 被解除禁言")
            group_events.record(group_id, "unmute", target_qq, tgt_name)
```

- [ ] **Step 4: 改 `group_decrease` 分支（handler.py:408-434）**

在 `sub_type == "kick" / "kick_me"` 分支（handler.py:423-425）加 record：
当前：
```python
        if sub_type == "kick" or sub_type == "kick_me":
            # 被踢
            logger.info(f"群{group_id} {nickname}({target_qq}) 被踢出")
        else:
```

改成：
```python
        if sub_type == "kick" or sub_type == "kick_me":
            # 被踢
            logger.info(f"群{group_id} {nickname}({target_qq}) 被踢出")
            group_events.record(group_id, "kick", target_qq, nickname)
        else:
```

在主动退群分支（发完 leave_msg 后，handler.py:432-434）加：
```python
            group_events.record(group_id, "leave", target_qq, nickname)
```

插在 `logger.info(f"群{group_id} 成员 {nickname}({target_qq}) 退群")` 这行后面（在 except 块外，最稳的做法是放在整个 try/except 之后——参照实际代码块边界，先 Read 确认）。

- [ ] **Step 5: 改 `group_increase` 分支（handler.py:358-406）**

在拿到 `nickname` 之后（handler.py:363-371 那段 try 后面）加：
```python
        group_events.record(group_id, "join", target_qq, nickname)
```

放在 `nickname` 解析完成之后、黑名单检查之前——这样无论后续流程是踢黑名单、verify 接管、还是发欢迎，事件都已记录。

- [ ] **Step 6: 运行现有测试确认没破坏**

Run: `python tests/test_group_events.py`
Expected: 仍然全过（没改 group_events 本体）

并跑一遍 import 自检：
Run: `python -c "from bot import handler; print('ok')"`
Expected: 输出 `ok`（import 链没断）

- [ ] **Step 7: 提交**

```bash
git add bot/handler.py
git commit -m "feat: handle_notice 把撤回/禁言/踢人/退群/入群事件喂给 group_events"
```

---

### Task 3: 在 `_build_roster` 末尾拼接事件块

**Files:**
- Modify: `bot/handler.py:152-174`（`_build_roster` 函数）

**Interfaces:**
- Consumes: Task 1 的 `group_events.build_recent_events_block`
- Produces: 每次给 AI 的 roster 字符串末尾会带「【群里最近发生的事】」块（有事件时）

- [ ] **Step 1: 改 `_build_roster`，在 return 前拼事件块**

当前（handler.py:152-174）末尾：
```python
    try:
        return await _get_identity().build_roster(
            bot, group_id,
            speaker_qq=user_id, speaker_name=user_name,
            mentioned_qqs=mentioned,
            quoted_qq=quoted_qq, quoted_name=quoted_name,
            bot_qq=bot_qq,
        )
    except Exception as e:
        logger.debug(f"花名册构建失败: {e}")
        return f"【正在跟你说话的人】{user_name} (QQ={user_id})"
```

改成（先拿 roster，再拼事件块）：
```python
    try:
        roster = await _get_identity().build_roster(
            bot, group_id,
            speaker_qq=user_id, speaker_name=user_name,
            mentioned_qqs=mentioned,
            quoted_qq=quoted_qq, quoted_name=quoted_name,
            bot_qq=bot_qq,
        )
    except Exception as e:
        logger.debug(f"花名册构建失败: {e}")
        roster = f"【正在跟你说话的人】{user_name} (QQ={user_id})"

    # 拼接群事件感知块（有事件时才加）
    try:
        events_block = group_events.build_recent_events_block(group_id)
        if events_block:
            roster = f"{roster}\n{events_block}" if roster else events_block
    except Exception:
        pass
    return roster
```

- [ ] **Step 2: import 自检**

Run: `python -c "from bot import handler; print('ok')"`
Expected: `ok`

- [ ] **Step 3: 提交**

```bash
git add bot/handler.py
git commit -m "feat: _build_roster 末尾拼接群事件感知块"
```

---

### Task 4: `bot/personality.py` 加事件块说明

**Files:**
- Modify: `bot/personality.py`（roster 说明附近）

- [ ] **Step 1: 找到 roster 说明的位置**

Grep: `grep -n "在场的人" bot/personality.py`
定位两处：约 personality.py:88 和 personality.py:197。

- [ ] **Step 2: 在每处 roster 说明后面加 1 段事件块说明**

在「在场的人」清单说明的紧后一行，加：

```
- 每条消息前可能还会附一份「【群里最近发生的事】」清单，记录最近的撤回/禁言/踢人/退群。
  有人问"刚才怎么了/谁被禁了"时对照这份回答；没人问就自然带过，别主动念。
```

两处都加（一份给群友模式提示词、一份给管理模式）。

- [ ] **Step 3: 提交**

```bash
git add bot/personality.py
git commit -m "docs: personality 提示词加群事件感知块说明"
```

---

### Task 5: `bot/identity.py` 加 `find_members_by_name`

**Files:**
- Modify: `bot/identity.py`
- Test: `tests/test_identity_find.py`

**Interfaces:**
- Consumes: `bot.get_group_member_list(group_id)`（已有，返回成员 dict 列表）
- Produces:
  - `find_members_by_name(bot, group_id: int, query: str) -> list[dict]`
  - 返回 dict 形状：`{"qq": int, "card": str, "nickname": str, "role": str, "match_type": str}`
  - match_type ∈ `"exact"` / `"substring"`，按 exact 优先排序，封顶 5 个

- [ ] **Step 1: 写失败的测试 `tests/test_identity_find.py`**

```python
"""find_members_by_name 单元测试 —— 用 python tests/test_identity_find.py 直接跑"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import identity


def _fake_bot(members):
    """构造一个假 bot，get_group_member_list 返回 members"""
    bot = MagicMock()
    bot.get_group_member_list = AsyncMock(return_value=members)
    return bot


MEMBERS = [
    {"user_id": 111, "card": "嘟嘟", "nickname": "dd", "role": "member"},
    {"user_id": 222, "card": "小嘟嘟", "nickname": "嘟嘟大人", "role": "member"},
    {"user_id": 333, "card": "", "nickname": "嘟嘟", "role": "admin"},
    {"user_id": 444, "card": "阿强", "nickname": "强哥", "role": "owner"},
    {"user_id": 555, "card": "DU", "nickname": "DuDu", "role": "member"},
]


class TestFindMembers(unittest.TestCase):

    def test_exact_match_card(self):
        """精确匹配群名片"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res if r["match_type"] == "exact"]
        self.assertIn(111, qqs)  # card 完全等于"嘟嘟"

    def test_exact_match_nickname(self):
        """精确匹配 QQ 昵称"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res if r["match_type"] == "exact"]
        self.assertIn(333, qqs)  # nickname 完全等于"嘟嘟"

    def test_substring_match(self):
        """子串匹配：'嘟嘟' 命中 '小嘟嘟' 的 card 和 '嘟嘟大人' 的 nickname"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        qqs = [r["qq"] for r in res]
        self.assertIn(222, qqs)

    def test_exact_sorted_before_substring(self):
        """exact 排在 substring 前面"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "嘟嘟"))
        # 第一个应该是 exact
        self.assertEqual(res[0]["match_type"], "exact")

    def test_zero_match(self):
        """查不到返回空列表"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "不存在的名字"))
        self.assertEqual(res, [])

    def test_case_insensitive(self):
        """英文大小写无关：'dudu' 命中 'DuDu'"""
        bot = _fake_bot(MEMBERS)
        res = asyncio.run(identity.find_members_by_name(bot, 1, "dudu"))
        qqs = [r["qq"] for r in res]
        self.assertIn(555, qqs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/test_identity_find.py`
Expected: FAIL with `AttributeError: module 'bot.identity' has no attribute 'find_members_by_name'`

- [ ] **Step 3: 在 `bot/identity.py` 末尾加 `find_members_by_name`**

在文件末尾（`clear_group` 函数之后）加：

```python
# ═══════════════════════════════════════════════════════════
#  名字 → QQ 解析（让 AI 能按名字找人）
# ═══════════════════════════════════════════════════════════

_FIND_LIMIT = 5  # 最多返回几个候选


async def find_members_by_name(bot, group_id: int, query: str) -> list[dict]:
    """按群名片/QQ 昵称查群成员。

    匹配策略（用户已确认）：
    - 第一轮：card 或 nickname 完全等于 query → match_type="exact"
    - 第二轮：query 是 card 或 nickname 的子串 → match_type="substring"
    - 大小写无关（英文场景）
    - 按 exact 优先排序，封顶 _FIND_LIMIT 个

    返回 [{"qq", "card", "nickname", "role", "match_type"}]，空列表=没找到
    """
    query = (query or "").strip().lower()
    if not query:
        return []

    try:
        members = await bot.get_group_member_list(group_id)
    except Exception as e:
        logger.warning(f"find_members_by_name 拉成员列表失败: {e}")
        return []

    exact_hits: list[dict] = []
    sub_hits: list[dict] = []

    for m in members or []:
        qq = int(m.get("user_id", 0) or 0)
        if not qq:
            continue
        card = (m.get("card", "") or "").strip()
        nick = (m.get("nickname", "") or "").strip()
        role = m.get("role", "member")

        if query == card.lower() or query == nick.lower():
            exact_hits.append({
                "qq": qq, "card": card, "nickname": nick,
                "role": role, "match_type": "exact",
            })
        elif query in card.lower() or query in nick.lower():
            sub_hits.append({
                "qq": qq, "card": card, "nickname": nick,
                "role": role, "match_type": "substring",
            })

    # exact 优先，封顶
    return (exact_hits + sub_hits)[:_FIND_LIMIT]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/test_identity_find.py`
Expected: 6 个测试全过

- [ ] **Step 5: 提交**

```bash
git add bot/identity.py tests/test_identity_find.py
git commit -m "feat: identity.find_members_by_name 按名字查群成员 QQ"
```

---

### Task 6: `bot/ai_tools.py` 加 `find_member` 工具定义 + execute_tool 分支

**Files:**
- Modify: `bot/ai_tools.py`（ALL_TOOLS 列表 + execute_tool 函数）

**Interfaces:**
- Consumes: Task 5 的 `identity.find_members_by_name`
- Produces: AI 可调用的新工具 `find_member(name) -> str`

- [ ] **Step 1: 在 `ALL_TOOLS` 列表里加 `find_member` 工具定义**

在 `get_member_info` 工具（ai_tools.py:76 附近）后面，加一个新工具：

```python
    {
        "name": "find_member",
        "description": (
            "按名字（群名片或昵称）查群成员的 QQ 号。"
            "用户只给名字（'封禁嘟嘟'/'给那个谁发私聊'/'@一下小王'）时，先用这个查 QQ，再调具体动作工具。"
            "命中多人会返回候选清单——这时要反问用户确认是哪一个，不要自己猜、不要随便选一个就执行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要查的名字（群名片或昵称的片段）"},
            },
            "required": ["name"],
        },
    },
```

- [ ] **Step 2: 在 `execute_tool` 里加 `find_member` 分支**

在 `elif tool_name == "get_member_info":` 分支（ai_tools.py:749）之前或之后，加：

```python
        elif tool_name == "find_member":
            name = str(tool_input.get("name", "")).strip()
            if not name:
                return "要查的名字不能为空"
            from . import identity
            hits = await identity.find_members_by_name(bot, group_id, name)
            if not hits:
                return f"群里没找到叫「{name}」的人"
            if len(hits) == 1:
                m = hits[0]
                display = m["card"] or m["nickname"] or str(m["qq"])
                return f"找到了：{display} (QQ={m['qq']})"
            # 多个候选：列出来让 AI 反问
            lines = []
            for m in hits:
                display = m["card"] or m["nickname"] or str(m["qq"])
                tag = "精确" if m["match_type"] == "exact" else "模糊"
                role_cn = {"owner": "群主", "admin": "管理员", "member": ""}.get(m["role"], "")
                role_str = f" 角色:{role_cn}" if role_cn else ""
                lines.append(f"- {display} (QQ={m['qq']}, {tag}{role_str})")
            return f"查到 {len(hits)} 个叫「{name}」的人，重名，请让用户指明具体是哪个 QQ：\n" + "\n".join(lines)
```

- [ ] **Step 3: 微调 6 个 user 类工具的 description**

在 `ban_member`、`kick_member`、`at_member`、`send_private`、`set_member_card`、`set_member_title` 这 6 个工具的 `description` 末尾，各加一句：

```
只有名字、没有 QQ 号时，先调 find_member 查到 QQ 再调用本工具。
```

- [ ] **Step 4: import 自检**

Run: `python -c "from bot import ai_tools; print('tools:', len(ai_tools.ALL_TOOLS))"`
Expected: `tools:` 加 1 个（比之前多 1）

Run: `python -c "from bot.ai_tools import execute_tool; print('ok')"`
Expected: `ok`

- [ ] **Step 5: 提交**

```bash
git add bot/ai_tools.py
git commit -m "feat: 新增 find_member 工具，AI 可按名字查群成员 QQ"
```

---

### Task 7: 全量验收

- [ ] **Step 1: 跑全部新增/相关测试**

Run:
```bash
python tests/test_group_events.py
python tests/test_identity_find.py
python tests/test_timeutil.py
```
Expected: 三个文件全过（test_timeutil 是上次改的，回归确认没破坏）

- [ ] **Step 2: import 全链路自检**

Run:
```bash
python -c "from bot import handler, ai_tools, identity, group_events, consciousness; print('all imports ok')"
```
Expected: `all imports ok`

- [ ] **Step 3: 工具完整性自检**

Run:
```bash
python -c "from bot.ai_tools import ALL_TOOLS; names=[t['name'] for t in ALL_TOOLS]; assert 'find_member' in names; print('find_member in tools, total', len(names))"
```
Expected: `find_member in tools, total <N>`

- [ ] **Step 4: 最终提交（如有遗漏的格式化/补丁）**

```bash
git add -A
git commit -m "chore: 群感知特性收尾" --allow-empty
```
（这步可选——如果前面 6 个 Task 都已提交且无残留改动，跳过即可）

---

## Self-Review 结果

**1. Spec 覆盖检查：**
- ✅ A1 名字匹配策略（精确+子串、多人反问）→ Task 5 + Task 6（execute_tool N 命中分支反问）
- ✅ A2 find_members_by_name → Task 5
- ✅ A2 find_member 工具定义 → Task 6
- ✅ A2 6 个工具 description 微调 → Task 6 Step 3
- ✅ B1 group_events 模块 → Task 1
- ✅ B2 handle_notice 6 个分支 → Task 2
- ✅ B3 _build_roster 拼接 → Task 3
- ✅ B4 personality 提示词 → Task 4
- ✅ 测试 → Task 1（4 用例）+ Task 5（6 用例）

**2. 占位扫描：** 无 TBD/TODO，每个步骤都有具体代码。

**3. 类型一致：** `record` 签名（Task 1 产出）与 Task 2 调用一致；`find_members_by_name` 签名（Task 5 产出）与 Task 6 调用一致；事件 dict 字段（ts/type/who_qq/who_name/detail）在 Task 1 产出和 Task 2 使用一致。

**4. 命名一致：** `group_events` / `find_members_by_name` / `find_member` / `build_recent_events_block` 在所有 Task 中一致。

