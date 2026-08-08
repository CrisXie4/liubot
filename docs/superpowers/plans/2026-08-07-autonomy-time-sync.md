# QQ Bot 自主性 + 时间同步 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 bot 的自主性最大化（分级异步裁判闸门 + AI 自然语言指令 + 主动设精华/传群文件/@全体），并用权威 HTTP 时间源同步上海时间，消除"搞错时间"。

**Architecture:** (A) 新增 `bot/timeutil.py`，从 10 个 HTTP 源随机抽 3 同步偏移量，全项目 10 个关键时间点改走它。(B) `bot/guardian.py` 升级为通用分级裁判（autonomous/light/heavy/exempt），AI 工具破坏性动作统一进闸门。(C) `bot/ai_tools.py` 新增群文件/改昵称工具并路由进 guardian。(D) `bot/handler.py` 删 7 个关键词指令，改 AI 自然语言理解 + `master_instructed` 标记。(E) `main.py consciousness_loop` 加主动设精华/重大新闻@全体 tick。(F) 清理思考模式残留配置 + Web UI。

**Tech Stack:** Python 3.14, aiohttp（已有，HTTP 时间源）, anthropic SDK（已有）, ZoneInfo + tzdata（新增）, SQLite（已有，审计表）。

**对应 spec:** `docs/superpowers/specs/2026-08-07-autonomy-design.md`

## Global Constraints

- 模型固定 `glm-5.2-free @ https://api.ccode.vip`，**不做思考模式**（已实测不可用）。
- 依赖只能加 `tzdata>=2024.1`（Windows 上 `ZoneInfo` 需要）；HTTP 用已有 `aiohttp`，不引入 `ntplib`/`pytz`/`arrow`。
- LLM 调用极慢（57–98s/次），所以裁判必须异步并先发"让我想想…"占位文案。
- 保护名单（主人 QQ + 群主 + 群管理员 + 白名单 QQ）是硬护栏；`master_instructed=True` 时除主人自身外可被覆盖。
- **不重构**心情引擎（`consciousness.py` 情绪部分）、news 管线、influence 体系——已成熟，只接钩子。
- 纯审计时间戳（`db.py` × 11、`verify.py`、`trainer.py`、`sticker.py`、`learner.py`、`group_personality.py`、`media_reader.py`、`llm_mod.py`、`guardian.py:130`、`consciousness.py` 日记/关系类时间戳）**不改**——它们只记日志，不影响"搞错时间"或自主性。
- 代码风格：保持现有中文注释 + 模块结构，不加 `# mypy` / type stub，不引入 `pytest`（项目无测试框架，验证靠 smoke test）。
- 提交规范：`feat:`/`fix:`/`refactor:` 前缀，简短中文描述。

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `bot/timeutil.py` | 权威上海时间源 + `now_shanghai()`/`today_shanghai()` | **新建** |
| `bot/guardian.py` | 通用分级裁判闸门（保护名单/分级/异步裁判/限速/审批队列） | 大改 |
| `bot/ai_tools.py` | 57→62 工具；`execute_tool` 路由进 guardian；新增 upload_group_file/create_group_folder/set_member_card/set_member_title | 中改 |
| `bot/handler.py` | 删 7 关键词块；接 master_instructed 标记 | 中改 |
| `main.py` | consciousness_loop 用 timeutil；加自主设精华/重大新闻@全体 tick | 小改 |
| `bot/consciousness.py` | get_time_facts_prompt/circadian/quiet/daily_reset 用 timeutil | 小改 |
| `bot/personality.py` | get_time_context 用 timeutil | 1 行改 |
| `bot/ai_brain.py` | sync_qq_status/maybe_proactive_action 用 timeutil | 3 处改 |
| `bot/summary.py` | generate_group_summary 标题用 timeutil | 1 处改 |
| `bot/scheduler.py` | _run_schedule 用 timeutil | 1 处改 |
| `bot/welcome.py` | do_checkin 用 timeutil | 3 处改 |
| `bot/db.py` | 加 `ai_audit` 表 + 读写函数 | 小改 |
| `bot/config.py` | 删 `LLM_THINKING_EFFORT` | 1 行删 |
| `bot/memory.py` | DEFAULT_MODEL_SETTINGS 删 `thinking_effort` | 1 行删 |
| `web/api.py` | 移除 `thinking_effort` 允许字段；加 ai_guard_config 读写 | 小改 |
| `web/static/pages/model.html` + `js/model.js` | 删思考强度 UI | 小改 |
| `requirements.txt` | 加 `tzdata>=2024.1` | 1 行加 |
| `tests/test_timeutil.py` | timeutil 单元测试（手工跑，不引 pytest） | **新建** |

---

## Task 1: 时间同步 — `bot/timeutil.py` 核心模块

**Files:**
- Create: `bot/timeutil.py`
- Create: `tests/test_timeutil.py`
- Modify: `requirements.txt`（加 `tzdata>=2024.1`）

**Interfaces:**
- Produces:
  - `now_shanghai() -> datetime`（tz-aware, Asia/Shanghai）— 返回系统时钟 + 已缓存的权威偏移；缓存未就绪时返回系统时钟（带 tz）
  - `today_shanghai() -> date`
  - `async sync_once() -> tuple[Optional[timedelta], str]` — 单次同步，返回 (偏移量, 状态描述)；偏移量 None 表示失败
  - `async start_background_sync()` — 启动后台同步任务（启动后立即一次 + 每 30min 一次）
  - `get_sync_status() -> dict` — 给 Web UI / 日志看同步状态
  - 全局状态：`_offset: timedelta | None`（None=未同步过）、`_last_sync_ts: float`、`_last_sources: list[str]`、`_sync_count: int`、`_fail_count: int`

- [ ] **Step 1: 加依赖 `tzdata`**

修改 `requirements.txt`，在末尾追加：

```
tzdata>=2024.1
```

- [ ] **Step 2: 写 `bot/timeutil.py` 骨架 + 常量**

创建 `bot/timeutil.py`：

```python
"""
权威上海时间源
==============
从国内/国际 HTTP 源拉 Date 头，算系统时钟与权威时间的偏移，
保证部署到错误 TZ/漂移的机器上 bot 仍然知道真实上海时间。

启动时 + 每 30min 同步一次。每次从 10 个源里随机抽 3 个并发测，
取多数一致值。全失败则保留上次偏移；从未成功过则退回系统时钟。
"""
import asyncio
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 10 个 HTTP 时间源（返回 GMT Date 头，+8 = 上海时间）
_TIME_SOURCES = [
    "https://www.baidu.com",
    "https://www.qq.com",
    "https://www.taobao.com",
    "https://www.microsoft.com",
    "https://www.apple.com",
    "https://www.bing.com",
    "https://github.com",
    "https://www.cloudflare.com",
    "https://www.alibaba.com",
    "https://www.sohu.com",
]

_SYNC_INTERVAL = 1800          # 30 分钟
_SAMPLE_SIZE = 3               # 每次随机抽 3 个
_SOURCE_TIMEOUT = 8            # 单源超时 8s
_MAX_TOLERANCE = timedelta(seconds=2)  # 源间最大允许偏差；超差则放弃本次

# 全局状态
_offset: Optional[timedelta] = None     # authoritative - system；None=未同步过
_last_sync_ts: float = 0.0
_last_sources: list[str] = []
_sync_count: int = 0
_fail_count: int = 0

logger = logging.getLogger("qqbot")


def now_shanghai() -> datetime:
    """返回权威上海时间（tz-aware）。缓存未就绪时退回系统时钟。"""
    now = datetime.now(SHANGHAI)
    if _offset is not None:
        return now + _offset
    return now


def today_shanghai() -> date:
    """返回权威上海日期"""
    return now_shanghai().date()


def get_sync_status() -> dict:
    return {
        "synced": _offset is not None,
        "offset_seconds": _offset.total_seconds() if _offset else 0.0,
        "last_sync_ts": _last_sync_ts,
        "last_sources": _last_sources,
        "sync_count": _sync_count,
        "fail_count": _fail_count,
    }


async def _fetch_source_time(url: str, session) -> Optional[datetime]:
    """从单个源拉 HTTP Date 头，返回 tz-aware UTC datetime；失败返回 None"""
    try:
        async with session.head(
            url, timeout=_SOURCE_TIMEOUT, allow_redirects=True,
            ssl=False,
        ) as resp:
            date_hdr = resp.headers.get("Date") or resp.headers.get("date")
            if not date_hdr:
                return None
            return parsedate_to_datetime(date_hdr)  # tz-aware UTC
    except Exception as e:
        logger.debug(f"timeutil 源 {url} 失败: {e}")
        return None


async def sync_once() -> tuple[Optional[timedelta], str]:
    """同步一次。返回 (偏移量, 状态描述)。偏移量 None = 失败。"""
    import aiohttp

    global _offset, _last_sync_ts, _last_sources, _sync_count, _fail_count

    sampled = random.sample(_TIME_SOURCES, min(_SAMPLE_SIZE, len(_TIME_SOURCES)))
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_source_time(u, session) for u in sampled]
        )

    # 过滤失败的
    pairs = [(u, t) for u, t in zip(sampled, results) if t is not None]
    if not pairs:
        _fail_count += 1
        return None, "all_sources_failed"

    # 系统当前 UTC 时间
    sys_utc = datetime.now(timezone.utc)
    # 每个源算出的偏移 = 源时间 - 系统时间
    offsets = [(u, t - sys_utc) for u, t in pairs]

    # 取多数一致：把偏移量化到秒，找出现次数最多的
    by_sec = {}
    for u, off in offsets:
        sec = round(off.total_seconds())
        by_sec.setdefault(sec, []).append((u, off))

    # 选票数最多且与第二差距满足多数的
    best_sec = max(by_sec, key=lambda s: len(by_sec[s]))
    winners = by_sec[best_sec]

    # 若多数组只有 1 个源、且源间偏差 > 容差，判为不一致
    if len(winners) == 1 and len(offsets) > 1:
        secs = [round(o.total_seconds()) for _, o in offsets]
        if max(secs) - min(secs) > _MAX_TOLERANCE.total_seconds():
            _fail_count += 1
            return None, "sources_disagree"

    consensus_offset = winners[0][1]
    _offset = consensus_offset
    _last_sync_ts = time.time()
    _last_sources = [u for u, _ in winners]
    _sync_count += 1
    logger.info(
        f"timeutil 同步成功: 偏移={consensus_offset.total_seconds():+.1f}s "
        f"来源={_last_sources}"
    )
    return consensus_offset, "ok"


async def start_background_sync():
    """启动后台同步：启动后立即一次 + 每 30min 一次。永不退出。"""
    # 立即同步一次（启动后尽快拿到准确时间）
    try:
        off, status = await sync_once()
        if off is None:
            logger.warning(f"timeutil 启动同步失败: {status}，退回系统时钟")
    except Exception as e:
        logger.warning(f"timeutil 启动同步异常: {e}")

    while True:
        await asyncio.sleep(_SYNC_INTERVAL)
        try:
            await sync_once()
        except Exception as e:
            logger.debug(f"timeutil 定期同步异常: {e}")
```

- [ ] **Step 3: 写单元测试 `tests/test_timeutil.py`**

项目没有 pytest，所以测试写成可 `python tests/test_timeutil.py` 直接跑的脚本（用标准库 `unittest`）。

创建 `tests/test_timeutil.py`：

```python
"""timeutil 单元测试 —— 用 python tests/test_timeutil.py 直接跑"""
import asyncio
import sys
import os
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import timeutil


class TestTimeUtil(unittest.TestCase):

    def test_now_shanghai_returns_tz_aware(self):
        """now_shanghai 返回的时间必须带 Asia/Shanghai tz"""
        from zoneinfo import ZoneInfo
        t = timeutil.now_shanghai()
        self.assertIsNotNone(t.tzinfo)
        self.assertEqual(t.tzinfo, ZoneInfo("Asia/Shanghai"))

    def test_offset_applied(self):
        """设了 _offset 后，now_shanghai = 系统时钟 + offset"""
        before = timeutil.now_shanghai()
        fake_offset = timedelta(seconds=42)
        with patch.object(timeutil, "_offset", fake_offset):
            after = timeutil.now_shanghai()
        # 应该比无 offset 时大约多 42 秒（允许 5 秒执行抖动）
        diff = (after - before).total_seconds()
        self.assertGreater(diff, 37, f"offset 未生效，diff={diff}")
        self.assertLess(diff, 47, f"offset 偏差过大，diff={diff}")

    def test_offset_none_uses_system_clock(self):
        """_offset=None 时不崩，退回系统时钟"""
        with patch.object(timeutil, "_offset", None):
            t = timeutil.now_shanghai()
        self.assertIsNotNone(t)

    def test_today_shanghai(self):
        d = timeutil.today_shanghai()
        self.assertIsNotNone(d)

    def test_sync_status_shape(self):
        s = timeutil.get_sync_status()
        self.assertIn("synced", s)
        self.assertIn("offset_seconds", s)
        self.assertIn("last_sources", s)
        self.assertIsInstance(s["sync_count"], int)

    def test_source_list_has_10(self):
        self.assertEqual(len(timeutil._TIME_SOURCES), 10)

    def test_sample_size_within_bounds(self):
        self.assertLessEqual(timeutil._SAMPLE_SIZE, len(timeutil._TIME_SOURCES))


class TestSyncOnce(unittest.TestCase):

    def test_all_sources_fail_returns_none(self):
        """所有源失败时返回 (None, 'all_sources_failed')"""
        async def fake_fetch(url, session):
            return None
        with patch.object(timeutil, "_fetch_source_time", fake_fetch):
            off, status = asyncio.run(timeutil.sync_once())
        self.assertIsNone(off)
        self.assertEqual(status, "all_sources_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 4: 跑测试确认核心逻辑通过**

Run: `python tests/test_timeutil.py`
Expected: 全部 PASS（`test_all_sources_fail_returns_none` 不依赖网络；其余纯逻辑）。如果系统没装 tzdata 会先装上。

- [ ] **Step 5: 手动验证同步真跑一次（联网）**

```python
python -c "import asyncio; from bot import timeutil; off,st=asyncio.run(timeutil.sync_once()); print('offset',off,'status',st); print('status',timeutil.get_sync_status())"
```
Expected: `offset timedelta(...)` 近 0（系统时钟对的话），`status 'ok'`，`synced True`。

- [ ] **Step 6: 安装 tzdata 依赖**

Run: `python -m pip install tzdata`
Expected: 安装成功（Windows 上 `ZoneInfo` 需要它）。

- [ ] **Step 7: Commit**

```bash
git add bot/timeutil.py tests/test_timeutil.py requirements.txt
git commit -m "feat: 新增 timeutil 权威上海时间源（10源随机抽样同步）"
```

---

## Task 2: 启动时间同步后台任务

**Files:**
- Modify: `main.py`（在 `main()` 启动后、连 WS 前启动 timeutil 后台同步）

**Interfaces:**
- Consumes: `bot.timeutil.start_background_sync`（Task 1）

- [ ] **Step 1: 找到 main() 里连 WS 之前的位置**

Read `main.py:215-238`（scheduler 启动后、consciousness_loop 创建前后）。在 scheduler 启动之后、consciousness_loop 创建之前插入 timeutil 后台任务启动。

- [ ] **Step 2: 加导入 + 启动调用**

在 `main.py` 顶部 import 区找现有 `from bot import ...` 块，追加：

```python
from bot import timeutil
```

在 `main()` 里 scheduler 启动（约 `main.py:217-221`）之后、`consciousness_loop` 定义之前，加：

```python
    # 启动权威时间同步（后台：启动时立即同步 + 每 30min 一次）
    asyncio.create_task(timeutil.start_background_sync())
    logger.info("启动时间同步后台任务（10 源随机抽样，每 30min 同步）")
```

- [ ] **Step 3: 手动验证**

Run: `python main.py`（让它跑 10 秒后 Ctrl-C）
Expected: 日志里看到 `timeutil 同步成功: 偏移=...s 来源=[...]` 和 `启动时间同步后台任务...`。无异常。

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: 启动时拉起 timeutil 后台时间同步"
```

---

## Task 3: 改 10 个关键时间点用 timeutil

**Files:**
- Modify: `bot/consciousness.py:245,134-135,182,195,297,975`
- Modify: `bot/personality.py:254`
- Modify: `bot/ai_brain.py:92,553`
- Modify: `bot/summary.py:30`
- Modify: `bot/scheduler.py:89`
- Modify: `bot/welcome.py:113,127,171`
- Modify: `main.py:251-253,347,351`

**Interfaces:**
- Consumes: `bot.timeutil.now_shanghai` / `today_shanghai`

策略：每个文件顶部加 `from . import timeutil`（或 main.py 加 `from bot import timeutil`），把 `datetime.now()` → `timeutil.now_shanghai()`。注意：`consciousness.py` 已有 `from datetime import datetime`，timeutil 返回的也是 datetime 实例，所以 `.hour`/`.weekday()`/`.strftime()` 全兼容。

- [ ] **Step 1: `bot/consciousness.py` — 6 处**

加导入（文件顶部 `from datetime import datetime` 旁）：

```python
from . import timeutil
```

改 `consciousness.py:245`（`get_time_facts_prompt`）：

```python
    now = timeutil.now_shanghai()
```
（原 `now = datetime.now()`）

改 `consciousness.py:134-135`（`get_circadian_modifiers`）：

```python
    now = timeutil.now_shanghai()
    hour = now.hour
    weekday = now.weekday()  # 0=周一
```
（原 `hour = datetime.now().hour` / `weekday = datetime.now().weekday()`）

改 `consciousness.py:182`（`get_time_of_day_name`）：

```python
    h = timeutil.now_shanghai().hour
```

改 `consciousness.py:195`（`get_circadian_prompt`）：

```python
    h = timeutil.now_shanghai().hour
```

改 `consciousness.py:297`（`is_quiet_hours`）：

```python
    h = timeutil.now_shanghai().hour
```

改 `consciousness.py:975`（`check_daily_reset`）：

```python
    today_str = timeutil.now_shanghai().strftime("%Y-%m-%d")
```

- [ ] **Step 2: `bot/personality.py:254`**

```python
def get_time_context() -> str:
    """获取当前时间上下文（动态信息，通过用户消息注入以保持系统提示词可缓存）"""
    from . import timeutil
    return timeutil.now_shanghai().strftime("%Y年%m月%d日 %H:%M")
```
（删掉原 `from datetime import datetime`，改用 timeutil）

- [ ] **Step 3: `bot/ai_brain.py:92,553`**

文件顶部找 `from datetime import datetime`，旁加 `from . import timeutil`。

改 `ai_brain.py:92`（`sync_qq_status`）：

```python
    hour = timeutil.now_shanghai().hour
```

改 `ai_brain.py:553`（`maybe_proactive_action`）：

```python
                messages=[{"role": "user", "content": (
                    f"现在是{hour}点，{'周末' if timeutil.now_shanghai().weekday() >= 5 else '工作日'}，"
                    "随口发一条，内容要跟这个时间对得上"
                )}],
```

（`hour` 变量在 `ai_brain.py:530`，那里也要改：`hour = timeutil.now_shanghai().hour`）

- [ ] **Step 4: `bot/summary.py:30`**

文件顶部找 `from datetime import datetime`，旁加 `from . import timeutil`。

```python
    now = timeutil.now_shanghai()
```

- [ ] **Step 5: `bot/scheduler.py:89`**

文件顶部找 `from datetime import datetime`，旁加 `from . import timeutil`。

```python
                    now = timeutil.now_shanghai()
```

- [ ] **Step 6: `bot/welcome.py:113,127,171`**

文件顶部加 `from . import timeutil`。

把 3 处 `date.today()` → `timeutil.today_shanghai()`：

```python
    today = timeutil.today_shanghai().isoformat()
```
（welcome.py:113、171 各一处；welcome.py:127 的 `yesterday = date.today()` → `yesterday = timeutil.today_shanghai()`，看上下文是否需要减一天——读上下文确认）

- [ ] **Step 7: `main.py:251-253,347,351`**

改 `main.py:251`：

```python
            now = timeutil.now_shanghai()
            today = now.strftime("%Y%m%d")
            current_hour = now.hour
```

改 `main.py:347`（`today_str`）：

```python
            today_str = now.strftime('%Y-%m-%d')
```
（now 已是 timeutil 时间，无需再改）

改 `main.py:351`（`_date.today().day % 2`）：

```python
                if timeutil.today_shanghai().day % 2 == 0:
```
（删掉原 `from datetime import date as _date` 这行）

- [ ] **Step 8: 跑 timeutil 测试确认没破坏**

Run: `python tests/test_timeutil.py`
Expected: 全 PASS。

- [ ] **Step 9: 手动启动 bot 跑 30 秒验证**

Run: `python main.py`（30 秒后 Ctrl-C）
Expected:
- 日志出现 `timeutil 同步成功`
- bot 不报 import 错误
- 触发的定时任务时间正确

- [ ] **Step 10: Commit**

```bash
git add bot/consciousness.py bot/personality.py bot/ai_brain.py bot/summary.py bot/scheduler.py bot/welcome.py main.py
git commit -m "feat: 10 个关键时间点改走 timeutil 权威上海时间"
```

---

## Task 4: 审计表 — `bot/db.py` 加 `ai_audit` 表

**Files:**
- Modify: `bot/db.py`（加表 + 写入 + 查询函数）

**Interfaces:**
- Produces:
  - `db.record_ai_audit(group_id, action, target, reason, verdict, master_instructed)` — 返回审计 id
  - `db.list_ai_audit(target_qq=None, action=None, limit=50, only_undone=False)` — 主人撤销时查
  - `db.mark_ai_audit_undone(audit_id)` — 标记已撤销
- Consumes: 现有 `db._conn()` / `db._execute()` 模式（看 db.py 顶部现有表初始化怎么写）

- [ ] **Step 1: 看 db.py 现有表初始化模式**

Read `bot/db.py` 找现有的 `CREATE TABLE IF NOT EXISTS` 块（应在初始化函数里）。看现有表的列约定（timestamps 用 `strftime("%Y-%m-%d %H:%M:%S")`）。

- [ ] **Step 2: 加表 + 索引**

在 db.py 表初始化区追加：

```python
CREATE TABLE IF NOT EXISTS ai_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                       -- %Y-%m-%d %H:%M:%S（系统时间，仅审计用）
    group_id INTEGER NOT NULL,
    action TEXT NOT NULL,                   -- recall/ban/kick/mute/whole_ban/essence/at_all/...
    target_qq INTEGER,                      -- 目标 QQ（无目标时 NULL，如 whole_ban）
    reason TEXT,
    judge_verdict TEXT,                     -- allow/deny/light/exempt/master_instructed
    master_instructed INTEGER DEFAULT 0,    -- 0/1
    undone INTEGER DEFAULT 0                -- 0/1 是否已撤销
)
```

加索引：

```sql
CREATE INDEX IF NOT EXISTS idx_ai_audit_target ON ai_audit(target_qq);
CREATE INDEX IF NOT EXISTS idx_ai_audit_ts ON ai_audit(ts);
```

- [ ] **Step 3: 加 3 个函数**

在 db.py 函数区追加：

```python
def record_ai_audit(group_id: int, action: str, target_qq=None,
                    reason: str = "", verdict: str = "",
                    master_instructed: bool = False) -> int:
    """记录一条 AI 破坏性动作审计，返回 audit id"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = _execute(
        "INSERT INTO ai_audit (ts, group_id, action, target_qq, reason, judge_verdict, master_instructed) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, int(group_id), action,
         int(target_qq) if target_qq else None,
         reason[:500], verdict, 1 if master_instructed else 0)
    )
    return cur.lastrowid if cur else 0


def list_ai_audit(target_qq: int = None, action: str = None,
                  group_id: int = None, limit: int = 50,
                  only_undone: bool = False) -> list[dict]:
    """查询审计记录，按时间倒序"""
    sql = "SELECT * FROM ai_audit WHERE 1=1"
    args = []
    if target_qq:
        sql += " AND target_qq = ?"
        args.append(int(target_qq))
    if action:
        sql += " AND action = ?"
        args.append(action)
    if group_id:
        sql += " AND group_id = ?"
        args.append(int(group_id))
    if only_undone:
        sql += " AND undone = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return _fetch_all(sql, args)  # 用 db.py 现有的查全部辅助函数名


def mark_ai_audit_undone(audit_id: int) -> bool:
    """标记某条审计已撤销（主人执行了解禁/撤精华后调）"""
    cur = _execute("UPDATE ai_audit SET undone = 1 WHERE id = ?", (int(audit_id),))
    return bool(cur and cur.rowcount)
```

注意：用 db.py 里实际存在的辅助函数名（可能是 `_fetch_all` 或别的——读现有代码确认后调整）。

- [ ] **Step 4: 手动验证表创建 + 写入**

```python
python -c "from bot import db; db.init(); aid=db.record_ai_audit(123,'ban',456,'test','light',False); print('id',aid); print(db.list_ai_audit(target_qq=456))"
```
Expected: 输出 audit id 和一条记录。

- [ ] **Step 5: Commit**

```bash
git add bot/db.py
git commit -m "feat: db 加 ai_audit 审计表 + 读写函数"
```

---

## Task 5: guardian 升级 — 分级裁判闸门（核心）

**Files:**
- Modify: `bot/guardian.py`（大改）
- Create: `data/ai_guard_config.json`（默认配置）

**Interfaces:**
- Produces:
  - `guardian.SEVERITY_AUTONOMOUS / LIGHT / HEAVY / EXEMPT` — 4 个常量字符串
  - `guardian.ACTION_SEVERITY: dict[str,str]` — 动作→级别映射表
  - `await guardian.request(group_id, action, target_qq=None, reason="", caller_qq=None, master_instructed=False, bot_is_admin=True) -> dict` — 统一闸门入口，返回 `{"allow": bool, "verdict": str, "audit_id": int, "severity": str, "reason": str}`
  - `guardian.is_protected(target_qq, group_id, master_instructed=False) -> bool` — 受保护？
  - `await guardian.judge(action, target, reason, context, master_instructed) -> tuple[bool,str]` — 重度异步裁判，返回 (allow, verdict)
- 保留：现有 `create_action_request`/`approve_action`/`reject_action`/`secondary_check`/`get_pending_actions`

- [ ] **Step 1: 写默认配置 `data/ai_guard_config.json`**

```json
{
  "protected": {
    "master_qq": [],
    "whitelist": [],
    "protect_group_owner": true,
    "protect_group_admin": true
  },
  "thresholds": {
    "recall_light": true,
    "mute_light_max_seconds": 600,
    "essence_daily_per_group": 5,
    "at_all_hours_per_group": 6,
    "at_all_max_per_window": 1
  },
  "judge": {
    "placeholder_messages": ["让我想想…", "嗯…让我考虑一下", "等一下，我在想这事"],
    "enabled": true,
    "model": "",
    "temperature": 0.1,
    "max_tokens": 100
  },
  "self_ban_exempt": true,
  "master_override_protects": true
}
```

- [ ] **Step 2: 在 guardian.py 加配置加载 + 常量**

在 guardian.py 顶部 import 区加：

```python
import os
from . import config as _cfg
```

加常量和加载函数：

```python
SEVERITY_AUTONOMOUS = "autonomous"   # 直接执行
SEVERITY_LIGHT = "light"             # 快规则当场执行
SEVERITY_HEAVY = "heavy"             # 异步裁判
SEVERITY_EXEMPT = "exempt"           # 自禁等豁免

# 动作 → 默认级别
ACTION_SEVERITY = {
    "set_mood": SEVERITY_AUTONOMOUS,
    "set_essence": SEVERITY_AUTONOMOUS,
    "upload_group_file": SEVERITY_AUTONOMOUS,
    "create_group_folder": SEVERITY_AUTONOMOUS,
    "set_self_card": SEVERITY_AUTONOMOUS,
    "set_self_nickname": SEVERITY_AUTONOMOUS,
    "at_all": SEVERITY_LIGHT,          # @全体受限速（轻量）
    "recall": SEVERITY_LIGHT,
    "mute": SEVERITY_LIGHT,            # 禁言 ≤10min 是轻量；>10min 是重度
    "set_member_card": SEVERITY_LIGHT,
    "set_member_title": SEVERITY_LIGHT,
    "kick": SEVERITY_HEAVY,
    "ban": SEVERITY_HEAVY,
    "whole_ban": SEVERITY_HEAVY,
    "self_ban": SEVERITY_EXEMPT,       # 自禁豁免
}

_CONFIG_PATH = os.path.join(_cfg.DATA_DIR, "ai_guard_config.json")
_config_cache = None
_config_ts = 0.0


def load_config() -> dict:
    """加载 ai_guard 配置，带文件 mtime 缓存（5s）"""
    global _config_cache, _config_ts
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        mtime = 0
    if _config_cache is not None and abs(mtime - _config_ts) < 5:
        return _config_cache
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    except Exception:
        _config_cache = {}
    _config_ts = mtime
    return _config_cache or {}
```

- [ ] **Step 3: 实现 `is_protected`**

需要查群主/群管理员——异步查 OneBot。但 guardian 不直接持有 bot 实例，所以 `is_protected` 拿 group_member_info 字典（由调用方传入）。

```python
async def is_protected(target_qq: int, group_id: int,
                       member_info: dict = None,
                       master_instructed: bool = False) -> bool:
    """判断目标是否受保护。

    master_instructed=True 且配置允许覆盖时，
    保护名单里除主人自身外都可被覆盖。
    """
    cfg = load_config().get("protected", {})
    master_list = set(int(x) for x in cfg.get("master_qq", []))
    whitelist = set(int(x) for x in cfg.get("whitelist", []))

    # 主人永远受保护（即使 master_instructed 也不能动主人自己）
    if target_qq in master_list:
        return True
    if target_qq in whitelist:
        return True

    # 主人指令可覆盖其余保护
    if master_instructed and load_config().get("master_override_protects", True):
        return False

    # 群主/群管理员
    if member_info and isinstance(member_info, dict):
        role = member_info.get("role", "")
        if cfg.get("protect_group_owner", True) and role == "owner":
            return True
        if cfg.get("protect_group_admin", True) and role == "admin":
            return True
    return False
```

- [ ] **Step 4: 实现 `judge` 异步裁判**

```python
async def judge(action: str, target_qq: int, target_name: str,
                reason: str, context_snippets: list[str],
                master_instructed: bool) -> tuple[bool, str]:
    """重度动作异步裁判。返回 (allow, verdict_reason)。"""
    cfg = load_config().get("judge", {})
    if not cfg.get("enabled", True):
        return True, "judge_disabled"

    from . import llm_call, memory
    if llm_call.get_client() is None:
        return False, "llm_unavailable"

    ms = memory.load_model_settings()
    prompt = (
        "你是 QQ 群机器人的安全裁判。机器人的 AI 想对一名群成员执行一个重度操作。"
        "判断这个操作是否合理。\n\n"
        f"操作: {action}\n"
        f"目标: {target_name}({target_qq})\n"
        f"AI 给的理由: {reason[:300]}\n"
        f"是否主人明确指令: {'是' if master_instructed else '否'}\n\n"
        "最近群聊上下文（仅参考，不据此判定违规）：\n"
        + "\n".join(f"- {s[:100]}" for s in context_snippets[-10:]) +
        "\n\n合理性判定标准：\n"
        "- 主人明确指令 → 默认允许（除非目标本身是主人）\n"
        "- 非主人指令时：需要明确的违规/攻击证据，不能因'看他不爽'就动\n"
        "- 无理取闹/报复/无证据 → 拒绝\n\n"
        "只输出 JSON:\n"
        '{"allow": true/false, "verdict": "简短理由(20字以内)"}'
    )
    try:
        response = await llm_call.llm_create(
            model=cfg.get("model") or ms.get("model", "glm-5.2-free"),
            max_tokens=cfg.get("max_tokens", 100),
            temperature=cfg.get("temperature", 0.1),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        result = json.loads(text)
        return bool(result.get("allow", False)), result.get("verdict", "")
    except Exception as e:
        logger.warning(f"guardian 裁判失败（保守拒绝）: {e}")
        return False, f"judge_error: {type(e).__name__}"
```

- [ ] **Step 5: 实现 `request` 统一闸门**

```python
async def request(group_id: int, action: str,
                  target_qq: int = None, target_name: str = "",
                  reason: str = "",
                  caller_qq: int = None,
                  master_instructed: bool = False,
                  member_info: dict = None,
                  mute_seconds: int = 0,
                  context_snippets: list[str] = None,
                  bot_is_admin: bool = True) -> dict:
    """统一闸门入口。返回:
       {"allow": bool, "verdict": str, "audit_id": int,
        "severity": str, "reason": str, "placeholder_sent": bool}
    """
    from . import db

    # 禁言时长决定级别：>10min 升级为重度
    severity = ACTION_SEVERITY.get(action, SEVERITY_LIGHT)
    if action == "mute" and mute_seconds > load_config().get(
            "thresholds", {}).get("mute_light_max_seconds", 600):
        severity = SEVERITY_HEAVY

    # 自禁豁免
    cfg = load_config()
    if (cfg.get("self_ban_exempt", True)
            and target_qq and caller_qq
            and target_qq == caller_qq
            and action in ("mute", "ban", "kick", "self_ban")):
        severity = SEVERITY_EXEMPT

    # 保护名单硬护栏（豁免级别也查——除非是自禁）
    if target_qq and severity != SEVERITY_EXEMPT:
        if await is_protected(target_qq, group_id, member_info, master_instructed):
            aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                     "deny_protected", master_instructed)
            return {"allow": False, "verdict": "deny_protected",
                    "audit_id": aid, "severity": severity,
                    "reason": f"目标 {target_name} 受保护",
                    "placeholder_sent": False}

    # 自主/豁免 → 直接放行
    if severity in (SEVERITY_AUTONOMOUS, SEVERITY_EXEMPT):
        v = "autonomous" if severity == SEVERITY_AUTONOMOUS else "exempt"
        aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                 v, master_instructed)
        return {"allow": True, "verdict": v, "audit_id": aid,
                "severity": severity, "reason": reason,
                "placeholder_sent": False}

    # 轻量 → 当场放行（不调裁判 LLM），仅记审计
    if severity == SEVERITY_LIGHT:
        aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                 "light", master_instructed)
        return {"allow": True, "verdict": "light", "audit_id": aid,
                "severity": severity, "reason": reason,
                "placeholder_sent": False}

    # 重度 → 异步裁判（调用方应在调本函数前已发占位文案）
    allow, verdict = await judge(
        action, target_qq or 0, target_name, reason,
        context_snippets or [], master_instructed)
    label = "allow" if allow else "deny_judge"
    aid = db.record_ai_audit(group_id, action, target_qq, reason,
                             label, master_instructed)
    return {"allow": allow, "verdict": label, "audit_id": aid,
            "severity": severity,
            "reason": verdict if not allow else reason,
            "placeholder_sent": False}
```

- [ ] **Step 6: 加限速器（@all / 精华）**

在 guardian.py 追加：

```python
# 内存限速状态：{(group_id, action): [(ts,...)]}
_rate_state: dict[tuple, list[float]] = {}


def _check_rate(group_id: int, action: str, window_hours: int, max_count: int) -> bool:
    """检查 (group_id, action) 在过去 window_hours 小时内是否超过 max_count。返回 True=允许。"""
    import time as _time
    key = (int(group_id), action)
    now = _time.time()
    cutoff = now - window_hours * 3600
    recent = [t for t in _rate_state.get(key, []) if t > cutoff]
    if len(recent) >= max_count:
        return False
    recent.append(now)
    _rate_state[key] = recent
    return True


def check_at_all_rate(group_id: int) -> bool:
    """@全体 限速：每群每 6h 至多 1 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "at_all",
        cfg.get("at_all_hours_per_group", 6),
        cfg.get("at_all_max_per_window", 1),
    )


def check_essence_rate(group_id: int) -> bool:
    """自主设精华限速：每群每天 N 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "essence", 24,
        cfg.get("essence_daily_per_group", 5),
    )
```

- [ ] **Step 7: 加占位文案选择器**

```python
def pick_placeholder() -> str:
    """随机选一条占位文案（重度动作触发时群里先发）"""
    msgs = load_config().get("judge", {}).get(
        "placeholder_messages", ["让我想想…"])
    import random as _r
    return _r.choice(msgs) if msgs else "让我想想…"
```

- [ ] **Step 8: 手动验证分级**

```python
python -c "
import asyncio
from bot import guardian
# 自主类
r = asyncio.run(guardian.request(123,'set_mood',target_qq=111,reason='test'))
print('autonomous:', r['verdict'])
# 保护类（设主人 QQ=999，目标=999）
import os; os.makedirs('data',exist_ok=True)
import json; json.dump({'protected':{'master_qq':[999],'whitelist':[],'protect_group_owner':True,'protect_group_admin':True}},open('data/ai_guard_config.json','w'))
guardian._config_cache=None
r = asyncio.run(guardian.request(123,'kick',target_qq=999,target_name='master',reason='test'))
print('protected:', r['verdict'], r['allow'])
# 自禁豁免（caller==target）
r = asyncio.run(guardian.request(123,'mute',target_qq=555,caller_qq=555,reason='ban me'))
print('self_exempt:', r['verdict'])
"
```
Expected: `autonomous: autonomous` / `protected: deny_protected False` / `self_exempt: exempt`

- [ ] **Step 9: Commit**

```bash
git add bot/guardian.py data/ai_guard_config.json
git commit -m "feat: guardian 升级为通用分级裁判闸门（autonomous/light/heavy/exempt）"
```

---

## Task 6: ai_tools 新增工具 + execute_tool 路由进 guardian

**Files:**
- Modify: `bot/ai_tools.py`（中改）

**Interfaces:**
- Consumes: `guardian.request` / `db.record_ai_audit`（Task 4/5）
- Produces: 新工具定义 `upload_group_file` / `create_group_folder` / `set_member_card` / `set_member_title`；`recall_msg` 解除仅自己限制

- [ ] **Step 1: 看现有 execute_tool 结构**

Read `bot/ai_tools.py:632-1085`（execute_tool dispatcher）和 `bot/ai_tools.py:106-117`（recall_msg 当前定义）+ `bot/ai_tools.py:829-853`（kick/ban 当前裸执行处）。看工具 input schema 怎么写、参数怎么拿。

- [ ] **Step 2: 加新工具定义**

在 `ai_tools.py` 的 `ALL_TOOLS`（约 `ai_tools.py:57-625`）里，找到现有 `delete_file` 工具附近，加：

```python
{
    "name": "upload_group_file",
    "description": "把指定文本内容作为 .txt 文件上传到群文件。用于分享总结/笔记/精华合集。",
    "input_schema": {
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "filename": {"type": "string", "description": "文件名（含扩展名，如 总结.txt）"},
            "content": {"type": "string", "description": "文件正文"},
            "folder_id": {"type": "string", "description": "目标文件夹 ID（可选，默认根目录）"},
        },
        "required": ["filename", "content"],
    },
},
{
    "name": "create_group_folder",
    "description": "在群文件系统建一个文件夹。",
    "input_schema": {
        "type": "object",
        "properties": {
            "group_id": {"type": "integer"},
            "name": {"type": "string", "description": "文件夹名"},
            "parent_id": {"type": "string", "description": "父文件夹 ID（默认根目录）"},
        },
        "required": ["name"],
    },
},
{
    "name": "set_member_card",
    "description": "修改某群成员的群昵称（群名片）。仅当机器人是群主/管理员时可用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "group_id": {"type": "integer"},
            "user_id": {"type": "integer", "description": "目标 QQ"},
            "card": {"type": "string", "description": "新昵称"},
        },
        "required": ["user_id", "card"],
    },
},
{
    "name": "set_member_title",
    "description": "设置某群成员的头衔。仅当机器人是群主时可用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "group_id": {"type": "integer"},
            "user_id": {"type": "integer"},
            "title": {"type": "string", "description": "新头衔"},
        },
        "required": ["user_id", "title"],
    },
},
```

把这 4 个工具名也加进 `get_tools_for_group` 的 admin 分支返回集（仅群主/管理员态下暴露给 AI）。

- [ ] **Step 3: 改 `recall_msg` 工具定义解除仅自己限制**

找到 `ai_tools.py:106-117` 的 `recall_msg` 工具，把描述从"仅撤回自己"改为可撤回任何人，input 加 `user_id`（可选）：

```python
{
    "name": "recall_msg",
    "description": "撤回一条群消息（自己的或他人的）。撤回他人消息会进轻量护栏。",
    "input_schema": {
        "type": "object",
        "properties": {
            "message_id": {"type": "integer", "description": "要撤回的消息 ID"},
        },
        "required": ["message_id"],
    },
},
```

- [ ] **Step 4: 改 execute_tool 给破坏性动作加 guardian 闸门**

在 execute_tool 里，把现有的 `recall_msg`/`ban_member`/`kick_member`/`whole_ban`/`mute_member`/`at_all`/`send_essence` 分支改为：执行前先调 `guardian.request(...)`，allow 才真正调 onebot。

为了不重复代码，在 execute_tool 顶部加一个 helper：

```python
async def _gated(group_id, action, tool_input, caller_qq,
                master_instructed, bot, member_info=None, mute_seconds=0):
    """对破坏性动作过 guardian 闸门。allow 才继续。"""
    from . import guardian
    target = tool_input.get("user_id") or tool_input.get("target_id")
    target_name = tool_input.get("user_name", "")
    r = await guardian.request(
        group_id=group_id, action=action,
        target_qq=target, target_name=target_name,
        reason=tool_input.get("reason", ""),
        caller_qq=caller_qq,
        master_instructed=master_instructed,
        member_info=member_info,
        mute_seconds=mute_seconds,
    )
    if not r["allow"]:
        # 返回拒绝原因给 AI
        return {"ok": False, "denied": True, "reason": r["reason"],
                "verdict": r["verdict"]}
    return None  # None = 放行，调用方继续执行
```

然后改每个破坏性分支，例如 `kick_member`（原 `ai_tools.py:839-845`）：

```python
elif name == "kick_member":
    gid = int(tool_input.get("group_id") or _current_group)
    target = int(tool_input.get("user_id", 0))
    # 先拿 member_info 判断保护
    member_info = None
    try:
        mi = await bot.get_group_member_info(gid, target)
        member_info = mi.get("data") if isinstance(mi, dict) else None
    except Exception:
        pass
    # 重度动作触发前先发占位
    if master_instructed is False:  # 非主人指令才发占位
        await bot.send_group_msg(gid, guardian.pick_placeholder())
    gate = await _gated(gid, "kick", tool_input, _caller_qq,
                        master_instructed, bot, member_info)
    if gate is not None:
        return gate
    await bot.set_group_kick(gid, target,
                             bool(tool_input.get("reject_add_request", False)))
    return {"ok": True}
```

对 `ban_member`/`mute_member`/`whole_ban`/`recall_msg`(他人)/`at_all` 同样改造。注意：
- `at_all` 加 `guardian.check_at_all_rate(gid)` 限速检查
- `send_essence` 加 `guardian.check_essence_rate(gid)` 限速
- `recall_msg` 撤自己不过闸门，撤他人过 light 闸门

- [ ] **Step 5: 改 execute_tool 签名传 caller_qq + master_instructed**

找到 `execute_tool` 定义（`ai_tools.py:632` 附近），加两个参数：

```python
async def execute_tool(name: str, tool_input: dict, bot,
                       group_id: int = 0,
                       caller_qq: int = None,
                       master_instructed: bool = False) -> dict:
```

用模块级 `_current_group` / `_caller_qq` 临时变量传递（避免大改），在 chat.py 调 execute_tool 前设置。

- [ ] **Step 6: 加新工具的 execute 分支**

在 execute_tool 里加：

```python
elif name == "upload_group_file":
    gid = int(tool_input.get("group_id") or _current_group)
    fn = tool_input.get("filename", "note.txt")
    content = tool_input.get("content", "")
    if not content:
        return {"ok": False, "error": "content 为空"}
    # 写临时文件
    import tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), f"qqbot_{fn}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    await bot.upload_group_file(gid, tmp, fn)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return {"ok": True}

elif name == "create_group_folder":
    gid = int(tool_input.get("group_id") or _current_group)
    await bot.create_group_file_folder(
        gid, tool_input.get("name", "新建文件夹"),
        tool_input.get("parent_id", "/"))
    return {"ok": True}

elif name == "set_member_card":
    gid = int(tool_input.get("group_id") or _current_group)
    target = int(tool_input.get("user_id", 0))
    member_info = None
    try:
        mi = await bot.get_group_member_info(gid, target)
        member_info = mi.get("data") if isinstance(mi, dict) else None
    except Exception:
        pass
    gate = await _gated(gid, "set_member_card", tool_input,
                        _caller_qq, master_instructed, bot, member_info)
    if gate is not None:
        return gate
    await bot.set_group_card(gid, target, tool_input.get("card", ""))
    return {"ok": True}

elif name == "set_member_title":
    gid = int(tool_input.get("group_id") or _current_group)
    target = int(tool_input.get("user_id", 0))
    member_info = None
    try:
        mi = await bot.get_group_member_info(gid, target)
        member_info = mi.get("data") if isinstance(mi, dict) else None
    except Exception:
        pass
    gate = await _gated(gid, "set_member_title", tool_input,
                        _caller_qq, master_instructed, bot, member_info)
    if gate is not None:
        return gate
    await bot.set_group_special_title(gid, target, tool_input.get("title", ""))
    return {"ok": True}
```

- [ ] **Step 7: 改 chat.py 调 execute_tool 时传新参数**

Read `bot/chat.py:498-520`（tool 执行循环）。在调 `ai_tools.execute_tool(...)` 时加 `caller_qq=...` 和 `master_instructed=...`。caller_qq 从 chat_reply 入参 user_qq 拿；master_instructed 判断 user_qq == config.MASTER_QQ。

- [ ] **Step 8: 手动 smoke test**

启动 bot，群里用主人号说"把xxx禁言10分钟"，观察：
1. AI 触发 mute_member 工具
2. 因 ≤10min 走 light 闸门，当场执行
3. 日志出现审计记录
4. bot.log 看到 `record_ai_audit` 调用

- [ ] **Step 9: Commit**

```bash
git add bot/ai_tools.py bot/chat.py
git commit -m "feat: ai_tools 新增 4 工具 + 破坏性动作过 guardian 闸门"
```

---

## Task 7: handler 删关键词指令 + master_instructed 标记

**Files:**
- Modify: `bot/handler.py`（中改）

- [ ] **Step 1: 定位要删的 7 个关键词块**

Read `bot/handler.py:1224-1863`。找到这 7 个关键词的处理块（grep `if "违规" in` / `if "禁言" in` / `if "踢人" in` / `if "拉黑" in` / `if "精华" in` / `if "取消精华" in` / `if "全员禁言" in`）。

- [ ] **Step 2: 删 7 个关键词块**

把这 7 个块整段删除。这些能力改为：
- AI 通过工具调用执行（Task 6）
- 主人用自然语言说"把xxx禁言"→ AI chat_reply 触发 `mute_member` 工具

注意：删之前看清楚每个块是否包含其他依赖（比如更新某个状态变量），把副作用剥离保留。

- [ ] **Step 3: 保留审批类指令**

确认 `待审批` / `同意` / `拒绝` / `mode` 切换 / 查询类指令**保留不动**（约 `handler.py:1666-1719` 的审批块）。

- [ ] **Step 4: 加 master_instructed 透传**

在 `handle_group_message`（约 `handler.py:848`）里，识别发言者是不是主人，存到局部变量 `is_master`，传给 chat_reply（chat.py 接收并透传给 execute_tool）。这一步要改 chat_reply 签名加 `master_instructed` 参数。

Read `handler.py:2000-2122` 找 chat.chat_reply 调用处，加 `master_instructed=is_master`。

Read `bot/chat.py` 的 chat_reply 定义，加 `master_instructed: bool = False` 参数，透传给 execute_tool。

- [ ] **Step 5: 手动 smoke test**

启动 bot，主人号用自然语言说"撤回刚才xxx那条"——观察 AI 是否触发 recall_msg 工具并过闸门执行。再说"禁我"——观察自禁豁免生效。

- [ ] **Step 6: Commit**

```bash
git add bot/handler.py bot/chat.py
git commit -m "refactor: 删 7 关键词管理指令，改 AI 自然语言 + master_instructed 标记"
```

---

## Task 8: consciousness_loop 加主动式自主 tick

**Files:**
- Modify: `main.py`（小改，consciousness_loop 里加 2 个 tick）
- Modify: `bot/ai_brain.py`（加 `maybe_auto_essence` / 改 `share_news_if_ready` 支持 @全体）

- [ ] **Step 1: 加自主设精华 tick**

在 `bot/ai_brain.py` 加：

```python
async def maybe_auto_essence(bot) -> int:
    """扫所有群，对近 N 分钟高影响力消息主动设精华。返回设精数目。"""
    from . import guardian, memory, influence
    count = 0
    try:
        r = await bot._call("get_group_list", {})
        groups = r.get("data", []) if r.get("status") == "ok" else []
        for g in groups:
            gid = g.get("group_id", 0)
            if not gid or not guardian.check_essence_rate(gid):
                continue
            # 找近 30 分钟该群影响力最高的消息
            recent = memory._group_message_window.get(gid, [])
            if not recent:
                continue
            # 取最后 50 条里 influence 最高的
            import time as _t
            cutoff = _t.time() - 1800
            candidates = [m for m in recent[-50:]
                          if m.get("ts", 0) > cutoff
                          and m.get("message_id")]
            if not candidates:
                continue
            best = max(candidates, key=lambda m: influence.score(m.get("text", "")))
            if influence.score(best.get("text", "")) < 5:  # 阈值
                continue
            try:
                await bot.set_essence_msg(best["message_id"])
                count += 1
                await asyncio.sleep(random.uniform(3, 6))
            except Exception as e:
                logger.debug(f"自主设精华失败 {gid}: {e}")
    except Exception as e:
        logger.debug(f"maybe_auto_essence 异常: {e}")
    return count
```

（`influence.score` 的实际函数名要看 influence.py——读后调整）

在 `main.py` consciousness_loop（约 `main.py:357` 保存状态之前）加：

```python
            # ── 9. 自主设精华（每 30 分钟）──
            if now.minute == 30 or now.minute == 0:
                try:
                    n = await ai_brain.maybe_auto_essence(bot)
                    if n:
                        logger.info(f"⭐ 自主设了 {n} 条精华")
                except Exception as e:
                    logger.debug(f"自主设精华跳过: {e}")
```

- [ ] **Step 2: 重大新闻 @全体**

改 `bot/ai_brain.py` 的 `share_news_if_ready`（约 `ai_brain.py:383`），在发新闻前检查是否"重大"（标题含灾难/突发/重大关键词），如果是则对每个群：
1. `guardian.check_at_all_rate(gid)` 检查限速
2. `bot.get_group_at_all_remain(gid)` 检查剩余额度
3. 满足则在新闻前加 `[CQ:at,qq=all] ` 段

```python
# 在 share_news_if_ready 发送循环里
big_keywords = ("灾难", "突发", "重大", "地震", "事故", "疫情", "警告")
is_big = any(kw in (news_title or "") for kw in big_keywords)
for g in groups:
    gid = g.get("group_id", 0)
    if not gid:
        continue
    msg_to_send = news
    if is_big and guardian.check_at_all_rate(gid):
        try:
            remain = await bot.get_group_at_all_remain(gid)
            if remain.get("data", {}).get("can_at_all"):
                msg_to_send = "[CQ:at,qq=all] " + news
        except Exception:
            pass
    await bot.send_group_msg(gid, msg_to_send)
    await asyncio.sleep(random.uniform(5, 8))
```

- [ ] **Step 3: 每日总结自主传群文件**

在 `main.py` 17:00 总结块（`main.py:312-330`）之后，加一个把当日总结写成 .txt 上传群文件的调用（复用 Task 6 的 upload_group_file 工具底层，或直接调 `bot.upload_group_file`）。

- [ ] **Step 4: 手动验证**

启动 bot，等 consciousness_loop 跑到整点/半点，观察日志 `⭐ 自主设了 N 条精华`。

- [ ] **Step 5: Commit**

```bash
git add main.py bot/ai_brain.py
git commit -m "feat: consciousness_loop 加自主设精华/重大新闻@全体/总结传群文件"
```

---

## Task 9: 清理思考模式残留配置 + Web UI

**Files:**
- Modify: `bot/config.py:25`
- Modify: `bot/memory.py:684`
- Modify: `web/api.py:288`
- Modify: `web/static/pages/model.html`
- Modify: `web/static/js/model.js`

- [ ] **Step 1: 删 config.py 的 LLM_THINKING_EFFORT**

Read `bot/config.py:25`，删掉这行：

```python
    LLM_THINKING_EFFORT: str = os.getenv("LLM_THINKING_EFFORT", "medium")
```

grep 全项目确认无其他引用（应只有这一处）。

- [ ] **Step 2: 删 memory.py 默认设置的 thinking_effort**

Read `bot/memory.py:678-690`，从 `DEFAULT_MODEL_SETTINGS` dict 里删 `"thinking_effort": "adaptive",` 这行。

- [ ] **Step 3: 删 web/api.py 允许字段**

Read `web/api.py:288`，从 `allowed = {...}` 集合里删 `"thinking_effort"`。

- [ ] **Step 4: 删 model.html 思考强度下拉**

Read `web/static/pages/model.html:15-16`，删掉 `<select id="m-think">` 整块（含 label）。

- [ ] **Step 5: 删 model.js 思考强度读写**

Read `web/static/js/model.js:15,28`，删掉 `$('m-think').value = ...` 和保存逻辑里的 `thinking_effort` 字段。

- [ ] **Step 6: grep 确认零残留**

Run: `rg -n "thinking_effort|THINKING_EFFORT" --type py`（或 PowerShell `Select-String`）
Expected: 0 matches.

- [ ] **Step 7: Commit**

```bash
git add bot/config.py bot/memory.py web/api.py web/static/pages/model.html web/static/js/model.js
git commit -m "refactor: 删除思考模式残留配置（glm-5.2-free 实测不支持）"
```

---

## Task 10: 端到端 smoke test + 验收清单

**Files:** 无（验证）

- [ ] **Step 1: 启动 bot 跑完整流程**

Run: `python main.py`，让它跑 5 分钟。

- [ ] **Step 2: 跑 timeutil 单测**

Run: `python tests/test_timeutil.py`
Expected: 全 PASS。

- [ ] **Step 3: 验收清单逐项核对**

对照 spec 验收标准：
- [ ] 用自然语言说"把xxx禁言10分钟"能执行（light 闸门）
- [ ] 用自然语言说"踢了yyy"能触发异步裁判（先发"让我想想…"，~100s 后执行或拒绝）
- [ ] 保护名单内成员被 AI 尝试撤回时被拒（日志 `deny_protected`）
- [ ] 自禁请求（"禁我"）能执行（`exempt`）
- [ ] bot 报给群/LLM 的时间在系统时钟偏移时仍准确
- [ ] 17:00 总结/23:00 反思/03:00 扫描按真实上海时间触发
- [ ] 无 `thinking_effort` 残留配置
- [ ] AI 主动设精华/传群文件/@全体 各能触发（可能要等到整点）

- [ ] **Step 4: 修复发现的问题**

按 smoke test 暴露的问题逐个修复。

- [ ] **Step 5: 最终 commit**

```bash
git add -A
git commit -m "test: 端到端 smoke test 通过"
```

---

## Self-Review（plan 写完后自查）

**1. Spec coverage:**
- ✅ 思考模式跳过 + 残留清理 → Task 9
- ✅ 分级异步裁判 → Task 5
- ✅ AI 工具路由 + 新工具（群文件/昵称） → Task 6
- ✅ 删 7 关键词 + 自然语言 + master_instructed → Task 7
- ✅ consciousness_loop 主动设精华/@全体/传群文件 → Task 8
- ✅ 保护名单（主人/群主/管理员/白名单）+ 主人指令覆盖 → Task 5 is_protected
- ✅ 自禁豁免 → Task 5（severity=exempt）
- ✅ 审计 + 主人自然语言撤销 → Task 4 db.ai_audit
- ✅ @全体限速 → Task 5 check_at_all_rate
- ✅ 时间同步（10 源随机抽 3） → Task 1-3
- ⚠️ "主人自然语言撤销"完整闭环（AI 查审计 + 调反向 API）—— spec 里描述了，plan 里 Task 4 加了查询函数，但"AI 工具自动撤销"需要再加一个 `undo_action` 工具。**补 Task 6.5。**

**2. Placeholder scan:** 发现一处——Task 5 Step 5 里 `master_override_protects`、`self_ban_exempt`、`mute_light_max_seconds` 在 Task 5 Step 1 默认配置里都有定义 ✓。

**3. Type consistency:** `guardian.request` 返回 dict 字段在 Task 5 定义，Task 6 `_gated` 用了 `r["reason"]`/`r["verdict"]` ✓ 一致。`db.record_ai_audit` 返回 int，Task 5 用 `aid = db.record_ai_audit(...)` ✓。

**补缺：Task 6.5 — undo_action 工具（主人自然语言撤销）**

---

## Task 6.5: undo_action 工具（主人自然语言撤销）

**Files:**
- Modify: `bot/ai_tools.py`（加工具定义 + execute 分支）

- [ ] **Step 1: 加工具定义**

在 ALL_TOOLS 加：

```python
{
    "name": "undo_recent_action",
    "description": "撤销机器人最近对某成员执行的破坏性动作（解禁/撤精华等）。主人说\"撤销对xxx的禁言\"时用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "target_qq": {"type": "integer", "description": "目标 QQ"},
            "group_id": {"type": "integer"},
        },
        "required": ["target_qq"],
    },
},
```

- [ ] **Step 2: 加 execute 分支**

```python
elif name == "undo_recent_action":
    from . import db
    gid = int(tool_input.get("group_id") or _current_group)
    target = int(tool_input.get("user_id") or tool_input.get("target_qq", 0))
    # 查最近对该目标的、未撤销的破坏性动作
    records = db.list_ai_audit(target_qq=target, group_id=gid,
                               only_undone=True, limit=5)
    if not records:
        return {"ok": False, "reason": "没有可撤销的记录"}
    undone = []
    for rec in records:
        act = rec.get("action")
        try:
            if act in ("mute", "ban"):
                # 解禁/解封
                await bot.set_group_ban(gid, target, 0)
                db.mark_ai_audit_undone(rec["id"])
                undone.append(act)
            elif act == "essence":
                # 撤精华（需要 message_id，审计里没存——记录为待补）
                pass
            elif act == "kick":
                # 踢了无法撤销（人已不在群里）——只能重新邀请
                pass
        except Exception as e:
            logger.debug(f"撤销 {act} 失败: {e}")
    return {"ok": bool(undone), "undone": undone}
```

- [ ] **Step 3: Commit**

```bash
git add bot/ai_tools.py
git commit -m "feat: 加 undo_recent_action 工具（主人自然语言撤销）"
```
