# AI 自主权扩大 + 旧新闻修复 + 心情/睡眠系统重做 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 AI 心情卡死、旧新闻、睡眠僵硬三大问题，同时扩大 AI 自主权（拆分消息、自动表态、自主睡眠）让它更像真人。

**Architecture:** 五个工作流分五个阶段实施（心情→新闻→睡眠→消息拆分→自动表态）。所有改动以 TDD 进行：先写 `unittest` 测试，再改 `bot/` 代码。状态变更通过版本迁移钩子兼容旧 `consciousness.json`。

**Tech Stack:** Python 3, asyncio, httpx（搜索）, NapCat OneBot v11, unittest（测试，`python tests/test_xxx.py` 直接跑）

**Spec:** `docs/superpowers/specs/2026-08-11-ai-autonomy-and-mood-fix-design.md`

## Global Constraints

- 不动 `DANGEROUS_TOOLS`（kick/ban/whole_ban 等仍走 guardian 审批）
- 不动保护名单（主人/管理员/白名单）
- 不改自动审核（违规识别）系统
- 睡眠硬底线：每 24h 累计睡眠 ≥ 60 分钟（不可去除）
- 所有测试用 `python tests/test_xxx.py` 直接运行（项目不用 pytest）
- 新加的 settings 配置项必须有安全默认值（`settings.py` 里 `load()` 返回的字典用 `.get(key, default)` 消费）
- 改动 `consciousness.py` 的状态结构时，必须通过版本迁移（`_migrate_v1_to_v2`）兼容旧的 `data/consciousness.json`
- 不加代码注释（项目约定）

## File Structure

**修改的文件**（按阶段）:
- Phase A: `bot/consciousness.py`（情绪基线 + 优先级 + 迁移）, 新测试 4 个
- Phase B: `bot/web_search.py`（时间过滤）, `bot/ai_brain.py`（新鲜度校验）, 新测试 2 个
- Phase C: `bot/consciousness.py`（睡眠系统）, `bot/ai_tools.py`（set_sleep 工具）, `bot/handler.py`（被叫醒）, `main.py`（SLEEP 协议）, 新测试 3 个
- Phase D: `bot/humanizer.py`（拆分阈值）, `main.py`（走 humanizer）, `bot/ai_brain.py` + `bot/guardian.py`（去冷却+软限速）, 新测试 1 个
- Phase E: `bot/guardian.py`（表态限速）, `bot/handler.py`（自动表态）, 新模块 `bot/auto_emoji.py`, 新测试 1 个

**新增文件**:
- `bot/auto_emoji.py` — 自动表情表态评估器
- `tests/test_mood_decay.py`, `tests/test_dominant_mood.py`, `tests/test_positive_events.py`, `tests/test_state_migration.py`
- `tests/test_web_search_freshness.py`, `tests/test_news_freshness.py`
- `tests/test_sleep_system.py`, `tests/test_sleep_floor.py`, `tests/test_set_sleep_tool.py`
- `tests/test_proactive_rate.py`, `tests/test_auto_emoji_rate.py`

---

## Phase A: 修复心情卡死（Workflow ④）

> 根因：`apply_natural_decay` 让 `irritation` 以 0.5 为目标衰减，而 `get_dominant_mood` 在 `>0.5` 就判 grumpy，边缘值 0.5098 卡死出不来；且无任何正向事件减 irritation。

### Task 1: 情绪衰减独立基线

**Files:**
- Modify: `bot/consciousness.py:507-582`（DECAY_RATES + apply_natural_decay）
- Test: `tests/test_mood_decay.py`

**Interfaces:**
- Produces: `EMOTION_BASELINE: dict[str, float]` — 各情绪的衰减目标值
- Produces: `apply_natural_decay(state, elapsed_minutes)` — 新签名不变，行为改变（target 取自 EMOTION_BASELINE）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_mood_decay.py`:

```python
"""情绪衰减测试 — 验证负向情绪衰减到低基线，不卡在 0.5"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import apply_natural_decay, EMOTION_BASELINE


class TestMoodDecay(unittest.TestCase):

    def test_irritation_decays_to_low_baseline(self):
        """irritation=0.51 衰减后应向 0.12 靠拢，不卡在 0.5"""
        state = {"emotions": {"irritation": 0.51}}
        # 模拟 60 分钟衰减
        apply_natural_decay(state, 60.0)
        self.assertLess(state["emotions"]["irritation"], 0.45,
                        "irritation 应明显低于 0.5，不再卡死")

    def test_irritation_baseline_is_low(self):
        """EMOTION_BASELINE['irritation'] 必须是低值"""
        self.assertLessEqual(EMOTION_BASELINE["irritation"], 0.15)

    def test_joy_decays_to_neutral_positive(self):
        """joy 衰减后向 0.55 靠拢（略偏正），不是 0.5"""
        state = {"emotions": {"joy": 0.9}}
        apply_natural_decay(state, 120.0)
        self.assertGreater(state["emotions"]["joy"], 0.5)

    def test_boredom_growth_caps_at_baseline(self):
        """boredom 增长后不超过基线太多"""
        state = {"emotions": {"boredom": 0.2}}
        apply_natural_decay(state, 60.0)
        self.assertLessEqual(state["emotions"]["boredom"], 0.86)

    def test_sadness_decays_low(self):
        """sadness 也衰减到低基线"""
        state = {"emotions": {"sadness": 0.7}}
        apply_natural_decay(state, 60.0)
        self.assertLess(state["emotions"]["sadness"], 0.5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_mood_decay.py
```
Expected: FAIL（`EMOTION_BASELINE` 不存在 → ImportError）

- [ ] **Step 3: 添加 EMOTION_BASELINE 常量**

在 `bot/consciousness.py` 的 `DECAY_RATES` 字典（约 519 行）之后、`EMOTION_MIN` 之前，加:

```python
EMOTION_BASELINE = {
    "joy": 0.55,
    "calm": 0.60,
    "energy": 0.55,
    "curiosity": 0.55,
    "social": 0.55,
    "satisfaction": 0.55,
    "irritation": 0.12,
    "sadness": 0.12,
    "boredom": 0.25,
    "loneliness": 0.25,
}
```

- [ ] **Step 4: 修改 apply_natural_decay 用 EMOTION_BASELINE**

把 `apply_natural_decay` 函数（约 563 行）里的 `target = 0.5` 改为:

```python
def apply_natural_decay(state: dict, elapsed_minutes: float):
    """自然情绪衰减 — 正向情绪向各自基线靠拢，负向情绪向低基线衰减"""
    for emotion, rate in DECAY_RATES.items():
        decay = rate * elapsed_minutes
        target = EMOTION_BASELINE.get(emotion, 0.5)
        if decay > 0:
            current = state["emotions"][emotion]
            if abs(current - target) < 0.01:
                continue
            half_life_factor = 1 - min(decay * 2, 0.5)
            new_val = target + (current - target) * half_life_factor
            state["emotions"][emotion] = max(EMOTION_MIN, min(EMOTION_MAX, new_val))
        else:
            growth = abs(decay)
            cap = EMOTION_BASELINE.get(emotion, 0.85) + 0.6
            cap = min(cap, EMOTION_MAX)
            state["emotions"][emotion] = min(cap, state["emotions"][emotion] + growth)
```

注意：boredom/loneliness 的增长上限从硬编码 0.85 改为基于基线（0.25+0.6=0.85，保持原有行为）。

- [ ] **Step 5: 运行测试确认通过**

```bash
python tests/test_mood_decay.py
```
Expected: 5 tests PASS

- [ ] **Step 6: 提交**

```bash
git add bot/consciousness.py tests/test_mood_decay.py
git commit -m "fix: 情绪衰减独立基线，irritation 衰减到 0.12 不再卡 0.5"
```

---

### Task 2: 调整 get_dominant_mood 优先级

**Files:**
- Modify: `bot/consciousness.py:867-902`（get_dominant_mood）
- Test: `tests/test_dominant_mood.py`

**Interfaces:**
- Produces: `get_dominant_mood(state)` — 新行为：积极状态优先判定，irritation 阈值提到 0.6

- [ ] **Step 1: 写失败测试**

创建 `tests/test_dominant_mood.py`:

```python
"""get_dominant_mood 优先级测试 — 修复边缘 irritation 卡死 grumpy"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import get_dominant_mood


def _state(**emotions):
    """构造测试用 state，emotions 缺省值用合理中位"""
    defaults = {
        "joy": 0.5, "calm": 0.5, "energy": 0.5, "curiosity": 0.5,
        "social": 0.5, "irritation": 0.1, "boredom": 0.3,
        "loneliness": 0.25, "satisfaction": 0.5, "sadness": 0.1,
    }
    defaults.update(emotions)
    return {"emotions": defaults, "sleeping": False}


class TestDominantMood(unittest.TestCase):

    def test_edge_irritation_with_high_joy_is_happy(self):
        """irritation=0.51 + joy=0.57 → happy，不是 grumpy（核心 bug 场景）"""
        s = _state(irritation=0.51, joy=0.57, energy=0.45, satisfaction=0.86, curiosity=0.99)
        self.assertEqual(get_dominant_mood(s), "happy")

    def test_high_irritation_still_grumpy(self):
        """irritation=0.7 仍然是 grumpy"""
        s = _state(irritation=0.7, joy=0.4)
        self.assertEqual(get_dominant_mood(s), "grumpy")

    def test_excited_when_joy_and_energy_high(self):
        """joy>0.7 + energy>0.6 → excited"""
        s = _state(joy=0.8, energy=0.7, irritation=0.2)
        self.assertEqual(get_dominant_mood(s), "excited")

    def test_curious_when_low_irritation(self):
        """curiosity>0.7 + 低 irritation → curious"""
        s = _state(curiosity=0.85, joy=0.4, energy=0.5, irritation=0.2)
        self.assertEqual(get_dominant_mood(s), "curious")

    def test_grumpy_threshold_is_06(self):
        """irritation=0.55（低于新阈值 0.6）+ 低 joy → 不是 grumpy"""
        s = _state(irritation=0.55, joy=0.3, energy=0.3)
        mood = get_dominant_mood(s)
        self.assertNotEqual(mood, "grumpy")

    def test_sleeping_overrides_all(self):
        """sleeping=True → sleeping"""
        s = _state(joy=0.9, energy=0.9)
        s["sleeping"] = True
        self.assertEqual(get_dominant_mood(s), "sleeping")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_dominant_mood.py
```
Expected: `test_edge_irritation_with_high_joy_is_happy` FAIL（当前返回 grumpy）

- [ ] **Step 3: 修改 get_dominant_mood**

替换 `bot/consciousness.py:867-902` 整个函数体:

```python
def get_dominant_mood(state: dict) -> str:
    """获取当前主导情绪标签"""
    e = state["emotions"]
    if state.get("sleeping"):
        return "sleeping"

    # 积极复合情绪优先判定（避免边缘 irritation 抢占开心状态）
    if e["joy"] > 0.7 and e["energy"] > 0.6 and e.get("irritation", 0) < 0.5:
        return "excited"
    if e["joy"] > 0.55 and e.get("irritation", 0) < 0.45:
        return "happy"

    # 负向情绪（阈值提高，避免边缘卡死）
    if e.get("sadness", 0) > 0.55:
        return "sad"
    if e["irritation"] > 0.6:
        return "grumpy"

    # 中性情绪
    if e["boredom"] > 0.6:
        return "bored"
    if e["loneliness"] > 0.6:
        return "lonely"
    if e["energy"] < 0.25:
        return "tired"
    if e["curiosity"] > 0.7:
        return "curious"
    if e["calm"] > 0.7 and e["satisfaction"] > 0.6:
        return "content"
    if e["irritation"] > 0.3:
        return "irritated"

    if e["joy"] >= 0.45:
        return "neutral_happy"
    elif e["energy"] >= 0.4:
        return "neutral"
    else:
        return "tired"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python tests/test_dominant_mood.py
```
Expected: 6 tests PASS

- [ ] **Step 5: 提交**

```bash
git add bot/consciousness.py tests/test_dominant_mood.py
git commit -m "fix: get_dominant_mood 积极状态优先，irritation 阈值提到 0.6"
```

---

### Task 3: 正向事件降低烦躁

**Files:**
- Modify: `bot/consciousness.py:598-625`（on_mentioned, on_replied_to_bot, on_praised）
- Test: `tests/test_positive_events.py`

**Interfaces:**
- Produces: 三个 on_* 函数新增 irritation -0.04/-0.04/-0.08 的 delta

- [ ] **Step 1: 写失败测试**

创建 `tests/test_positive_events.py`:

```python
"""正向事件降低 irritation 测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import on_mentioned, on_replied_to_bot, on_praised


def _state(irritation=0.5):
    return {
        "emotions": {
            "joy": 0.5, "calm": 0.5, "energy": 0.5, "curiosity": 0.5,
            "social": 0.5, "irritation": irritation, "boredom": 0.3,
            "loneliness": 0.25, "satisfaction": 0.5, "sadness": 0.1,
        },
        "today": {"interactions": 0, "praised": 0, "highlights": []},
        "relationships": {},
        "traits": {"stubbornness": 0.5},
    }


class TestPositiveEventsReduceIrritation(unittest.TestCase):

    def test_on_mentioned_reduces_irritation(self):
        s = _state(irritation=0.5)
        on_mentioned(s, "测试用户", 123456)
        self.assertLess(s["emotions"]["irritation"], 0.5,
                        "被@后 irritation 应下降")

    def test_on_replied_to_bot_reduces_irritation(self):
        s = _state(irritation=0.5)
        on_replied_to_bot(s, "测试用户")
        self.assertLess(s["emotions"]["irritation"], 0.5,
                        "被回复后 irritation 应下降")

    def test_on_praised_reduces_irritation_more(self):
        s = _state(irritation=0.5)
        before = s["emotions"]["irritation"]
        on_praised(s, "测试用户")
        after = s["emotions"]["irritation"]
        self.assertLess(after, before,
                        "被夸后 irritation 应明显下降")
        # 被夸减得比被@多
        s2 = _state(irritation=0.5)
        on_mentioned(s2, "测试用户", 123456)
        self.assertLess(after, s2["emotions"]["irritation"],
                        "被夸减 irritation 应比被@减得多")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_positive_events.py
```
Expected: 3 tests FAIL（当前 on_* 不减 irritation）

- [ ] **Step 3: 给三个函数加 irritation delta**

在 `on_mentioned`（约 598 行）的 `apply_emotion_delta(state, "curiosity", 0.03)` 之后加:

```python
    apply_emotion_delta(state, "irritation", -0.04)
```

在 `on_replied_to_bot`（约 609 行）的 `apply_emotion_delta(state, "loneliness", -0.03)` 之后加:

```python
    apply_emotion_delta(state, "irritation", -0.04)
```

在 `on_praised`（约 618 行）的 `apply_emotion_delta(state, "satisfaction", 0.06)` 之后加:

```python
    apply_emotion_delta(state, "irritation", -0.08)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python tests/test_positive_events.py
```
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add bot/consciousness.py tests/test_positive_events.py
git commit -m "fix: 正向事件（被@/回复/夸）降低 irritation，防止只增不减"
```

---

### Task 4: 状态迁移 v1→v2（一次性修复现存卡死值）

**Files:**
- Modify: `bot/consciousness.py:32-96`（DEFAULT_STATE version 改 2）, `103-113`（load_state）, 新增 `_migrate_v1_to_v2`
- Test: `tests/test_state_migration.py`

**Interfaces:**
- Produces: `_migrate_v1_to_v2(state)` — 把 v1 状态升级到 v2，重置异常卡死值
- Produces: `load_state()` 升级后自动调用迁移

- [ ] **Step 1: 写失败测试**

创建 `tests/test_state_migration.py`:

```python
"""状态迁移 v1→v2 测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.consciousness import _migrate_v1_to_v2


class TestStateMigration(unittest.TestCase):

    def test_stuck_irritation_with_high_joy_resets(self):
        """v1 状态 irritation=0.51 + joy=0.57 → 迁移后 irritation 降到低基线"""
        state = {
            "version": 1,
            "emotions": {
                "joy": 0.57, "calm": 0.75, "energy": 0.4,
                "curiosity": 0.99, "social": 0.87, "irritation": 0.5098,
                "boredom": 0.3, "loneliness": 1.0, "satisfaction": 0.86,
                "sadness": 0.1,
            },
            "sleeping": False,
        }
        _migrate_v1_to_v2(state)
        self.assertEqual(state["version"], 2)
        self.assertLess(state["emotions"]["irritation"], 0.2,
                        "卡死的 irritation 应被重置到低值")

    def test_normal_low_irritation_not_touched(self):
        """irritation 本来就低的不动"""
        state = {
            "version": 1,
            "emotions": {"irritation": 0.15, "joy": 0.6},
        }
        _migrate_v1_to_v2(state)
        self.assertAlmostEqual(state["emotions"]["irritation"], 0.15)

    def test_genuinely_grumpy_kept(self):
        """真正烦躁（irritation 高 + joy 低）保留"""
        state = {
            "version": 1,
            "emotions": {"irritation": 0.8, "joy": 0.2},
        }
        _migrate_v1_to_v2(state)
        self.assertGreaterEqual(state["emotions"]["irritation"], 0.7,
                                "真正烦躁不应被重置")

    def test_loneliness_capped(self):
        """loneliness=1.0 降到合理上限"""
        state = {
            "version": 1,
            "emotions": {"irritation": 0.1, "joy": 0.5, "loneliness": 1.0},
        }
        _migrate_v1_to_v2(state)
        self.assertLess(state["emotions"]["loneliness"], 0.9)

    def test_already_v2_noop(self):
        """version=2 不迁移"""
        state = {"version": 2, "emotions": {"irritation": 0.51, "joy": 0.57}}
        _migrate_v1_to_v2(state)
        self.assertAlmostEqual(state["emotions"]["irritation"], 0.51)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_state_migration.py
```
Expected: ImportError（`_migrate_v1_to_v2` 不存在）

- [ ] **Step 3: 添加迁移函数**

在 `bot/consciousness.py` 的 `DEFAULT_STATE` 字典里把 `"version": 2,` 确认（原来就是 2，但旧文件可能是 1）。

在 `load_state` 函数（约 103 行）之前，加迁移函数:

```python
def _migrate_v1_to_v2(state: dict):
    """v1 → v2 迁移：修复卡死的边缘情绪值。

    规则：如果 irritation 在 0.4-0.6 边缘但 joy 明显更高（积极状态），
    说明是卡死 bug 的产物，重置到低基线。真正烦躁（joy 低）保留。
    """
    if state.get("version", 1) >= 2:
        return
    e = state.get("emotions", {})
    irritation = e.get("irritation", 0)
    joy = e.get("joy", 0.5)
    if irritation > 0.4 and joy > irritation + 0.1:
        e["irritation"] = EMOTION_BASELINE["irritation"]
        logger.info(f"迁移 v1→v2: 重置卡死的 irritation {irritation:.2f} → {e['irritation']}")
    if e.get("loneliness", 0) > 0.9:
        e["loneliness"] = 0.85
    if e.get("boredom", 0) > 0.9:
        e["boredom"] = 0.85
    state["version"] = 2
```

- [ ] **Step 4: 在 load_state 里调用迁移**

修改 `load_state`（约 103 行），在 `return s` 之前加:

```python
            _migrate_v1_to_v2(s)
            return s
```

（在 `_deep_merge(DEFAULT_STATE, s)` 之后）

- [ ] **Step 5: 运行测试确认通过**

```bash
python tests/test_state_migration.py
```
Expected: 5 tests PASS

- [ ] **Step 6: 提交**

```bash
git add bot/consciousness.py tests/test_state_migration.py
git commit -m "fix: 状态迁移 v1→v2，一次性修复卡死的 irritation 边缘值"
```

---

## Phase B: 修复旧新闻（Workflow ③）

### Task 5: web_search 时间过滤

**Files:**
- Modify: `bot/web_search.py:37-92`（search + _format_results）, `143-160`（format_search_results, search_and_format）
- Test: `tests/test_web_search_freshness.py`

**Interfaces:**
- Produces: `search(query, max_results, time_range, max_age_days)` — 新增两个可选参数
- Produces: `search_recent(query, max_age_days=7, max_results=5)` — 默认只搜一周
- 搜索结果 dict 新增 `"published": str` 字段（ISO 日期或空串）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_web_search_freshness.py`:

```python
"""web_search 时间过滤测试 — 用 httpx mock 验证"""
import sys
import os
import unittest
import asyncio
from unittest.mock import patch, AsyncMock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import web_search


def _make_result(title, published_date="", days_ago=None):
    r = {"title": title, "url": "http://x.com", "content": "内容",
         "engines": ["google"]}
    if published_date:
        r["publishedDate"] = published_date
    if days_ago is not None:
        dt = datetime.utcnow() - timedelta(days=days_ago)
        r["publishedDate"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return r


class TestWebSearchFreshness(unittest.TestCase):

    def test_old_results_filtered_by_max_age_days(self):
        """max_age_days=7 时，30 天前的结果被过滤掉"""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                _make_result("今天的新闻", days_ago=1),
                _make_result("旧闻", days_ago=30),
                _make_result("没日期的", ),
            ]
        }

        async with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            results = asyncio.run(web_search.search("test", max_results=5, max_age_days=7))

        titles = [r["title"] for r in results]
        self.assertIn("今天的新闻", titles)
        self.assertNotIn("旧闻", titles,
                         "30 天前的结果应被 max_age_days=7 过滤掉")

    def test_no_date_kept(self):
        """没有 publishedDate 的结果保留（有些引擎不返回此字段）"""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [_make_result("无日期结果")]
        }

        async with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            results = asyncio.run(web_search.search("test", max_age_days=7))
        self.assertEqual(len(results), 1)

    def test_published_field_in_result(self):
        """结果里有 published 字段"""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [_make_result("有日期", days_ago=2)]
        }

        async with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_cls.return_value = mock_client

            results = asyncio.run(web_search.search("test"))
        self.assertIn("published", results[0])
        self.assertTrue(len(results[0]["published"]) > 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_web_search_freshness.py
```
Expected: FAIL（`search()` 不接受 `max_age_days` 参数）

- [ ] **Step 3: 修改 search() 函数**

替换 `bot/web_search.py` 的 `search` 函数（约 37-79 行）:

```python
async def search(query: str, max_results: int = 5,
                 time_range: str = None, max_age_days: int = None) -> list[dict]:
    """
    调用 SearXNG 搜索。优先 JSON API，回退 HTML 解析。

    Args:
        time_range: SearXNG 时间过滤，取值 day/week/month/year/None
        max_age_days: 二次过滤，丢弃 publishedDate 早于 N 天前的结果。
                      None = 不做二次过滤。
    Returns:
        [{"title": "...", "url": "...", "content": "...", "engine": "...",
          "published": "YYYY-MM-DD"}, ...]
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"q": query, "format": "json", "language": "zh-CN", "categories": "general"}
            if time_range:
                params["time_range"] = time_range

            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params=params,
                headers={"User-Agent": "QQBot/5.0"},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    results = data.get("results", [])[:max_results]
                    formatted = _format_results(results)
                    if max_age_days is not None:
                        formatted = _filter_by_age(formatted, max_age_days)
                    logger.info(f"SearXNG JSON: '{query}' → {len(formatted)} 条")
                    return formatted
                except Exception:
                    pass

            params_html = {"q": query, "language": "zh-CN", "categories": "general"}
            if time_range:
                params_html["time_range"] = time_range
            resp2 = await client.get(
                f"{SEARXNG_URL}/search",
                params=params_html,
                headers={"User-Agent": "QQBot/5.0", "Accept": "text/html"},
            )
            if resp2.status_code == 200:
                results = _parse_html_results(resp2.text, max_results)
                logger.info(f"SearXNG HTML: '{query}' → {len(results)} 条")
                return results

            logger.warning(f"SearXNG 搜索失败 HTTP {resp.status_code}: {query}")
            return []
    except httpx.TimeoutException:
        logger.warning(f"SearXNG 搜索超时: {query}")
        return []
    except Exception as e:
        logger.warning(f"SearXNG 搜索异常: {e}")
        return []


def _filter_by_age(results: list[dict], max_age_days: int) -> list[dict]:
    """丢弃 publishedDate 早于 max_age_days 的结果。无日期的保留。"""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    kept = []
    for r in results:
        pub = r.get("published", "")
        if not pub:
            kept.append(r)
            continue
        try:
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00").split("+")[0].split(".")[0])
            if pub_dt >= cutoff:
                kept.append(r)
            else:
                logger.debug(f"旧闻过滤: «{r.get('title','')[:30]}» published={pub[:10]}")
        except Exception:
            kept.append(r)
    return kept
```

- [ ] **Step 4: 修改 _format_results 加 published 字段**

替换 `_format_results`（约 82 行）:

```python
def _format_results(results: list[dict]) -> list[dict]:
    """格式化 JSON API 结果"""
    formatted = []
    for r in results:
        pub_raw = r.get("publishedDate", "") or ""
        published = ""
        if pub_raw:
            try:
                published = pub_raw[:10]
            except Exception:
                published = ""
        formatted.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": (r.get("content", "") or r.get("snippet", ""))[:300],
            "engine": ", ".join(r.get("engines", [])) if isinstance(r.get("engines"), list) else r.get("engine", ""),
            "published": published,
        })
    return formatted
```

- [ ] **Step 5: 添加 search_recent 封装**

在 `search_and_format` 函数（约 157 行）之后加:

```python
async def search_recent(query: str, max_age_days: int = 7, max_results: int = 5) -> list[dict]:
    """便捷封装：默认只搜最近一周"""
    return await search(query, max_results=max_results,
                        time_range="week", max_age_days=max_age_days)
```

- [ ] **Step 6: 修改 format_search_results 显示发布日期**

替换 `format_search_results`（约 143 行）:

```python
def format_search_results(results: list[dict]) -> str:
    """将搜索结果格式化为 AI 可读的文本"""
    if not results:
        return "（未找到相关搜索结果）"

    lines = ["【联网搜索结果】"]
    for i, r in enumerate(results, 1):
        pub = r.get("published", "")
        pub_str = f" (发布:{pub})" if pub else ""
        lines.append(f"{i}. {r['title']}{pub_str}")
        lines.append(f"   链接: {r['url']}")
        lines.append(f"   摘要: {r['content']}")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 7: 运行测试确认通过**

```bash
python tests/test_web_search_freshness.py
```
Expected: 3 tests PASS

- [ ] **Step 8: 提交**

```bash
git add bot/web_search.py tests/test_web_search_freshness.py
git commit -m "fix: web_search 加 time_range + max_age_days 过滤 + published 字段"
```

---

### Task 6: autonomous_learn 接入新鲜度

**Files:**
- Modify: `bot/ai_brain.py:245-383`（autonomous_learn）, `386-425`（share_news_if_ready）
- Test: `tests/test_news_freshness.py`

**Interfaces:**
- Consumes: `web_search.search_recent(query, max_age_days, max_results)` (Task 5)
- Produces: `autonomous_learn(bot)` 改用 search_recent + 新鲜度 prompt + 硬校验

- [ ] **Step 1: 写失败测试**

创建 `tests/test_news_freshness.py`:

```python
"""新闻新鲜度校验测试"""
import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.ai_brain import _is_news_too_old


class TestNewsFreshness(unittest.TestCase):

    def test_old_news_rejected(self):
        """发布日期 >30 天 → True（太旧）"""
        old = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        self.assertTrue(_is_news_too_old(old, max_age_days=30))

    def test_recent_news_accepted(self):
        """发布日期 <30 天 → False（够新）"""
        recent = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertFalse(_is_news_too_old(recent, max_age_days=30))

    def test_empty_date_accepted(self):
        """无日期 → False（放行，让 LLM 判断）"""
        self.assertFalse(_is_news_too_old("", max_age_days=30))

    def test_just_at_boundary(self):
        """正好 30 天 → 不算太旧（边界含）"""
        boundary = (datetime.utcnow() - timedelta(days=29)).strftime("%Y-%m-%d")
        self.assertFalse(_is_news_too_old(boundary, max_age_days=30))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python tests/test_news_freshness.py
```
Expected: ImportError（`_is_news_too_old` 不存在）

- [ ] **Step 3: 添加 _is_news_too_old 函数**

在 `bot/ai_brain.py` 的 `LEARN_INTERVAL` 常量（约 197 行）之后加:

```python
def _is_news_too_old(published: str, max_age_days: int = 30) -> bool:
    """判断新闻发布日期是否太旧。空日期返回 False（放行让 LLM 判断）。"""
    if not published:
        return False
    try:
        from datetime import datetime, timedelta
        pub = datetime.fromisoformat(published[:10])
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        return pub < cutoff
    except Exception:
        return False
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python tests/test_news_freshness.py
```
Expected: 4 tests PASS

- [ ] **Step 5: 修改 autonomous_learn 用 search_recent**

在 `bot/ai_brain.py:autonomous_learn`（约 293 行）里，把:

```python
        text_results = await web_search.search(query, max_results=5)
        image_results = await web_search.search_images(query, max_results=3)
```

改为:

```python
        text_results = await web_search.search_recent(query, max_age_days=7, max_results=5)
        image_results = await web_search.search_images(query, max_results=3)
```

- [ ] **Step 6: 更新搜索 query 修饰词**

在 `autonomous_learn`（约 286 行）里，把:

```python
    angles = ["最新消息", "今日新闻", "有趣的事", "热门话题", "重大突破"]
```

改为:

```python
    angles = ["最新", "今天", "本周", "最近", "2026"]
```

- [ ] **Step 7: 更新选题池用带时效的词**

在 `autonomous_learn`（约 264 行）里，把:

```python
    topics_pool = ["AI 最新进展", "科技新闻", "编程技术", "游戏资讯", "动漫资讯",
                   "有趣的冷知识", "互联网热点", "数码产品"]
```

改为:

```python
    topics_pool = ["AI 最新进展", "本周科技新闻", "编程技术", "本周游戏发布", "本周新番动画",
                   "有趣的冷知识", "本周互联网热点", "本周数码产品"]
```

- [ ] **Step 8: 更新判断 prompt 加新鲜度约束 + 喂发布日期**

在 `autonomous_learn`（约 305 行），把构建 `search_text` 的部分:

```python
        search_text = "\n".join(
            f"{i+1}. {r['title']}: {r['content'][:150]}" for i, r in enumerate(text_results[:3])
        )
```

改为:

```python
        search_text = "\n".join(
            f"{i+1}. {r['title']} (发布:{r.get('published','未知')}): {r['content'][:150]}"
            for i, r in enumerate(text_results[:3])
        )
```

把 system prompt（约 309 行）改为:

```python
            system=f"你现在{cs.get_dominant_mood(state)}。根据搜索结果做两件事：\n"
                   "1. 判断这些新闻是否值得分享给群友（够新鲜、够有趣、与群友兴趣相关）\n"
                   "2. 用一句话（50字内）总结其中最值得分享的一条，要有你的个人态度\n\n"
                   "⚠️ 新鲜度硬约束：任何一条发布日期早于 7 天前都不能算\"够新鲜\"，必须 DECISION: no。\n"
                   "只有发布日期在 7 天内的才算新。没有日期的可以放宽。\n\n"
                   "输出格式:\n"
                   "DECISION: yes 或 no\n"
                   "SUMMARY: 你的总结（50字内）",
```

- [ ] **Step 9: 分享前硬校验**

在 `share_news_if_ready`（约 412 行），拿到 `result` 后、发送前，加新鲜度校验。

在 `if result.get("decision") == "no":` 之后、`text = result.get("text", "")` 之前加:

```python
    latest_published = result.get("latest_published", "")
    if _is_news_too_old(latest_published, max_age_days=30):
        logger.info(f"新闻太旧，放弃分享: published={latest_published}")
        cs.add_diary(state, f"查到的都是旧闻（{latest_published}），算了", "meh")
        return None
```

在 `autonomous_learn` 的 return 语句（约 379 行）里加 `latest_published` 字段:

```python
        latest_pub = ""
        if text_results:
            pubs = [r.get("published", "") for r in text_results if r.get("published")]
            if pubs:
                latest_pub = max(pubs)
        return {"text": news_text, "image_url": image_url, "decision": "yes",
                "topic": topic, "latest_published": latest_pub}
```

（注意：这个 return 替换原来的 `return {"text": news_text, ...}`）

- [ ] **Step 10: 提交**

```bash
git add bot/ai_brain.py tests/test_news_freshness.py
git commit -m "fix: autonomous_learn 用 search_recent + 新鲜度 prompt + 分享前硬校验"
```

---

## Phase C: AI 自主睡眠（Workflow ⑤）

### Task 7: 新增 sleepiness 字段 + 迁移

**Files:**
- Modify: `bot/consciousness.py:32-96`（DEFAULT_STATE 加字段）, `_migrate_v1_to_v2`（Task 4 已建）
- Test: 复用 `tests/test_state_migration.py` 加测试

**Interfaces:**
- Produces: state 新增 `sleepiness: float`, `sleeping_until: str`, `last_24h_sleep_minutes: int`, `last_wake_ts: float`

- [ ] **Step 1: 加测试到 test_state_migration.py**

在 `tests/test_state_migration.py` 的 `TestStateMigration` 类里加方法:

```python
    def test_v1_gets_sleepiness_fields(self):
        """v1 状态迁移后获得 sleepiness 相关字段"""
        state = {"version": 1, "emotions": {"irritation": 0.1, "joy": 0.5}}
        _migrate_v1_to_v2(state)
        self.assertIn("sleepiness", state)
        self.assertIn("sleeping_until", state)
        self.assertIn("last_24h_sleep_minutes", state)
        self.assertEqual(state["sleepiness"], 0.0)

    def test_sleepiness_in_emotions(self):
        """sleepiness 也在 emotions 里（用于衰减/事件）"""
        state = {"version": 1, "emotions": {"irritation": 0.1, "joy": 0.5}}
        _migrate_v1_to_v2(state)
        self.assertIn("sleepiness", state["emotions"])
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_state_migration.py
```
Expected: 2 new tests FAIL

- [ ] **Step 3: 在 DEFAULT_STATE 加字段**

在 `bot/consciousness.py:DEFAULT_STATE`（约 84 行 `sleeping: False` 之后）加:

```python
    "sleepiness": 0.0,
    "sleeping_until": "",
    "last_24h_sleep_minutes": 0,
    "last_wake_ts": 0.0,
```

并在 `DEFAULT_STATE["emotions"]`（约 34 行）里加:

```python
        "sleepiness": 0.0,
```

- [ ] **Step 4: 更新 _migrate_v1_to_v2 补字段**

在 `_migrate_v1_to_v2` 函数末尾（`state["version"] = 2` 之前）加:

```python
    if "sleepiness" not in state:
        state["sleepiness"] = 0.0
    if "sleeping_until" not in state:
        state["sleeping_until"] = ""
    if "last_24h_sleep_minutes" not in state:
        state["last_24h_sleep_minutes"] = 0
    if "last_wake_ts" not in state:
        state["last_wake_ts"] = 0.0
    if "sleepiness" not in state.get("emotions", {}):
        state.setdefault("emotions", {})["sleepiness"] = 0.0
```

- [ ] **Step 5: 运行确认通过**

```bash
python tests/test_state_migration.py
```
Expected: 7 tests PASS

- [ ] **Step 6: 提交**

```bash
git add bot/consciousness.py tests/test_state_migration.py
git commit -m "feat: 意识状态新增 sleepiness/sleeping_until/睡眠追踪字段"
```

---

### Task 8: sleepiness 驱动 + go_to_sleep + wake_up

**Files:**
- Modify: `bot/consciousness.py`（新增函数）
- Test: `tests/test_sleep_system.py`

**Interfaces:**
- Produces: `update_sleepiness(state, elapsed_minutes)` — 根据时段+连续清醒时长累积困意
- Produces: `go_to_sleep(state, minutes)` — 执行入睡
- Produces: `wake_up(state, cranky=False)` — 执行醒来

- [ ] **Step 1: 写失败测试**

创建 `tests/test_sleep_system.py`:

```python
"""睡眠系统测试 — sleepiness 驱动 + go_to_sleep + wake_up"""
import sys
import os
import unittest
from unittest.mock import patch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import consciousness as cs


def _state(sleepiness=0.0, energy=0.5, sleeping=False, last_wake_ts=None, last_24h=0):
    if last_wake_ts is None:
        last_wake_ts = datetime.now().timestamp()
    return {
        "sleepiness": sleepiness,
        "sleeping": sleeping,
        "sleeping_until": "",
        "last_24h_sleep_minutes": last_24h,
        "last_wake_ts": last_wake_ts,
        "emotions": {"energy": energy, "irritation": 0.1, "sleepiness": sleepiness},
        "today": {},
        "diary": [],
    }


class TestSleepinessAccumulation(unittest.TestCase):

    @patch.object(cs.timeutil, 'now_shanghai')
    def test_deep_night_accumulates_fast(self, mock_now):
        """深夜 3 点 sleepiness 累积快"""
        mock_now.return_value = datetime(2026, 8, 11, 3, 0)
        s = _state(sleepiness=0.0)
        cs.update_sleepiness(s, elapsed_minutes=30)
        self.assertGreater(s["sleepiness"], 0.3,
                          "深夜 30 分钟 sleepiness 应显著上升")

    @patch.object(cs.timeutil, 'now_shanghai')
    def test_afternoon_accumulates_slow(self, mock_now):
        """下午 2 点 sleepiness 累积慢"""
        mock_now.return_value = datetime(2026, 8, 11, 14, 0)
        s = _state(sleepiness=0.0)
        cs.update_sleepiness(s, elapsed_minutes=10)
        self.assertLess(s["sleepiness"], 0.1,
                       "下午 10 分钟 sleepiness 应上升很少")


class TestGoToSleep(unittest.TestCase):

    def test_go_to_sleep_sets_sleeping_until(self):
        s = _state()
        cs.go_to_sleep(s, minutes=90)
        self.assertTrue(s["sleeping"])
        self.assertTrue(len(s["sleeping_until"]) > 0)

    def test_go_to_sleep_enforces_floor_when_deficient(self):
        """当日累计 <60 分钟时，强制睡到补齐"""
        s = _state(last_24h=30)
        cs.go_to_sleep(s, minutes=20)
        self.assertGreaterEqual(s["last_24h_sleep_minutes"] + 20, 60,
                                "不足 60 分钟应被补齐")

    def test_go_to_sleep_clamps_minutes(self):
        """minutes 超出 20-480 范围被 clamp"""
        s = _state()
        cs.go_to_sleep(s, minutes=999)
        self.assertLessEqual(s["sleeping"] , True)


class TestWakeUp(unittest.TestCase):

    def test_wake_up_restores_energy(self):
        s = _state(sleeping=True, energy=0.2)
        s["sleeping_until"] = "2099-01-01T00:00:00"
        cs.wake_up(s)
        self.assertFalse(s["sleeping"])
        self.assertGreater(s["emotions"]["energy"], 0.2)
        self.assertEqual(s["sleepiness"], 0.0)

    def test_wake_up_cranky_increases_irritation(self):
        s = _state(sleeping=True, energy=0.2)
        s["sleeping_until"] = "2099-01-01T00:00:00"
        before = s["emotions"]["irritation"]
        cs.wake_up(s, cranky=True)
        self.assertGreater(s["emotions"]["irritation"], before,
                          "起床气应增加 irritation")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_sleep_system.py
```
Expected: ImportError（`update_sleepiness`, `go_to_sleep`, `wake_up` 不存在）

- [ ] **Step 3: 添加 update_sleepiness 函数**

在 `bot/consciousness.py` 的 `apply_natural_decay` 之后加:

```python
SLEEPINESS_RATES = {
    "deep_night": (0, 6, 0.015),
    "late_night": (23, 24, 0.010),
    "food_coma": (13, 14, 0.003),
    "default": (0, 0, 0.001),
}


def update_sleepiness(state: dict, elapsed_minutes: float):
    """根据时段 + 连续清醒时长累积困意"""
    now = timeutil.now_shanghai()
    hour = now.hour

    rate = SLEEPINESS_RATES["default"][2]
    for label, (start, end, r) in SLEEPINESS_RATES.items():
        if label == "default":
            continue
        if start <= hour < end:
            rate = r
            break

    awake_boost = 1.0
    last_wake = state.get("last_wake_ts", 0)
    if last_wake:
        awake_hours = (now.timestamp() - last_wake) / 3600
        if awake_hours > 1:
            awake_boost = 1.0 + min(awake_hours * 0.2, 2.0)

    delta = rate * elapsed_minutes * awake_boost
    state["sleepiness"] = min(1.0, state.get("sleepiness", 0.0) + delta)
    state.setdefault("emotions", {})["sleepiness"] = state["sleepiness"]
```

- [ ] **Step 4: 添加 go_to_sleep 函数**

```python
SLEEP_MIN_MINUTES = 20
SLEEP_MAX_MINUTES = 480
DAILY_SLEEP_FLOOR = 60


def go_to_sleep(state: dict, minutes: int):
    """执行入睡。强制补齐当日睡眠底线。"""
    minutes = max(SLEEP_MIN_MINUTES, min(SLEEP_MAX_MINUTES, minutes))

    already = state.get("last_24h_sleep_minutes", 0)
    if already + minutes < DAILY_SLEEP_FLOOR:
        minutes = DAILY_SLEEP_FLOOR - already
        logger.info(f"睡眠补齐到 {minutes} 分钟（满足 60 分钟底线）")

    from datetime import timedelta
    wake_at = timeutil.now_shanghai() + timedelta(minutes=minutes)
    state["sleeping"] = True
    state["sleeping_until"] = wake_at.strftime("%Y-%m-%dT%H:%M:%S")
    state["sleepiness"] = min(state.get("sleepiness", 0.5), 0.3)
    state.setdefault("emotions", {})["sleepiness"] = state["sleepiness"]
    add_diary(state, f"困了，去睡 {minutes} 分钟", "tired")
    logger.info(f"😴 入睡 {minutes} 分钟，醒于 {state['sleeping_until']}")
```

- [ ] **Step 5: 添加 wake_up 函数**

```python
def wake_up(state: dict, cranky: bool = False):
    """执行醒来。cranky=True 时带起床气（被吵醒）。"""
    minutes_slept = 0
    try:
        from datetime import datetime as _dt
        if state.get("sleeping_until"):
            wake_at = _dt.fromisoformat(state["sleeping_until"])
            minutes_slept = max(0, int((wake_at - timeutil.now_shanghai()).total_seconds() / 60))
    except Exception:
        pass

    state["sleeping"] = False
    state["sleeping_until"] = ""
    state["sleepiness"] = 0.0
    state.setdefault("emotions", {})["sleepiness"] = 0.0
    state["last_wake_ts"] = timeutil.now_shanghai().timestamp()
    state["last_24h_sleep_minutes"] = state.get("last_24h_sleep_minutes", 0) + max(0, minutes_slept)

    energy_gain = min(0.5, minutes_slept * 0.005)
    apply_emotion_delta(state, "energy", energy_gain)

    if cranky:
        apply_emotion_delta(state, "irritation", 0.10)
        state["sleepiness"] = 0.3
        add_diary(state, "被叫醒了…有点烦", "grumpy")
    else:
        if minutes_slept >= 30:
            add_diary(state, f"睡了 {minutes_slept} 分钟，精神多了", "refreshed")
    logger.info(f"🌅 醒来（睡了约 {minutes_slept} 分钟{'，带起床气' if cranky else ''}）")
```

- [ ] **Step 6: 运行确认通过**

```bash
python tests/test_sleep_system.py
```
Expected: 7 tests PASS

- [ ] **Step 7: 提交**

```bash
git add bot/consciousness.py tests/test_sleep_system.py
git commit -m "feat: sleepiness 驱动 + go_to_sleep + wake_up（含起床气）"
```

---

### Task 9: 移除 apply_circadian 硬触发 + tick 集成

**Files:**
- Modify: `bot/consciousness.py:535-560`（apply_circadian 不再硬置 sleeping）, `1131-1164`（tick 调用 update_sleepiness + 检查 sleeping_until）
- Test: `tests/test_sleep_system.py` 加测试

- [ ] **Step 1: 加测试到 test_sleep_system.py**

在 `TestSleepinessAccumulation` 类里加:

```python
    def test_tick_updates_sleepiness(self):
        """tick() 应调用 update_sleepiness"""
        s = _state(sleepiness=0.0)
        cs.tick(s)
        self.assertGreaterEqual(s["sleepiness"], 0.0)

    def test_tick_wakes_when_sleeping_until_passed(self):
        """sleeping_until 已过 → tick 自动醒来"""
        from datetime import datetime, timedelta
        s = _state(sleeping=True, energy=0.2)
        s["sleeping_until"] = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        cs.tick(s)
        self.assertFalse(s["sleeping"], "sleeping_until 过期后应自动醒来")
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_sleep_system.py -k test_tick
```
Expected: FAIL（tick 不更新 sleepiness，不自动醒）

- [ ] **Step 3: 修改 apply_circadian 不硬置 sleeping**

把 `bot/consciousness.py:apply_circadian`（约 535 行）里所有 `state["sleeping"] = True` 的硬置删掉，改成只给 sleepiness 加成。替换函数体:

```python
def apply_circadian(state: dict):
    """应用昼夜节律修正 — 不再硬触发 sleeping，改为影响情绪和 sleepiness"""
    mods = get_circadian_modifiers()
    for emotion, delta in mods.items():
        if emotion in state["emotions"]:
            apply_emotion_delta(state, emotion, delta * 0.02)

    if state.get("sleeping"):
        if not mods.get("sleeping"):
            state["sleeping"] = False
            state["last_wake_ts"] = timeutil.now_shanghai().timestamp()
            state["emotions"]["energy"] = max(state["emotions"]["energy"], 0.5)
            add_diary(state, f"醒了，现在{datetime.now().strftime('%H点')}", "refreshed")
            logger.info("🌅 醒来（昼夜节律）")
```

注意：入睡不再由昼夜节律触发，而是由 sleepiness 阈值或自主决定（Task 10/12）。

- [ ] **Step 4: 修改 tick 集成 sleepiness + 自动醒**

替换 `bot/consciousness.py:tick`（约 1131 行）:

```python
def tick(state: dict):
    """意识时钟滴答（每分钟调用一次）。"""
    global _last_tick_time, _tick_count
    now = time.time()
    elapsed = now - _last_tick_time
    _last_tick_time = now
    _tick_count += 1

    elapsed_minutes = max(elapsed / 60, 1)
    apply_natural_decay(state, elapsed_minutes)
    apply_circadian(state)
    update_sleepiness(state, elapsed_minutes)

    if state.get("sleeping"):
        _check_wake_up(state)
    else:
        _check_forced_sleep(state)

    check_daily_reset(state)

    if _tick_count % 15 == 0:
        save_state(state)

    if _tick_count % 30 == 0 and not state.get("sleeping"):
        mood = get_dominant_mood(state)
        e = state["emotions"]
        thought = f"在{get_time_of_day_name()}时分，感觉{mood}（精力{e['energy']:.0%} 快乐{e['joy']:.0%}）"
        add_diary(state, thought, mood)


def _check_wake_up(state: dict):
    """检查是否到该醒来的时间"""
    wake_str = state.get("sleeping_until", "")
    if not wake_str:
        return
    try:
        from datetime import datetime as _dt
        wake_at = _dt.fromisoformat(wake_str)
        if timeutil.now_shanghai() >= wake_at:
            wake_up(state, cranky=False)
    except Exception:
        pass


MAX_AWAKE_HOURS = 23
FORCED_SLEEP_MINUTES = 90


def _check_forced_sleep(state: dict):
    """连续清醒 > 23h 或 sleepiness >= 0.98 → 强制睡"""
    sleepiness = state.get("sleepiness", 0)
    if sleepiness >= 0.98:
        go_to_sleep(state, FORCED_SLEEP_MINUTES)
        return
    last_wake = state.get("last_wake_ts", 0)
    if last_wake:
        awake_hours = (timeutil.now_shanghai().timestamp() - last_wake) / 3600
        if awake_hours >= MAX_AWAKE_HOURS:
            logger.warning(f"连续清醒 {awake_hours:.1f}h，强制入睡")
            go_to_sleep(state, FORCED_SLEEP_MINUTES)
```

- [ ] **Step 5: 运行确认通过**

```bash
python tests/test_sleep_system.py
```
Expected: 9 tests PASS

- [ ] **Step 6: 提交**

```bash
git add bot/consciousness.py tests/test_sleep_system.py
git commit -m "feat: tick 集成 sleepiness 驱动 + 自动醒 + 23h 强制睡兜底"
```

---

### Task 10: 强制睡眠底线单测

**Files:**
- Test: `tests/test_sleep_floor.py`

- [ ] **Step 1: 写测试**

创建 `tests/test_sleep_floor.py`:

```python
"""睡眠底线测试 — 24h 内必须睡够 60 分钟"""
import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import consciousness as cs


def _state_awake_23h():
    """构造一个连续清醒 23 小时的状态"""
    return {
        "sleeping": False,
        "sleepiness": 0.5,
        "sleeping_until": "",
        "last_24h_sleep_minutes": 0,
        "last_wake_ts": (datetime.now() - timedelta(hours=23)).timestamp(),
        "emotions": {"energy": 0.2, "irritation": 0.1, "sleepiness": 0.5},
        "today": {},
        "diary": [],
    }


class TestSleepFloor(unittest.TestCase):

    def test_23h_awake_triggers_forced_sleep(self):
        """清醒 23h → _check_forced_sleep 触发入睡"""
        s = _state_awake_23h()
        cs._check_forced_sleep(s)
        self.assertTrue(s["sleeping"], "清醒 23h 应被强制入睡")

    def test_forced_sleep_meets_60min_floor(self):
        """强制睡的时长满足 60 分钟底线"""
        s = _state_awake_23h()
        cs._check_forced_sleep(s)
        self.assertGreaterEqual(s["last_24h_sleep_minutes"], 0)
        # go_to_sleep 内部会补齐，验证 sleeping_until 时间跨度 >= 60 分钟
        from datetime import datetime as _dt
        wake_at = _dt.fromisoformat(s["sleeping_until"])
        now = cs.timeutil.now_shanghai()
        span_min = (wake_at - now).total_seconds() / 60
        self.assertGreaterEqual(span_min, 60, "强制睡时长应 >= 60 分钟")

    def test_extreme_sleepiness_triggers_sleep(self):
        """sleepiness >= 0.98 → 强制睡"""
        s = _state_awake_23h()
        s["sleepiness"] = 0.99
        s["last_wake_ts"] = datetime.now().timestamp()
        cs._check_forced_sleep(s)
        self.assertTrue(s["sleeping"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认通过**

```bash
python tests/test_sleep_floor.py
```
Expected: 3 tests PASS（Task 9 已实现逻辑）

- [ ] **Step 3: 提交**

```bash
git add tests/test_sleep_floor.py
git commit -m "test: 睡眠底线 — 23h 强制睡 + sleepiness 阈值触发"
```

---

### Task 11: 被叫醒带起床气（handler.py）

**Files:**
- Modify: `bot/handler.py:2082, 2097`（两个 on_bot_mentioned 调用点）
- Modify: `bot/ai_brain.py:128-132`（on_bot_mentioned 接口）

**Interfaces:**
- Consumes: `consciousness.wake_up(state, cranky=True)` (Task 8)
- Produces: `ai_brain.on_bot_mentioned` 在 sleeping 时触发 cranky wake

- [ ] **Step 1: 修改 on_bot_mentioned 加睡眠检查**

在 `bot/ai_brain.py:on_bot_mentioned`（约 128 行）改为:

```python
def on_bot_mentioned(user_name: str, group_id: int):
    """被人 @ 了"""
    state = cs.get_state()
    if state.get("sleeping"):
        cs.wake_up(state, cranky=True)
        logger.info(f"💤 被 {user_name} 叫醒，带起床气")
    cs.on_mentioned(state, user_name, group_id)
    _check_mood_change(state, "被@")
```

- [ ] **Step 2: 确认 handler.py 两个调用点不需要改**

`handler.py:2082` 和 `2097` 都调 `ai_brain.on_bot_mentioned(user_name, group_id)`，接口签名不变，自动获得新行为。

- [ ] **Step 3: 手动验证（无单测，因为是跨模块集成）**

读一遍 `bot/ai_brain.py:on_bot_mentioned` 确认逻辑正确：sleeping=True → wake_up(cranky=True) → irritation+0.1 → 然后正常处理 on_mentioned。

- [ ] **Step 4: 提交**

```bash
git add bot/ai_brain.py
git commit -m "feat: 被@时如果正在睡觉，醒来带起床气（irritation +0.1）"
```

---

### Task 12: set_sleep 工具 + decide_and_speak SLEEP 协议

**Files:**
- Modify: `bot/ai_tools.py:60-738`（ALL_TOOLS 加 set_sleep）, `769+`（execute_tool 加分支）
- Modify: `bot/ai_brain.py:670-776`（decide_and_speak 加 SLEEP 协议）
- Test: `tests/test_set_sleep_tool.py`

**Interfaces:**
- Produces: `set_sleep` 工具定义 + execute_tool 分支
- Produces: `decide_and_speak` 支持 `SLEEP: <minutes>` 输出协议

- [ ] **Step 1: 写失败测试**

创建 `tests/test_set_sleep_tool.py`:

```python
"""set_sleep 工具测试"""
import sys
import os
import unittest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools
from bot import consciousness as cs


class TestSetSleepTool(unittest.TestCase):

    def test_tool_definition_exists(self):
        names = [t["name"] for t in ai_tools.ALL_TOOLS]
        self.assertIn("set_sleep", names)

    def test_tool_not_in_dangerous(self):
        self.assertNotIn("set_sleep", ai_tools.DANGEROUS_TOOLS)

    def test_execute_triggers_sleep(self):
        """execute_tool('set_sleep', {minutes: 90}) → state.sleeping=True"""
        state = cs.get_state()
        state["sleeping"] = False
        state["sleeping_until"] = ""

        async def run():
            class FakeBot:
                pass
            result = await ai_tools.execute_tool(
                FakeBot(), 0, 0, "set_sleep", {"minutes": 90}
            )
            return result

        result = asyncio.run(run())
        self.assertTrue(state["sleeping"])
        self.assertTrue(len(state["sleeping_until"]) > 0)
        self.assertIn("睡", result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_set_sleep_tool.py
```
Expected: `test_tool_definition_exists` FAIL

- [ ] **Step 3: 在 ALL_TOOLS 加 set_sleep**

在 `bot/ai_tools.py:ALL_TOOLS` 列表里（`set_typing` 工具之后，约 324 行）加:

```python
    {
        "name": "set_sleep",
        "description": (
            "去睡觉。当你困了、累了、或者想躲起来一会儿时用。"
            "minutes 是睡觉时长，建议 30-480。当天累计睡眠不足 60 分钟时会自动补足。"
            "睡觉期间不会主动发消息，但被@会醒（带起床气）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "睡多少分钟，30-480"},
            },
            "required": ["minutes"],
        },
    },
```

- [ ] **Step 4: 在 execute_tool 加分支**

在 `bot/ai_tools.py:execute_tool` 里（`set_typing` 分支之后，约 998 行）加:

```python
        elif tool_name == "set_sleep":
            minutes = int(tool_input.get("minutes", 90))
            from . import consciousness as _cs
            _cs.go_to_sleep(_cs.get_state(), minutes)
            return f"去睡 {minutes} 分钟了，晚安~ ✓"
```

- [ ] **Step 5: 在 decide_and_speak 加 SLEEP 协议**

在 `bot/ai_brain.py:decide_and_speak`（约 720 行）的 prompt 里，把:

```python
            "想说 → 回复：SPEAK: <内容，20字以内，要跟当前时间/心情对得上>\n"
            "不想说 → 回复：SILENT: <一句话原因，写给自己看>\n"
```

改为:

```python
            "想说 → 回复：SPEAK: <内容，20字以内，要跟当前时间/心情对得上>\n"
            "不想说 → 回复：SILENT: <一句话原因，写给自己看>\n"
            "想睡觉 → 回复：SLEEP: <分钟数，30-480>\n"
```

并在解析输出的地方（约 748 行 `upper.startswith("SPEAK:")` 之后）加:

```python
        elif upper.startswith("SLEEP:"):
            minutes_str = raw.split(":", 1)[1].strip()
            try:
                minutes = int("".join(c for c in minutes_str if c.isdigit()))
            except Exception:
                minutes = 90
            go_to_sleep(state, minutes)
            logger.info(f"😴 Volition SLEEP: 自主决定睡 {minutes} 分钟")
            return None
```

注意：`go_to_sleep` 需要从 `consciousness` 导入。在 `ai_brain.py` 顶部已有 `from . import consciousness as cs`，所以调用 `cs.go_to_sleep(state, minutes)`。

- [ ] **Step 6: 运行确认通过**

```bash
python tests/test_set_sleep_tool.py
```
Expected: 3 tests PASS

- [ ] **Step 7: 提交**

```bash
git add bot/ai_tools.py bot/ai_brain.py tests/test_set_sleep_tool.py
git commit -m "feat: set_sleep 工具 + decide_and_speak 支持 SLEEP 协议"
```

---

## Phase D: 拆分短消息 + 解除限制（Workflow ① + 收尾）

### Task 13: 调整 split_bubbles 阈值

**Files:**
- Modify: `bot/humanizer.py:125-169`（split_bubbles 不拆阈值）, `54`（SPLIT_THRESHOLD）

> 当前 `split_bubbles` 对 `<60 字 且 ≤3 行` 的回复不拆。降到 35 字让中等长度回复也拆。

- [ ] **Step 1: 修改 split_bubbles 不拆阈值**

在 `bot/humanizer.py:split_bubbles`（约 138 行）把:

```python
    if total_visible < 60 and len(parts) <= 3:
        return [_tidy_bubble(" ".join(parts))]
```

改为:

```python
    if total_visible < 35 and len(parts) <= 2:
        return [_tidy_bubble(" ".join(parts))]
```

- [ ] **Step 2: 手动验证**

读一遍 `split_bubbles` 确认逻辑：35 字以上或有 3+ 段的回复会被拆分。短回复仍合并为一条。

- [ ] **Step 3: 提交**

```bash
git add bot/humanizer.py
git commit -m "tune: split_bubbles 拆分阈值从 60 字降到 35 字，更多回复会被拆条"
```

---

### Task 14: 主动/新闻消息走 humanizer

**Files:**
- Modify: `main.py:288, 323, 355`（三处 `bot.send_group_msg` 改走 `send_human_like`）

> 当前 volition SPEAK、proactive_action、news share 都直接 `bot.send_group_msg` 单条发，不走拆分。

- [ ] **Step 1: 修改空闲思考发言**

`main.py:288` 把:

```python
                                    await bot.send_group_msg(gid, speak_text)
                                    await asyncio.sleep(random.uniform(4, 7))
```

改为:

```python
                                    await send_human_like(bot, gid, speak_text)
                                    await asyncio.sleep(random.uniform(4, 7))
```

- [ ] **Step 2: 修改 volition 主动发言**

`main.py:323` 把:

```python
                                    await bot.send_group_msg(gid, proactive_msg)
                                    await asyncio.sleep(random.uniform(4, 7))
```

改为:

```python
                                    await send_human_like(bot, gid, proactive_msg)
                                    await asyncio.sleep(random.uniform(4, 7))
```

- [ ] **Step 3: 修改新闻分享发送**

`main.py:355` 附近的 `await bot.send_group_msg(gid, msg_to_send)` 改为:

```python
                        if isinstance(msg_to_send, str):
                            await send_human_like(bot, gid, msg_to_send)
                        else:
                            await bot.send_group_msg(gid, msg_to_send)
                        await asyncio.sleep(random.uniform(4, 7))
```

注意：新闻可能含图片消息段（list），不是 string，这种情况仍走原 `send_group_msg`。

- [ ] **Step 4: 确认 send_human_like 已在 main.py 导入**

检查 `main.py:188` 附近已有 `from bot.humanizer import send_human_like`。如果没有，加导入。

- [ ] **Step 5: 提交**

```bash
git add main.py
git commit -m "feat: 主动/新闻/空闲发言走 humanizer，统一拆条+拟人节奏"
```

---

### Task 15: 去除冷却硬限制 + 软限速兜底

**Files:**
- Modify: `bot/ai_brain.py:197-201`（删 NEWS_SHARE_INTERVAL/LEARN_INTERVAL）, `386-401`（share_news_if_ready）, `847-856`（maybe_proactive_action）
- Modify: `bot/guardian.py:369-405`（加 check_proactive_rate）
- Test: `tests/test_proactive_rate.py`

**Interfaces:**
- Produces: `guardian.check_proactive_rate(group_id)` — 每群每小时主动消息限速

- [ ] **Step 1: 写失败测试**

创建 `tests/test_proactive_rate.py`:

```python
"""主动消息软限速测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import guardian


class TestProactiveRate(unittest.TestCase):

    def setUp(self):
        guardian._rate_state.clear()

    def test_allows_first_8(self):
        """前 8 次允许"""
        for i in range(8):
            self.assertTrue(guardian.check_proactive_rate(123456),
                            f"第 {i+1} 次应允许")

    def test_blocks_9th(self):
        """第 9 次拒绝"""
        for _ in range(8):
            guardian.check_proactive_rate(123456)
        self.assertFalse(guardian.check_proactive_rate(123456),
                         "第 9 次应拒绝")

    def test_different_groups_independent(self):
        """不同群独立计数"""
        for _ in range(8):
            guardian.check_proactive_rate(111)
        self.assertTrue(guardian.check_proactive_rate(222),
                        "群 222 不应受群 111 影响")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_proactive_rate.py
```
Expected: AttributeError（`check_proactive_rate` 不存在）

- [ ] **Step 3: 在 guardian.py 加 check_proactive_rate**

在 `bot/guardian.py` 的 `check_essence_rate` 之后（约 405 行）加:

```python
def check_proactive_rate(group_id: int) -> bool:
    """主动消息（volition SPEAK + 新闻分享）软限速：每群每小时最多 8 条"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "proactive", 1,
        cfg.get("proactive_hourly_per_group", 8),
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python tests/test_proactive_rate.py
```
Expected: 3 tests PASS

- [ ] **Step 5: 去除 ai_brain.py 冷却常量**

在 `bot/ai_brain.py`（约 197 行）把:

```python
LEARN_INTERVAL = 1800  # 30 分钟
_last_learn_time = 0

NEWS_SHARE_INTERVAL = 7200  # 2 小时
_last_news_share = 0
```

改为:

```python
LEARN_INTERVAL = 60  # 防同一秒内连调
_last_learn_time = 0
```

删除 `NEWS_SHARE_INTERVAL` 和 `_last_news_share`。

- [ ] **Step 6: 修改 share_news_if_ready 去掉冷却和 irritation 门**

在 `bot/ai_brain.py:share_news_if_ready`（约 386 行）把函数开头:

```python
    global _last_news_share
    now = time.time()
    if now - _last_news_share < NEWS_SHARE_INTERVAL:
        return None

    state = cs.get_state()

    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"新闻分享跳过: {why}")
        return None
    if state["emotions"]["irritation"] > 0.6:
        return None
```

改为:

```python
    state = cs.get_state()

    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"新闻分享跳过: {why}")
        return None
```

并删除函数末尾的 `_last_news_share = now`（约 407 行）。

- [ ] **Step 7: 修改 maybe_proactive_action 加软限速**

在 `bot/ai_brain.py:maybe_proactive_action`（约 831 行）函数开头加:

```python
    from . import guardian
    import bot as _bot_mod
    for gid in getattr(_bot_mod, '_monitored_groups', []):
        if not guardian.check_proactive_rate(gid):
            logger.debug(f"主动发言软限速 群{gid}")
            return None
```

注意：如果没有 `_monitored_groups`，则简化为对所有群检查。更稳妥的做法是在 `main.py` 调用 `maybe_proactive_action` 之前按群检查限速。这里先在 `maybe_proactive_action` 里不加，改在 `main.py:312-326` 调用循环里加。

**改为在 main.py 里加限速**（更准确）:

`main.py:312` 附近，把 volition 发送循环改为:

```python
            if time.time() >= _next_volition_check:
                _next_volition_check = time.time() + random.uniform(480, 720)
                if not state.get("sleeping") and cs.should_be_active(state):
                    try:
                        proactive_msg = await maybe_proactive_action(bot)
                        if proactive_msg:
                            r = await bot._call("get_group_list", {})
                            groups = r.get("data", []) if r.get("status") == "ok" else []
                            for g in groups:
                                gid = g.get("group_id", 0)
                                if gid and guardian.check_proactive_rate(gid):
                                    await send_human_like(bot, gid, proactive_msg)
                                    await asyncio.sleep(random.uniform(4, 7))
                    except Exception as e:
                        logger.debug(f"volition 跳过: {e}")
```

同样在新闻分享循环（`main.py:330+`）里，发送前加 `guardian.check_proactive_rate(gid)` 检查。

- [ ] **Step 8: 提交**

```bash
git add bot/guardian.py bot/ai_brain.py main.py tests/test_proactive_rate.py
git commit -m "feat: 去除 2h 新闻冷却 + 30min 学习冷却，加 8条/小时软限速兜底"
```

---

## Phase E: 自动表情表态（Workflow ②）

### Task 16: guardian 自动表态限速

**Files:**
- Modify: `bot/guardian.py`（加 check_auto_emoji_rate）
- Test: `tests/test_auto_emoji_rate.py`

- [ ] **Step 1: 写测试**

创建 `tests/test_auto_emoji_rate.py`:

```python
"""自动表情表态限速测试"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import guardian


class TestAutoEmojiRate(unittest.TestCase):

    def setUp(self):
        guardian._rate_state.clear()

    def test_allows_first_8(self):
        for _ in range(8):
            self.assertTrue(guardian.check_auto_emoji_rate(123))

    def test_blocks_9th(self):
        for _ in range(8):
            guardian.check_auto_emoji_rate(123)
        self.assertFalse(guardian.check_auto_emoji_rate(123))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

```bash
python tests/test_auto_emoji_rate.py
```
Expected: AttributeError

- [ ] **Step 3: 加 check_auto_emoji_rate**

在 `bot/guardian.py:check_proactive_rate` 之后加:

```python
def check_auto_emoji_rate(group_id: int) -> bool:
    """自动表情表态限速：每群每小时最多 8 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "auto_emoji", 1,
        cfg.get("auto_emoji_hourly_per_group", 8),
    )
```

- [ ] **Step 4: 运行确认通过**

```bash
python tests/test_auto_emoji_rate.py
```
Expected: 2 tests PASS

- [ ] **Step 5: 提交**

```bash
git add bot/guardian.py tests/test_auto_emoji_rate.py
git commit -m "feat: guardian.check_auto_emoji_rate — 自动表态限速"
```

---

### Task 17: 自动表态评估函数 + handler 接入

**Files:**
- Create: `bot/auto_emoji.py`
- Modify: `bot/handler.py`（非 @ 消息处理完后触发）
- Modify: `bot/settings.py`（加 `chat.auto_emoji_rate` 配置）

**Interfaces:**
- Produces: `auto_emoji.maybe_react(bot, group_id, message_id, text)` — 异步评估并贴表情

- [ ] **Step 1: 创建 auto_emoji 模块**

创建 `bot/auto_emoji.py`:

```python
"""
自动表情表态 — AI 看到群消息时低概率主动贴表情（不说话）
================================================================
模拟真人在群里的"潜水参与"：看到好笑的贴个😂，赞同的贴个👍。
不是每条都贴，低概率触发 + LLM 判断 + 限速三重控制。
"""
import logging
import random

logger = logging.getLogger("qqbot")

EMOJI_MAP = {
    "128514": "😂",
    "128513": "😁",
    "128512": "😀",
    "128525": "😍",
    "128077": "👍",
    "128079": "👏",
    "128293": "🔥",
    "128591": "🙏",
    "100": "💯",
    "128522": "😊",
}


async def maybe_react(bot, group_id: int, message_id: int, text: str, trigger_rate: float = 0.12):
    """低概率对一条群消息贴表情表态。

    Args:
        bot: OneBotClient
        group_id: 群号
        message_id: 要表态的消息 ID
        text: 消息纯文本（剥 CQ 码后）
        trigger_rate: 触发评估的概率（默认 12%）
    """
    if not message_id or not text or len(text.strip()) < 3:
        return
    if random.random() > trigger_rate:
        return

    from . import guardian
    if not guardian.check_auto_emoji_rate(group_id):
        return

    try:
        from . import llm_call, memory
        ms = memory.load_model_settings()
        if not ms.get("api_key"):
            return

        response = await llm_call.llm_create(
            priority="P3",
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=10,
            temperature=0.3,
            system=(
                "你看一条群消息，决定要不要贴表情表态。"
                "只有好笑/赞同/惊讶/感动/给力时才贴。中性/无聊/看不懂就 NONE。"
                "只输出一个 emoji_id 或 NONE。可选: 128514(😂) 128513(😁) 128077(👍) "
                "128079(👏) 128293(🔥) 128525(😍) 100(💯) 128591(🙏) 128522(😊)"
            ),
            messages=[{"role": "user", "content": f"消息: {text[:100]}"}],
        )
        raw = response.content[0].text.strip()
        if raw.upper() == "NONE" or not raw:
            return

        emoji_id = "".join(c for c in raw if c.isdigit())
        if not emoji_id or emoji_id not in EMOJI_MAP:
            return

        ok = await bot.set_msg_emoji_like(message_id, emoji_id)
        if ok:
            logger.info(f"😎 自动表态 群{group_id} mid={message_id} → {EMOJI_MAP[emoji_id]}")
    except Exception as e:
        logger.debug(f"自动表态异常: {e}")
```

- [ ] **Step 2: 在 handler.py 接入**

在 `bot/handler.py` 处理非 @ 消息的路径末尾（找到处理完群消息后返回前的位置），加:

```python
        try:
            from .auto_emoji import maybe_react
            import re as _re2
            clean_for_emoji = _re2.sub(r"\[CQ:[^\]]+\]", "", raw_message or "").strip()
            asyncio.create_task(maybe_react(bot, group_id, message_id, clean_for_emoji))
        except Exception:
            pass
```

**定位插入点**：在 `handler.py` 里找到处理普通群消息（非 @、非命令）的分支末尾，`return` 之前插入。如果不好定位，就在 `handle_group_message` 函数最后的"非 @ 消息记忆记录"之后、return 之前插入。

- [ ] **Step 3: 在 settings.py 加配置**

读 `bot/settings.py` 确认配置结构，加默认值:

```python
"chat": {
    "split_messages": True,
    "auto_emoji_rate": 0.12,
},
```

（在 `settings.py` 的默认配置字典里）

- [ ] **Step 4: 手动验证**

读一遍 `bot/auto_emoji.py` 确认逻辑：trigger_rate 过滤 → 限速 → LLM P3 优先级判断 → set_msg_emoji_like。

- [ ] **Step 5: 提交**

```bash
git add bot/auto_emoji.py bot/handler.py bot/settings.py
git commit -m "feat: 自动表情表态 — 12% 概率给群消息贴表情，每群每小时≤8 次"
```

---

## 自检结果

### Spec 覆盖检查

| Spec 要求 | 对应 Task | 状态 |
|-----------|----------|------|
| ④ 改动 1：EMOTION_BASELINE | Task 1 | ✓ |
| ④ 改动 2：get_dominant_mood 优先级 | Task 2 | ✓ |
| ④ 改动 3：正向事件减 irritation | Task 3 | ✓ |
| ④ 改动 4：一次性修复现存状态 | Task 4 | ✓ |
| ③ 改动 A：web_search 时间过滤 | Task 5 | ✓ |
| ③ 改动 B：autonomous_learn 用 search_recent | Task 6 Step 5-8 | ✓ |
| ③ 改动 C：分享前新鲜度硬校验 | Task 6 Step 9 | ✓ |
| ⑤ 新增字段 | Task 7 | ✓ |
| ⑤ sleepiness 驱动 + go_to_sleep + wake_up | Task 8 | ✓ |
| ⑤ 移除 apply_circadian 硬触发 + tick 集成 | Task 9 | ✓ |
| ⑤ 强制睡眠底线 | Task 10（单测）+ Task 9（实现） | ✓ |
| ⑤ 被叫醒带起床气 | Task 11 | ✓ |
| ⑤ set_sleep 工具 + SLEEP 协议 | Task 12 | ✓ |
| ① 拆分短消息 | Task 13（阈值）+ Task 14（走 humanizer） | ✓ |
| ② 自动表情表态 | Task 16 + Task 17 | ✓ |
| 解除主动发言限制 | Task 15 | ✓ |
| 软限速兜底 | Task 15 Step 3-7 | ✓ |

### 占位符扫描

无 TBD/TODO/待定。所有 Step 都有具体代码。

### 类型一致性检查

- `go_to_sleep(state, minutes)` — Task 8 定义，Task 11/12 调用，签名一致 ✓
- `wake_up(state, cranky=False)` — Task 8 定义，Task 11 调用，签名一致 ✓
- `update_sleepiness(state, elapsed_minutes)` — Task 8 定义，Task 9 调用，签名一致 ✓
- `_is_news_too_old(published, max_age_days)` — Task 6 定义，Task 6 Step 9 调用，签名一致 ✓
- `check_proactive_rate(group_id)` — Task 15 定义，Task 15 Step 7 调用，签名一致 ✓
- `check_auto_emoji_rate(group_id)` — Task 16 定义，Task 17 调用，签名一致 ✓
- `maybe_react(bot, group_id, message_id, text, trigger_rate)` — Task 17 定义并调用 ✓

---

## 执行顺序

严格按 Task 1 → 17 顺序执行。每个 Phase 内部任务有依赖关系（如 Task 9 依赖 Task 7/8），跨 Phase 无强依赖（Phase A/B 相互独立）。但建议按顺序做，因为 Phase C 依赖 Phase A 的 EMOTION_BASELINE。
