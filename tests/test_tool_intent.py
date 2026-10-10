"""口头承诺检测正则测试（chat.py 的 _INTENT_TO_ACT_RE）。

背景：9B 模型在大系统提示 + 54 工具下，会说"我查一下/看看用量排行"
却不发 tool_use——这句叙述被当成最终回复，群里表现为"我查一下…
然后没有下文"。正则用于识别这类叙述并触发 tool_choice=any 强制重试，
必须精准：漏判 = 没下文复发，误判 = 普通闲聊被强塞工具调用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from bot.chat import _INTENT_TO_ACT_RE as R

# 真实案例：线上复现时模型的实际输出
SHOULD_MATCH = [
    "好的，先看看中转站数据库的结构，再查今天谁用量最大。",
    "查一下今天的用量排行，看看谁用量最多。",
    "我查一下中转站的 logs 给你看看",
    "我帮你查查",
    "稍等我搜一下",
    "我去看看记录",
    "我查查是谁",
    "我搜一下今天的新闻",
]

# 普通闲聊/正常回答，不能误伤
SHOULD_NOT_MATCH = [
    "今天天气不错",
    "我觉得是用户A用得最多",
    "我看看你说的这个问题",      # 看看后无数据关键词
    "这题我不会查字典",           # 我和查之间隔着"不会"
    "哈哈哈哈查无此人",
    "我用得最多的是微信",
    "昨天的球赛太精彩了，最后那球绝了",
]


def test_intent_regex_matches():
    for s in SHOULD_MATCH:
        assert R.search(s), f"应匹配却没匹配: {s}"


def test_intent_regex_no_false_positive():
    for s in SHOULD_NOT_MATCH:
        assert not R.search(s), f"不应匹配却匹配了: {s}"
