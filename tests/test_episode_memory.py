"""双层事件记忆（episode_memory）测试 — 全部离线，mock LLM"""
import sys
import os
import unittest
import asyncio
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import db, episode_memory, memory


def _run(coro):
    return asyncio.run(coro)


def _json(obj):
    import json as _j
    return _j.dumps(obj, ensure_ascii=False)


class _TempDB:
    """临时数据库 + 内存缓冲隔离，测完还原"""

    def __init__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        db.init_db()

    def cleanup(self):
        db.DB_PATH = self._old_db_path


class TestEpisodeCrud(unittest.TestCase):
    """episodes / understandings 表 CRUD 往返"""

    def setUp(self):
        self.tmp = _TempDB()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_get_episode(self):
        eid = db.add_episode(
            group_id=123, date="9月6日", time="下午",
            participants="张三、李四", keywords=["火锅", "周末聚会"],
            importance=4, title="约好周末吃火锅",
            content="群里商量周末聚餐，最后定在周六晚上吃火锅。",
            message_count=12,
        )
        self.assertTrue(eid)
        eps = db.get_episodes(123)
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertEqual(ep["title"], "约好周末吃火锅")
        self.assertEqual(ep["keywords"], ["火锅", "周末聚会"])
        self.assertEqual(ep["importance"], 4)
        self.assertTrue(ep["last_recalled_at"])

    def test_importance_clamped(self):
        db.add_episode(group_id=1, importance=99, title="x", content="y")
        self.assertEqual(db.get_episodes(1)[0]["importance"], 5)
        db.add_episode(group_id=2, importance="abc", title="x2", content="y2")
        self.assertEqual(db.get_episodes(2)[0]["importance"], 3)

    def test_touch_episode_recall_updates(self):
        eid = db.add_episode(group_id=1, title="t", content="c")
        old = db.get_episodes(1)[0]["last_recalled_at"]
        time.sleep(1.1)
        db.touch_episode_recall(eid)
        new = db.get_episodes(1)[0]["last_recalled_at"]
        self.assertNotEqual(old, new)

    def test_understanding_lifecycle(self):
        eid = db.add_episode(group_id=7, title="t", content="c")
        uid = db.add_understanding(
            group_id=7, subject="张三的性格",
            keywords=["张三", "直率"], content="张三说话直但心软。",
            linked_episode=eid,
        )
        us = db.get_understandings(7)
        self.assertEqual(len(us), 1)
        self.assertEqual(us[0]["subject"], "张三的性格")
        self.assertEqual(us[0]["linked_episodes"], [eid])
        self.assertEqual(len(us[0]["history"]), 1)  # 初次形成快照

        # 印证：只传 subject → content 不变，history 不追加
        db.update_understanding(uid, subject="张三这个人")
        u = db.get_understandings(7)[0]
        self.assertEqual(u["subject"], "张三这个人")
        self.assertEqual(u["content"], "张三说话直但心软。")
        self.assertEqual(len(u["history"]), 1)

        # 修正：content 变化 → history 追加演化快照
        db.update_understanding(
            uid, content="张三说话直但心软，最近开始主动帮忙。",
            linked_episode=eid, history_title="主动帮忙",
        )
        u = db.get_understandings(7)[0]
        self.assertEqual(len(u["history"]), 2)
        self.assertEqual(u["history"][-1]["title"], "主动帮忙")
        self.assertEqual(u["linked_episodes"], [eid])  # 不重复挂链

        # 回想强化
        db.touch_understanding_recall(uid)
        self.assertEqual(db.get_understandings(7)[0]["recall_count"], 1)


class TestScoring(unittest.TestCase):
    """融合打分：词法重叠 × importance × 新近度"""

    def setUp(self):
        self.tmp = _TempDB()

    def tearDown(self):
        self.tmp.cleanup()

    def test_keyword_overlap_beats_recency(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        relevant_old = {
            "title": "周末吃火锅的约定", "content": "群里约定周六去吃火锅",
            "keywords": ["火锅", "周末", "聚会"], "participants": "张三",
            "importance": 3, "created_at": "2020-01-01 00:00:00",
        }
        irrelevant_new = {
            "title": "日常灌水", "content": "大家闲聊表情包",
            "keywords": ["灌水"], "participants": "李四",
            "importance": 1, "created_at": now,
        }
        q = episode_memory._tokenize("周末火锅去哪吃")
        self.assertGreater(
            episode_memory.score_episode(relevant_old, q),
            episode_memory.score_episode(irrelevant_new, q),
        )

    def test_high_importance_gets_floor_score(self):
        """重要事件即使词法不重叠也有保底分（阈值之上）"""
        old_big = {
            "title": "群规大讨论", "content": "因为吵架事件重定了群规",
            "keywords": ["群规", "吵架"], "participants": "全体",
            "importance": 5, "created_at": "2020-01-01 00:00:00",
        }
        q = episode_memory._tokenize("今天天气不错")
        self.assertGreaterEqual(
            episode_memory.score_episode(old_big, q),
            episode_memory.EPISODE_MIN_SCORE,
        )

    def test_understanding_subject_hit_ranks_first(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        on_topic = {
            "subject": "张三的游戏品味", "keywords": ["张三", "游戏"],
            "content": "张三偏爱独立游戏。", "recall_count": 0,
            "updated_at": now,
        }
        off_topic = {
            "subject": "李四的工作", "keywords": ["李四", "上班"],
            "content": "李四在厂里上班。", "recall_count": 0,
            "updated_at": now,
        }
        q = episode_memory._tokenize("张三喜欢玩什么游戏")
        self.assertGreater(
            episode_memory.score_understanding(on_topic, q),
            episode_memory.score_understanding(off_topic, q),
        )

    def test_retrieve_and_render_flow(self):
        db.add_episode(
            group_id=42, title="周末吃火锅的约定",
            content="群里约定周六去吃火锅", keywords=["火锅", "周末", "聚会"],
            participants="张三", importance=4,
        )
        got = episode_memory.retrieve_relevant_episodes(42, "周六火锅还去吗")
        self.assertTrue(got)
        block = episode_memory.render_episodes_block(got)
        self.assertIn("周末吃火锅的约定", block)
        # 无关群不受影响
        self.assertEqual(
            episode_memory.retrieve_relevant_episodes(43, "周六火锅还去吗"), [])


class TestConsolidate(unittest.TestCase):
    """整合流水线：mock LLM，验证闭合/缓冲切片/认知回写/退路"""

    def setUp(self):
        self.tmp = _TempDB()
        self._buf = memory._message_buffers.get(100, [])
        self._last = memory._last_summary_time.get(100, 0)
        memory._message_buffers[100] = []
        memory._last_summary_time[100] = 0
        self._old_settings = memory.load_model_settings
        memory.load_model_settings = lambda: {
            "api_key": "test-key", "model": "test-model"}
        self._patched = []

    def tearDown(self):
        memory._message_buffers[100] = self._buf
        memory._last_summary_time[100] = self._last
        memory.load_model_settings = self._old_settings
        for mod, name, val in self._patched:
            setattr(mod, name, val)
        self.tmp.cleanup()

    def _patch(self, module, name, value):
        self._patched.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    @staticmethod
    def _fill_buffer(n=18):
        for i in range(n):
            memory.buffer_message(
                100, 1000 + i, f"群友{i}",
                f"消息{i}：" + ("周末去吃火锅吧" if i < 12 else "换个话题聊天气"))

    def test_no_closure_keeps_buffer(self):
        """无收束话题 → 消息留在缓冲区，不写库"""
        self._fill_buffer()

        async def fake_episode(*a, **k):
            return {"closed_until": 0}
        self._patch(episode_memory, "_consolidate_episode", fake_episode)
        done = _run(episode_memory.maybe_consolidate(100))
        self.assertTrue(done)  # 已处理（非失败退回）
        self.assertEqual(len(memory._message_buffers[100]), 18)
        self.assertEqual(db.get_episodes(100), [])

    def test_buffer_overflow_falls_back(self):
        """满仓仍无闭合 → 返回 False 让上层退回旧摘要，防积压"""
        self._fill_buffer(memory.SUMMARY_MAX_MSGS)  # 50 条

        async def fake_episode(*a, **k):
            return {"closed_until": 0}
        self._patch(episode_memory, "_consolidate_episode", fake_episode)
        done = _run(episode_memory.maybe_consolidate(100))
        self.assertFalse(done)
        self.assertEqual(len(memory._message_buffers[100]),
                         memory.SUMMARY_MAX_MSGS)

    def test_closure_consolidates_and_slices_buffer(self):
        """闭合 → 写 episode + 轻量旧摘要，已闭合消息移出缓冲"""
        self._fill_buffer(18)

        async def fake_episode(gid, dialogue, window):
            return {
                "closed_until": 12, "date": "9月6日", "time": "下午",
                "participants": "群友0、群友1", "keywords": ["火锅", "周末"],
                "importance": 4, "title": "周末火锅之约",
                "content": "群里约好周末一起吃火锅。",
            }

        async def fake_patch(gid, eid, ep):
            return 1
        self._patch(episode_memory, "_consolidate_episode", fake_episode)
        self._patch(episode_memory, "_patch_understandings", fake_patch)

        done = _run(episode_memory.maybe_consolidate(100))

        self.assertTrue(done)
        eps = db.get_episodes(100)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["title"], "周末火锅之约")
        self.assertEqual(eps[0]["importance"], 4)
        # 12 条已整合，剩 6 条未闭合的留在缓冲
        self.assertEqual(len(memory._message_buffers[100]), 6)
        # 轻量旧摘要同步落库（日报可见）
        old_style = db.get_recent_memories(100, limit=5)
        self.assertEqual(len(old_style), 1)
        self.assertEqual(old_style[0]["topic"], "周末火锅之约")

    def test_llm_failure_returns_false(self):
        """LLM 挂 → 返回 False，上层退回旧摘要，消息不丢"""
        self._fill_buffer()

        async def fake_boom(*a, **k):
            raise RuntimeError("llm down")
        self._patch(episode_memory, "_consolidate_episode", fake_boom)
        done = _run(episode_memory.maybe_consolidate(100))
        self.assertFalse(done)
        self.assertEqual(len(memory._message_buffers[100]), 18)
        self.assertEqual(db.get_episodes(100), [])

    def test_patch_understanding_writes_add_and_update(self):
        """认知 patch：add 新认知 + update 已有认知（带演化历史）"""
        eid = db.add_episode(group_id=100, title="约火锅", content="约定吃火锅")
        old_uid = db.add_understanding(
            group_id=100, subject="张三的性格", content="张三很热心。")
        payload = '```json\n' + _json({
            "add": [{"subject": "张三的组织力",
                     "keywords": ["张三", "组织"],
                     "content": "张三爱张罗聚餐。"}],
            "update": {old_uid: {"subject": "张三的性格",
                                 "content": "张三热心，爱张罗事务。"}},
        }) + '\n```'

        class _R:
            def __init__(self, text):
                self.content = [type("T", (), {"text": text})()]

        async def fake_llm_create(*a, **k):
            return _R(payload)
        import bot.llm_call as llm_call_mod
        self._patch(llm_call_mod, "llm_create", fake_llm_create)

        applied = _run(episode_memory._patch_understandings(100, eid, {
            "title": "约火锅", "content": "约定吃火锅"}))

        self.assertEqual(applied, 2)
        us = {u["subject"]: u for u in db.get_understandings(100)}
        self.assertIn("张三的组织力", us)
        self.assertEqual(us["张三的性格"]["content"], "张三热心，爱张罗事务。")
        self.assertEqual(us["张三的性格"]["linked_episodes"], [eid])

    def test_extract_json_tolerates_wrappers(self):
        self.assertEqual(
            episode_memory._extract_json('前言 {"closed_until": 0} 后缀'),
            {"closed_until": 0})
        self.assertEqual(
            episode_memory._extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(ValueError):
            episode_memory._extract_json("完全没有JSON")


if __name__ == "__main__":
    unittest.main()
