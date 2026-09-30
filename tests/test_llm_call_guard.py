"""思维链泄漏防御单测 — 2026-09-30 群里发疯事故回归。

场景：中转站把推理模型的思考过程当正文返回（无 type:"thinking" block），
2452 字英文推理直接发进群。防御两层：
  1) _strip_inline_thinking 剥 <think> 标签块，剥后为空 → 空响应 failover
  2) _looks_like_leaked_reasoning 识别无标签裸思考 → failover 换渠道
"""
from types import SimpleNamespace

from bot.llm_call import (
    _is_empty_response,
    _looks_like_leaked_reasoning,
    _response_text,
    _strip_inline_thinking,
)


def _resp(*texts):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=t) for t in texts])


# 2026-09-30 实际发进群的泄漏文本（截取）
REAL_LEAK = (
    "Let me look at the current situation. There's a message from 违规昵称 "
    "(QQ=2208956889) who sent a \"你好\" cat image. This seems to be a greeting. "
    "The name is \"违规昵称\" which is a bit odd. Wait, looking at the context - "
    "earlier someone (Cris谢) said to me \"@违规 广告\" meaning he flagged something "
    "as violation/advertisment. Actually the message was \"违规 广告\" - Cris谢 was "
    "marking a message as 违规广告, and told me to handle it. Hmm, but that was my "
    "previous unexecuted reply? Let me look - the last message from me was "
    "\"得咧 六哥 撤了 禁他30分钟...\" but I never actually called punish_violation "
    "or ban_member. Actually wait - let me re-read. I need to check what the user wants."
)


def test_real_accident_text_is_caught():
    assert _looks_like_leaked_reasoning(REAL_LEAK)


def test_normal_chinese_reply_not_caught():
    assert not _looks_like_leaked_reasoning(
        "哟 大周六晚上还打卡 这鸭子也不休息 群里推销成品号的 见一个处理一个" * 5
    )


def test_short_english_reply_not_caught():
    assert not _looks_like_leaked_reasoning("Okay, I will check it later. Wait for me." * 3)


def test_chinese_reasoning_not_flagged():
    # 中文思考腔不做启发式（误伤率高），交给人设约束兜底
    assert not _looks_like_leaked_reasoning("嗯 让我看看这个问题的来龙去脉 " * 30)


def test_think_tag_pair_stripped():
    r = _resp("<think>先分析一下用户意图</think>得咧 六哥 马上办")
    assert _strip_inline_thinking(r)
    assert _response_text(r) == "得咧 六哥 马上办"


def test_unclosed_think_tag_stripped_to_end():
    r = _resp("正文开头<think>后面全是思考 没有闭合标签")
    assert _strip_inline_thinking(r)
    assert _response_text(r) == "正文开头"


def test_all_thinking_becomes_empty_response():
    r = _resp("<think>整条都是思考</think>")
    _strip_inline_thinking(r)
    assert _is_empty_response(r)


def test_normal_text_untouched():
    r = _resp("正常回复，聊聊 <code> 标签也没事")
    assert not _strip_inline_thinking(r)
    assert _response_text(r) == "正常回复，聊聊 <code> 标签也没事"


def test_multi_block_text_joined():
    r = _resp("第一段", "第二段")
    assert _response_text(r) == "第一段\n第二段"
