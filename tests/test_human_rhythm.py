"""humanizer 节奏自然化测试：间隔方差 / 尾泡 / 深夜变慢 / CQ 码不撕。

锁定这些不变量：
  1. [CQ:reply] 引用码永远在第一条气泡开头，且全对话只出现一次
  2. CQ 码（表情/图片）绝不会被句子切分撕断
  3. 尾泡：多条气泡时 15% 概率补一条极短的；单气泡和 priority 绝不补
  4. 气泡间隔是混合分布（有走神档），BUBBLE_GAP 归零可整体静音
  5. 深夜（23~8 点）整体间隔变慢
"""
import asyncio
import random
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import humanizer


def _gid() -> int:
    """运行时随机群号：避开群发送冷却的跨用例污染。"""
    return 800_000_000 + int(uuid.uuid4().int % 100_000_000)


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.bot_qq = "10000"
    bot.send_group_msg = AsyncMock(return_value=999)
    bot.send_group_forward_msg = AsyncMock(return_value=888)
    bot.delete_msg = AsyncMock(return_value=True)
    return bot


@pytest.fixture(autouse=True)
def _clean():
    humanizer._last_send_time.clear()
    humanizer.last_send_failure = None
    yield
    humanizer._last_send_time.clear()
    humanizer.last_send_failure = None


def _send(bot, gid, text, **kw):
    """打平随机性（typo/犹豫/尾泡）与真实延迟，专注气泡结构本身。"""
    with patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
         patch("bot.humanizer.random.random", return_value=0.99):
        return asyncio.run(humanizer.send_human_like(bot, gid, text, **kw))


# ─── 引用码与 CQ 码不变量 ───

def test_reply_code_first_bubble_only():
    """多气泡回复：引用码只在第一条气泡开头出现一次。"""
    bot = _make_bot()
    gid = _gid()
    text = ("[CQ:reply,id=77]第一条消息里带个表情[CQ:face,id=182]\n"
            "第二条消息说点别的\n第三条收个尾")
    sent = _send(bot, gid, text)

    assert len(sent) == 3
    assert sent[0].startswith("[CQ:reply,id=77]")
    code_bubbles = [b for b in sent if "[CQ:reply,id=77]" in b]
    assert len(code_bubbles) == 1 and code_bubbles[0] is sent[0]
    # 表情码完整地待在某一条气泡里，没有被撕开
    face_bubbles = [b for b in sent if "[CQ:face,id=182]" in b]
    assert len(face_bubbles) == 1


def test_reply_code_short_single_bubble():
    """短回复：引用码原样贴在唯一一条气泡开头。"""
    bot = _make_bot()
    sent = _send(bot, _gid(), "[CQ:reply,id=9]收到喵")
    assert sent == ["[CQ:reply,id=9]收到喵"]


def test_cq_codes_never_torn_by_split():
    """句号正好落在 CQ 码前后：切句后每个码仍完整独立。"""
    text = ("看这个[CQ:face,id=14]。真的绝了\n"
            "还有图[CQ:image,file=abc.jpg.jpg]。自己看\n"
            "就到这")
    bubbles = humanizer.split_bubbles(text)
    joined = "".join(bubbles)
    assert joined.count("[CQ:face,id=14]") == 1
    assert joined.count("[CQ:image,file=abc.jpg.jpg]") == 1
    for b in bubbles:
        # 括号必须成对出现（半截码会只有一半）
        assert b.count("[") == b.count("]")


def test_extract_reply_code_takes_first():
    """多个引用码时只取第一个，其余从正文剥掉（不往气泡里漏）。"""
    rest, code = humanizer._extract_reply_code(
        "[CQ:reply,id=1]正文[CQ:reply,id=2]继续")
    assert code == "[CQ:reply,id=1]"
    assert "[CQ:reply" not in rest


# ─── 尾泡 ───

def _two_bubble_text() -> str:
    """恰好拆成 2 条气泡的文本：两行各 20 字（总 40 ≥ 35，不会触发短合并）。"""
    return "甲" * 20 + "\n" + "乙" * 20


def test_tail_bubble_appended_for_multi_bubble():
    """多条气泡 + 命中概率 → 末尾补一条池子里的极短尾泡。"""
    bot = _make_bot()
    gid = _gid()
    with patch.object(humanizer, "TAIL_BUBBLE_CHANCE", 1.0), \
         patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
         patch("bot.humanizer.random.random", return_value=0.5):
        sent = asyncio.run(humanizer.send_human_like(
            bot, gid, _two_bubble_text()))

    assert len(sent) == 3
    assert sent[-1] in humanizer.TAIL_BUBBLES
    assert sent[0] == "甲" * 20  # 正文气泡不受影响


def test_tail_bubble_skipped_for_single_bubble():
    """单气泡回复绝不补尾泡（单发一条后面跟"哈哈"像毛病不像真人）。"""
    bot = _make_bot()
    with patch.object(humanizer, "TAIL_BUBBLE_CHANCE", 1.0), \
         patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
         patch("bot.humanizer.random.random", return_value=0.5):
        sent = asyncio.run(humanizer.send_human_like(bot, _gid(), "就一句话"))

    assert len(sent) == 1


def test_tail_bubble_skipped_for_priority():
    """系统关键消息（验证题/判题反馈）绝不补尾泡。"""
    bot = _make_bot()
    text = "第一句话说完了啊\n第二句话也说完了\n第三句就收个尾"
    with patch.object(humanizer, "TAIL_BUBBLE_CHANCE", 1.0), \
         patch("bot.humanizer.asyncio.sleep", new=AsyncMock()), \
         patch("bot.humanizer.random.random", return_value=0.5):
        sent = asyncio.run(humanizer.send_human_like(
            bot, _gid(), text, priority=True))

    assert len(sent) == 3
    assert all(b not in humanizer.TAIL_BUBBLES for b in sent)


def test_tail_bubble_not_sent_when_probability_missed():
    """概率未命中（默认 15%，random=0.99）→ 无尾泡。"""
    bot = _make_bot()
    sent = _send(bot, _gid(), _two_bubble_text())
    assert len(sent) == 2


# ─── 间隔随机化 ───

def test_bubble_gap_within_bounds_and_three_bands():
    """间隔必须落在三档范围内，且大部分样本在"顺手连发"档。"""
    random.seed(1234)
    hi = humanizer.BUBBLE_GAP[1]
    samples = [humanizer._bubble_gap() for _ in range(2000)]
    assert all(0.0 <= g <= hi * 6.0 + 1e-9 for g in samples)
    quick = sum(1 for g in samples if g <= hi) / len(samples)
    assert 0.5 < quick < 0.9, "顺手连发档占比异常，间隔分布退化"


def test_bubble_gap_respects_zero_patch():
    """BUBBLE_GAP 归零（测试静音约定）→ 三档全部归零。"""
    with patch.object(humanizer, "BUBBLE_GAP", (0.0, 0.0)):
        assert all(humanizer._bubble_gap() == 0.0 for _ in range(200))


def test_night_factor_boundaries():
    """深夜 23~8 点放慢，白天原速。"""
    f = humanizer._night_factor
    assert f(hour=23) == humanizer.NIGHT_SLOWDOWN
    assert f(hour=0) == humanizer.NIGHT_SLOWDOWN
    assert f(hour=7) == humanizer.NIGHT_SLOWDOWN
    assert f(hour=8) == 1.0
    assert f(hour=12) == 1.0
    assert f(hour=22) == 1.0


def test_night_slows_all_delays():
    """同一文本：深夜倍率下每条气泡的等待都严格更长。"""
    text = _two_bubble_text()

    def _delays(night):
        bot = _make_bot()
        sleeps = []

        async def _rec(t):
            sleeps.append(t)

        with patch.object(humanizer, "_night_factor", lambda hour=None: night), \
             patch("bot.humanizer.asyncio.sleep", new=_rec), \
             patch("bot.humanizer.random.random", return_value=0.99), \
             patch("bot.humanizer.random.uniform", lambda a, b: (a + b) / 2):
            asyncio.run(humanizer.send_human_like(bot, _gid(), text))
        return sleeps

    day = _delays(1.0)
    night = _delays(humanizer.NIGHT_SLOWDOWN)
    assert len(day) == len(night) == 2
    assert sum(night) > sum(day) > 0
    assert all(n > d for n, d in zip(night, day))
