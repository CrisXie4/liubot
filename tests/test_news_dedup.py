"""新闻分享包改造测试 — 去重判定 / 选题去模板 / 搜索重试 / 持久化

覆盖 2026-10-09/10 "同一条 Steam 新闻连发两天" 事故的三处修复：
  1. ai_brain 分享史去重（difflib 相似度 + TTL）
  2. 选题去模板（48h 内不重选 + 月份词 angle）
  3. web_search.search_recent 0 结果换措辞重试
"""
import sys
import os
import json
import time
import asyncio
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_brain
from bot import consciousness as cs
from bot import timeutil
from bot import web_search


def _fresh_state() -> dict:
    """独立的最小状态深拷贝（不碰全局单例，不读写真实 data/ 目录）"""
    return json.loads(json.dumps(cs.DEFAULT_STATE))


class TestShareDedup(unittest.TestCase):
    """二层去重：新 summary 与历史 text 相似度 > 0.55 拦截"""

    def setUp(self):
        self.state = _fresh_state()

    def test_similar_news_blocked(self):
        ai_brain._remember_shared_news(self.state, "游戏", "Steam秋促开启 720款新游参与打折")
        self.assertTrue(ai_brain._is_duplicate_news(
            "Steam 秋促开启了，720款新游参与打折 历史低价", self.state))

    def test_different_news_allowed(self):
        ai_brain._remember_shared_news(self.state, "AI", "OpenAI发布新模型 推理能力翻倍")
        self.assertFalse(ai_brain._is_duplicate_news(
            "任天堂公布新主机发售日期 首发游戏阵容曝光", self.state))

    def test_ttl_expired_allowed(self):
        ai_brain._remember_shared_news(self.state, "游戏", "Steam秋促开启 720款新游打折")
        # 13 天前 → 超过 12 天 TTL，过期放行
        self.state["news_shared"][0]["ts"] = time.time() - 13 * 24 * 3600
        self.assertFalse(ai_brain._is_duplicate_news(
            "Steam秋促开启 720款新游打折", self.state))

    def test_ttl_within_still_blocked(self):
        ai_brain._remember_shared_news(self.state, "游戏", "Steam秋促开启 720款新游打折")
        # 11 天前 → 还在 12 天 TTL 内，仍拦截
        self.state["news_shared"][0]["ts"] = time.time() - 11 * 24 * 3600
        self.assertTrue(ai_brain._is_duplicate_news(
            "Steam秋促开启 720款新游打折", self.state))

    def test_history_cap_30(self):
        for i in range(35):
            ai_brain._remember_shared_news(self.state, "测试", f"完全不同的事件编号{i}号")
        self.assertEqual(len(self.state["news_shared"]), ai_brain.NEWS_HISTORY_CAP)
        # 最旧的被挤掉
        self.assertFalse(any(h["text"].endswith("编号0号") for h in self.state["news_shared"]))

    def test_history_recorded_into_state(self):
        ai_brain._remember_shared_news(self.state, "游戏", "某事件发生了")
        self.assertEqual(len(self.state["news_shared"]), 1)
        rec = self.state["news_shared"][0]
        self.assertEqual(rec["topic"], "游戏")
        self.assertEqual(rec["text"], "某事件发生了")
        self.assertTrue(rec["ts"] > 0)

    def test_recent_shared_prompt_lists_history(self):
        for i in range(10):
            ai_brain._remember_shared_news(self.state, "游戏", f"事件{i}" + "细节" * 15)
        prompt = ai_brain._recent_shared_prompt(self.state)
        self.assertIn("重复豁免", prompt)
        # 只注入最近 8 条，更早的不出现
        self.assertNotIn("事件0", prompt)
        self.assertIn("事件9", prompt)

    def test_recent_shared_prompt_empty_without_history(self):
        self.assertEqual(ai_brain._recent_shared_prompt(self.state), "")


class TestTopicDeTemplating(unittest.TestCase):
    """选题去模板：48h 内用过的 topic 不再重选；angle 池月份词动态生成"""

    def setUp(self):
        self.state = _fresh_state()

    def test_recent_topic_excluded(self):
        pool = ["AI 最新进展", "本周科技新闻", "有趣的冷知识"]
        for _ in range(10):
            # 每轮用全新状态（_pick_topic 会记录刚选中的 topic，属于预期行为）
            state = _fresh_state()
            ai_brain._remember_topic_used(state, "AI 最新进展")
            ai_brain._remember_topic_used(state, "本周科技新闻")
            self.assertEqual(ai_brain._pick_topic(state, pool), "有趣的冷知识")

    def test_expired_topic_can_return(self):
        ai_brain._remember_topic_used(self.state, "有趣的冷知识")
        # 49 小时前 → 超过 48h TTL，可以重选
        self.state["recent_topics"][0]["ts"] = time.time() - 49 * 3600
        self.assertEqual(ai_brain._pick_topic(self.state, ["有趣的冷知识"]), "有趣的冷知识")

    def test_pick_records_topic(self):
        topic = ai_brain._pick_topic(self.state, ["编程技术"])
        self.assertIn(topic, [r["topic"] for r in self.state["recent_topics"]])

    def test_all_recent_falls_back_to_full_pool(self):
        for t in ["A话题", "B话题"]:
            ai_brain._remember_topic_used(self.state, t)
        # 全被排除时放回全池，保证总有得选
        self.assertIn(ai_brain._pick_topic(self.state, ["A话题", "B话题"]),
                      ["A话题", "B话题"])

    def test_angles_use_dynamic_months(self):
        angles = ai_brain._news_angles()
        now = timeutil.now_shanghai()
        self.assertIn(f"{now.month}月", angles)
        self.assertIn(f"{now.month % 12 + 1}月", angles)
        # 写死的"最近/2026"已移除
        self.assertNotIn("最近", angles)
        self.assertNotIn("2026", angles)

    def test_recent_topics_capped(self):
        for i in range(20):
            ai_brain._remember_topic_used(self.state, f"话题{i}号")
        self.assertEqual(len(self.state["recent_topics"]), ai_brain.RECENT_TOPICS_CAP)


class TestSearchRecentRetry(unittest.TestCase):
    """search_recent 0 结果时换措辞重试一次（mock 底层 search）"""

    def test_retry_with_bare_topic_on_zero_results(self):
        with patch("bot.web_search.search",
                   new=AsyncMock(side_effect=[[], [{"title": "x"}]])) as m:
            results = asyncio.run(
                web_search.search_recent("本周科技新闻 最新", max_age_days=7))
        self.assertEqual(len(results), 1)
        self.assertEqual(m.await_count, 2)
        self.assertEqual(m.await_args_list[0].args[0], "本周科技新闻 最新")
        # 尾部"最新"被剥掉，用 topic 裸词重试
        self.assertEqual(m.await_args_list[1].args[0], "本周科技新闻")

    def test_no_qualifier_appends_month(self):
        with patch("bot.web_search.search",
                   new=AsyncMock(side_effect=[[], [{"title": "x"}]])) as m:
            results = asyncio.run(web_search.search_recent("编程技术"))
        self.assertEqual(len(results), 1)
        now = timeutil.now_shanghai()
        # 没有限定词可剥 → 补当月月份词换措辞
        self.assertEqual(m.await_args_list[1].args[0], f"编程技术 {now.month}月")

    def test_no_retry_when_first_try_has_results(self):
        with patch("bot.web_search.search",
                   new=AsyncMock(return_value=[{"title": "x"}])) as m:
            results = asyncio.run(web_search.search_recent("AI 最新"))
        self.assertEqual(len(results), 1)
        self.assertEqual(m.await_count, 1)

    def test_both_zero_returns_empty(self):
        with patch("bot.web_search.search",
                   new=AsyncMock(side_effect=[[], []])) as m:
            results = asyncio.run(web_search.search_recent("AI 最新"))
        self.assertEqual(results, [])
        self.assertEqual(m.await_count, 2)

    def test_pure_qualifier_no_retry(self):
        # 整个查询都是限定词，剥完为空 → 不重试直接返回空
        with patch("bot.web_search.search",
                   new=AsyncMock(side_effect=[[]])) as m:
            results = asyncio.run(web_search.search_recent("最新"))
        self.assertEqual(results, [])
        self.assertEqual(m.await_count, 1)


class TestPersistence(unittest.TestCase):
    """分享史随意识状态持久化，重启后仍能去重"""

    def test_state_roundtrip_keeps_history(self):
        state = _fresh_state()
        ai_brain._remember_shared_news(state, "游戏", "Steam秋促开启 720款新游打折")
        # 模拟重启：state 序列化落盘再读回
        state2 = json.loads(json.dumps(state))
        self.assertTrue(ai_brain._is_duplicate_news(
            "Steam秋促开启 720款新游打折", state2))

    def test_save_and_load_state_file(self):
        state = _fresh_state()
        ai_brain._remember_shared_news(state, "游戏", "Steam秋促开启 720款新游打折")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "consciousness.json")
            with patch.object(cs, "STATE_PATH", path):
                cs.save_state(state)
                reloaded = cs.load_state()
        self.assertEqual(len(reloaded["news_shared"]), 1)
        self.assertEqual(reloaded["news_shared"][0]["text"], "Steam秋促开启 720款新游打折")


if __name__ == "__main__":
    unittest.main()
