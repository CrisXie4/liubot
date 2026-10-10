"""
数据库模块 — SQLite 存储
- 违规记录
- 黑名单
- 自动审核日志
- 系统配置
"""
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

from . import timeutil

logger = logging.getLogger("qqbot")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.db")


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()

    # ─── 兼容老库：必须在 executescript 之前补齐缺失列 ───
    # executescript 里有 CREATE INDEX 引用这些列，老库里没列就会报错；
    # ALTER 在 executescript 之后再补就来不及了
    try:
        conn.execute("ALTER TABLE conversation_memories ADD COLUMN importance REAL DEFAULT 0.5")
    except Exception:
        pass

    conn.executescript("""
        -- 违规记录
        CREATE TABLE IF NOT EXISTS violations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      INTEGER NOT NULL,
            group_id        INTEGER NOT NULL,
            group_name      TEXT    DEFAULT '',
            violator_qq     INTEGER NOT NULL,
            violator_name   TEXT    DEFAULT '',
            raw_message     TEXT    NOT NULL,
            violation_note  TEXT    DEFAULT '',
            keywords        TEXT    DEFAULT '[]',
            confidence      REAL    DEFAULT 1.0,
            source          TEXT    DEFAULT 'manual',
            recorded_at     TEXT    NOT NULL,
            recorded_by     INTEGER NOT NULL,
            status          TEXT    DEFAULT 'recorded'
        );

        -- 黑名单
        CREATE TABLE IF NOT EXISTS blacklist (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_qq         INTEGER NOT NULL,
            user_name       TEXT    DEFAULT '',
            group_id        INTEGER DEFAULT 0,
            reason          TEXT    DEFAULT '',
            action          TEXT    DEFAULT 'ban',
            blacklisted_at  TEXT    NOT NULL,
            blacklisted_by  INTEGER NOT NULL
        );

        -- 自动审核日志
        CREATE TABLE IF NOT EXISTS automod_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      INTEGER NOT NULL,
            group_id        INTEGER NOT NULL,
            user_qq         INTEGER NOT NULL,
            raw_message     TEXT    NOT NULL,
            confidence      REAL    NOT NULL,
            keywords_matched TEXT   DEFAULT '[]',
            action          TEXT    DEFAULT 'none',
            action_taken_at TEXT    NOT NULL
        );

        -- 正常消息样本（用于 ML 训练）
        CREATE TABLE IF NOT EXISTS message_samples (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        INTEGER NOT NULL,
            user_qq         INTEGER NOT NULL,
            raw_message     TEXT    NOT NULL,
            label           TEXT    DEFAULT 'normal',
            collected_at    TEXT    NOT NULL
        );

        -- 用户画像（AI 记忆）
        CREATE TABLE IF NOT EXISTS user_profiles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_qq         INTEGER NOT NULL,
            user_name       TEXT    DEFAULT '',
            group_id        INTEGER NOT NULL,
            nickname        TEXT    DEFAULT '',
            interests       TEXT    DEFAULT '',
            notes           TEXT    DEFAULT '',
            message_count   INTEGER DEFAULT 0,
            first_seen      TEXT    NOT NULL,
            last_seen       TEXT    NOT NULL,
            UNIQUE(user_qq, group_id)
        );

        -- 对话记忆（长期摘要）
        CREATE TABLE IF NOT EXISTS conversation_memories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        INTEGER NOT NULL,
            topic           TEXT    DEFAULT '',
            summary         TEXT    NOT NULL,
            participants    TEXT    DEFAULT '[]',
            key_facts       TEXT    DEFAULT '[]',
            message_count   INTEGER DEFAULT 0,
            importance      REAL    DEFAULT 0.5,  -- 0.0-1.0，分级压缩用
            created_at      TEXT    NOT NULL
        );

        -- 知识碎片（从对话中提取的事实）
        CREATE TABLE IF NOT EXISTS knowledge_facts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fact            TEXT    NOT NULL,
            category        TEXT    DEFAULT 'general',
            source_group    INTEGER DEFAULT 0,
            source_user     INTEGER DEFAULT 0,
            source_user_name TEXT   DEFAULT '',
            confidence      REAL    DEFAULT 0.5,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        -- 手动训练数据（网页喂给AI的训练样本）
        CREATE TABLE IF NOT EXISTS training_data (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_text    TEXT    NOT NULL,
            label           TEXT    NOT NULL DEFAULT 'violation',
            category        TEXT    DEFAULT '',
            keywords        TEXT    DEFAULT '',
            notes           TEXT    DEFAULT '',
            applied         INTEGER DEFAULT 0,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        );

        -- AI 破坏性动作审计（recall/ban/kick/mute/essence/at_all 等）
        CREATE TABLE IF NOT EXISTS ai_audit (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,                       -- %Y-%m-%d %H:%M:%S（系统时间，仅审计用）
            group_id          INTEGER NOT NULL,
            action            TEXT    NOT NULL,                      -- recall/ban/kick/mute/whole_ban/essence/at_all/...
            target_qq         INTEGER,                               -- 目标 QQ（无目标时 NULL，如 whole_ban）
            reason            TEXT,
            judge_verdict     TEXT,                                  -- allow/deny/light/exempt/master_instructed
            master_instructed INTEGER DEFAULT 0,                     -- 0/1
            undone            INTEGER DEFAULT 0                      -- 0/1 是否已撤销
        );

        -- 每日用户活跃快照（日报用；按上海日期，每用户每天一行）
        CREATE TABLE IF NOT EXISTS daily_user_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        INTEGER NOT NULL,
            user_qq         INTEGER NOT NULL,
            user_name       TEXT    DEFAULT '',
            stat_date       TEXT    NOT NULL,   -- 上海日期 YYYY-MM-DD
            msg_count       INTEGER DEFAULT 0,
            UNIQUE(group_id, user_qq, stat_date)
        );

        -- 每小时用量聚合（Web 面板图表用）：LLM 调用数、tokens 三类、群消息数
        CREATE TABLE IF NOT EXISTS hourly_usage (
            hour            TEXT PRIMARY KEY,   -- 上海时区 'YYYY-MM-DD HH:00'
            llm_calls       INTEGER DEFAULT 0,
            tokens_in       INTEGER DEFAULT 0,  -- 未命中缓存的输入
            tokens_cached   INTEGER DEFAULT 0,  -- 缓存命中的输入
            tokens_out      INTEGER DEFAULT 0,
            msgs_in         INTEGER DEFAULT 0
        );

        -- 结构化事件记忆（借鉴 AgentGal EpisodeMemory）：
        -- 一条 = 一个已收束的话题事件，keywords 专门为"以后什么输入能命中它"而写
        CREATE TABLE IF NOT EXISTS episodes (
            id              TEXT    PRIMARY KEY,          -- 稳定 UUID
            group_id        INTEGER NOT NULL,
            date            TEXT    DEFAULT '',           -- X月X日（人读）
            time            TEXT    DEFAULT '',           -- 时刻/时间段
            participants    TEXT    DEFAULT '',           -- 关键在场人物（人读串）
            keywords        TEXT    DEFAULT '[]',         -- JSON 数组
            importance      INTEGER DEFAULT 3,            -- 1-5
            title           TEXT    DEFAULT '',
            content         TEXT    DEFAULT '',
            message_count   INTEGER DEFAULT 0,
            created_at      TEXT    NOT NULL,
            last_recalled_at TEXT   DEFAULT ''            -- 最近被回忆时间（回想强化）
        );

        -- 稳定认知（借鉴 AgentGal Understanding）：
        -- 对群友/群内关系的持久判断，随事件演化并保留历史
        CREATE TABLE IF NOT EXISTS understandings (
            id              TEXT    PRIMARY KEY,          -- 稳定 UUID
            group_id        INTEGER NOT NULL,
            subject         TEXT    DEFAULT '',           -- 认知主题（人/关系/相处模式）
            keywords        TEXT    DEFAULT '[]',         -- JSON 数组
            content         TEXT    DEFAULT '',           -- 结论（非事件描述）
            linked_episodes TEXT    DEFAULT '[]',         -- JSON 数组，episode id
            history         TEXT    DEFAULT '[]',         -- JSON 数组，演化快照
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            recall_count    INTEGER DEFAULT 0,
            last_recalled_at TEXT   DEFAULT ''
        );

        -- 索引
        CREATE INDEX IF NOT EXISTS idx_violations_group     ON violations(group_id);
        CREATE INDEX IF NOT EXISTS idx_violations_violator  ON violations(violator_qq);
        CREATE INDEX IF NOT EXISTS idx_violations_time      ON violations(recorded_at);
        CREATE INDEX IF NOT EXISTS idx_blacklist_user       ON blacklist(user_qq);
        CREATE INDEX IF NOT EXISTS idx_automod_group        ON automod_log(group_id);
        CREATE INDEX IF NOT EXISTS idx_samples_label        ON message_samples(label);
        CREATE INDEX IF NOT EXISTS idx_user_profiles_qq     ON user_profiles(user_qq);
        CREATE INDEX IF NOT EXISTS idx_user_profiles_group  ON user_profiles(group_id);
        CREATE INDEX IF NOT EXISTS idx_conv_memories_group  ON conversation_memories(group_id);
        CREATE INDEX IF NOT EXISTS idx_conv_memories_imp    ON conversation_memories(importance);
        CREATE INDEX IF NOT EXISTS idx_knowledge_category   ON knowledge_facts(category);
        CREATE INDEX IF NOT EXISTS idx_training_label       ON training_data(label);
        CREATE INDEX IF NOT EXISTS idx_ai_audit_target      ON ai_audit(target_qq);
        CREATE INDEX IF NOT EXISTS idx_ai_audit_ts          ON ai_audit(ts);
        CREATE INDEX IF NOT EXISTS idx_daily_stats_group_date ON daily_user_stats(group_id, stat_date);
        CREATE INDEX IF NOT EXISTS idx_episodes_group       ON episodes(group_id);
        CREATE INDEX IF NOT EXISTS idx_understandings_group ON understandings(group_id);
    """)
    conn.commit()
    conn.close()


# ===================== Violations =====================

def add_violation(
    message_id: int, group_id: int, group_name: str,
    violator_qq: int, violator_name: str, raw_message: str,
    violation_note: str, keywords: list[str], recorded_by: int,
    confidence: float = 1.0, source: str = "manual",
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO violations (message_id, group_id, group_name, violator_qq, violator_name,
           raw_message, violation_note, keywords, confidence, source, recorded_at, recorded_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (message_id, group_id, group_name, violator_qq, violator_name,
         raw_message, violation_note, json.dumps(keywords, ensure_ascii=False),
         confidence, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), recorded_by),
    )
    conn.commit()
    vid = cur.lastrowid
    conn.close()
    # 使相关缓存失效
    from . import cache
    cache.keyword_weights_cache.invalidate("keyword_weights")
    cache.violation_count_cache.invalidate("count")
    return vid


def get_violation(violation_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM violations WHERE id=?", (violation_id,)).fetchone()
    conn.close()
    if row is None: return None
    d = dict(row); d["keywords"] = json.loads(d["keywords"]); return d


def query_violations(group_id=None, violator_qq=None, limit=50, offset=0, source=None) -> list[dict]:
    conn = get_connection()
    where, params = [], []
    if group_id is not None: where.append("group_id=?"); params.append(group_id)
    if violator_qq is not None: where.append("violator_qq=?"); params.append(violator_qq)
    if source is not None: where.append("source=?"); params.append(source)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM violations {clause} ORDER BY recorded_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_violation_stats(group_id=None) -> dict:
    conn = get_connection()
    w = "WHERE group_id=?" if group_id else ""
    p = (group_id,) if group_id else ()

    total = conn.execute(f"SELECT COUNT(*) FROM violations {w}", p).fetchone()[0]
    auto_count = conn.execute(f"SELECT COUNT(*) FROM violations {w} AND source='auto'".replace("AND AND", "AND"), p).fetchone()[0] if w else \
                 conn.execute("SELECT COUNT(*) FROM violations WHERE source='auto'").fetchone()[0]

    top_users = [dict(r) for r in conn.execute(
        f"SELECT violator_qq, violator_name, COUNT(*) as cnt FROM violations {w} GROUP BY violator_qq ORDER BY cnt DESC LIMIT 10", p).fetchall()]
    top_groups = [dict(r) for r in conn.execute(
        "SELECT group_id, group_name, COUNT(*) as cnt FROM violations GROUP BY group_id ORDER BY cnt DESC LIMIT 10").fetchall()]
    hour_dist = [dict(r) for r in conn.execute(
        f"SELECT CAST(substr(recorded_at,12,2) AS INTEGER) as hour, COUNT(*) as cnt FROM violations {w} GROUP BY hour ORDER BY hour", p).fetchall()]
    source_dist = [dict(r) for r in conn.execute(
        f"SELECT source, COUNT(*) as cnt FROM violations {w} GROUP BY source".replace("WHERE  GROUP", "GROUP"), p).fetchall()] if not w else \
                   [dict(r) for r in conn.execute(
        f"SELECT source, COUNT(*) as cnt FROM violations {w} GROUP BY source", p).fetchall()]

    recent = [dict(r) for r in conn.execute(
        f"SELECT * FROM violations {w} ORDER BY recorded_at DESC LIMIT 10", p).fetchall()]
    for r in recent: r["keywords"] = json.loads(r["keywords"])

    conn.close()
    return {"total": total, "auto_count": auto_count, "top_violators": top_users,
            "top_groups": top_groups, "hour_distribution": hour_dist,
            "source_distribution": source_dist, "recent": recent}


def get_common_keywords(group_id=None, top_n=30) -> list[dict]:
    conn = get_connection()
    w = "WHERE group_id=?" if group_id else ""
    p = (group_id,) if group_id else ()
    rows = conn.execute(f"SELECT keywords FROM violations {w}", p).fetchall()
    conn.close()
    freq: dict[str, int] = {}
    for r in rows:
        for kw in json.loads(r["keywords"]):
            freq[kw] = freq.get(kw, 0) + 1
    return [{"keyword": k, "count": c} for k, c in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]]


def get_violation_count() -> int:
    """查询违规总条数（优先使用缓存）"""
    from . import cache
    cached = cache.violation_count_cache.get("count")
    if cached is not None:
        return cached

    conn = get_connection()
    c = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
    conn.close()
    cache.violation_count_cache.set("count", c)
    return c


# ===================== Blacklist =====================

def blacklist_add(user_qq: int, user_name: str = "", group_id: int = 0,
                  reason: str = "", action: str = "ban", by_qq: int = 0) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO blacklist (user_qq, user_name, group_id, reason, action, blacklisted_at, blacklisted_by) VALUES (?,?,?,?,?,?,?)",
        (user_qq, user_name, group_id, reason, action, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), by_qq))
    conn.commit()
    bid = cur.lastrowid; conn.close(); return bid


def blacklist_remove(user_qq: int, group_id: int = 0) -> bool:
    conn = get_connection()
    if group_id:
        conn.execute("DELETE FROM blacklist WHERE user_qq=? AND (group_id=? OR group_id=0)", (user_qq, group_id))
    else:
        conn.execute("DELETE FROM blacklist WHERE user_qq=?", (user_qq,))
    ok = conn.total_changes > 0; conn.commit(); conn.close(); return ok


def blacklist_check(user_qq: int, group_id: int = 0) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM blacklist WHERE user_qq=? AND (group_id=? OR group_id=0) LIMIT 1",
        (user_qq, group_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def blacklist_list(group_id: int = 0) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM blacklist WHERE group_id=? OR group_id=0 OR ?=0 ORDER BY blacklisted_at DESC",
        (group_id, group_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===================== AutoMod Log =====================

def automod_log_add(message_id: int, group_id: int, user_qq: int, raw_message: str,
                    confidence: float, keywords_matched: list[str], action: str) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO automod_log (message_id, group_id, user_qq, raw_message, confidence, keywords_matched, action, action_taken_at) VALUES (?,?,?,?,?,?,?,?)",
        (message_id, group_id, user_qq, raw_message, confidence,
         json.dumps(keywords_matched, ensure_ascii=False), action,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); lid = cur.lastrowid; conn.close(); return lid


def automod_log_query(limit=50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM automod_log ORDER BY action_taken_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def automod_stats() -> dict:
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM automod_log").fetchone()[0]
    recalled = conn.execute("SELECT COUNT(*) FROM automod_log WHERE action='recalled'").fetchone()[0]
    warned = conn.execute("SELECT COUNT(*) FROM automod_log WHERE action='warned'").fetchone()[0]
    conn.close()
    return {"total_scanned": total, "auto_recalled": recalled, "auto_warned": warned}


# ===================== Message Samples (ML) =====================

def sample_add(group_id: int, user_qq: int, raw_message: str, label: str = "normal") -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO message_samples (group_id, user_qq, raw_message, label, collected_at) VALUES (?,?,?,?,?)",
        (group_id, user_qq, raw_message, label, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit(); sid = cur.lastrowid; conn.close(); return sid


def sample_get_all() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM message_samples").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sample_count_by_label() -> dict:
    conn = get_connection()
    rows = conn.execute("SELECT label, COUNT(*) as cnt FROM message_samples GROUP BY label").fetchall()
    conn.close()
    return {r["label"]: r["cnt"] for r in rows}


# ===================== Helpers =====================

def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("keywords", "keywords_matched"):
        if key in d and isinstance(d[key], str):
            try: d[key] = json.loads(d[key])
            except: pass
    return d


# ===================== User Profiles (Memory) =====================

def upsert_user_profile(user_qq: int, group_id: int, user_name: str = "",
                        nickname: str = "", interests: str = "", notes: str = "") -> int:
    """创建或更新用户画像，返回 id"""
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = conn.execute(
        "SELECT * FROM user_profiles WHERE user_qq=? AND group_id=?",
        (user_qq, group_id)
    ).fetchone()
    if existing:
        new_interests = interests if interests else (existing["interests"] or "")
        new_notes = ((existing["notes"] or "") + "\n" + notes).strip() if notes else (existing["notes"] or "")
        conn.execute(
            """UPDATE user_profiles SET user_name=?, nickname=?, interests=?,
               notes=?, message_count=message_count+1, last_seen=?
               WHERE user_qq=? AND group_id=?""",
            (user_name or existing["user_name"] or "", nickname or existing["nickname"] or "",
             new_interests, new_notes, now, user_qq, group_id))
        conn.commit()
        pid = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO user_profiles (user_qq, user_name, group_id, nickname, interests, notes,
               message_count, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,1,?,?)""",
            (user_qq, user_name, group_id, nickname, interests, notes, now, now))
        conn.commit()
        pid = cur.lastrowid
    # ★ 累加今日活跃快照（日报专用，与上面的累计 message_count 相互独立）
    try:
        _bump_daily_user_stat(conn, group_id, user_qq, user_name or "")
        conn.commit()
    except Exception as e:
        logger.warning(f"daily_user_stats 写入失败: {e}")
    conn.close()
    return pid


def _bump_daily_user_stat(conn, group_id: int, user_qq: int, user_name: str):
    """累加今日(上海时间) 该用户在本群的消息计数。

    须在调用方已打开的 conn 上执行，由调用方负责 commit/close。
    用 INSERT ... ON CONFLICT 实现首条消息建行、后续累加。
    """
    today = timeutil.today_shanghai().isoformat()  # YYYY-MM-DD
    conn.execute(
        """INSERT INTO daily_user_stats (group_id, user_qq, user_name, stat_date, msg_count)
           VALUES (?,?,?,?,1)
           ON CONFLICT(group_id, user_qq, stat_date) DO UPDATE SET
               msg_count = msg_count + 1,
               user_name = excluded.user_name""",
        (group_id, user_qq, user_name, today),
    )


def get_user_profile(user_qq: int, group_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM user_profiles WHERE user_qq=? AND group_id=?", (user_qq, group_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_all_profiles(user_qq: int) -> list[dict]:
    """获取某用户在所有群的 profile（用于私聊记忆注入）"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM user_profiles WHERE user_qq=? ORDER BY message_count DESC LIMIT 10",
        (user_qq,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_group_profiles(group_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM user_profiles WHERE group_id=? ORDER BY message_count DESC LIMIT 100",
        (group_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_user_profiles(query: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM user_profiles WHERE user_name LIKE ? OR nickname LIKE ?
           OR interests LIKE ? OR notes LIKE ? ORDER BY message_count DESC LIMIT 50""",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===================== Conversation Memories =====================

def add_conversation_memory(group_id: int, topic: str, summary: str,
                            participants: list[int], key_facts: list[str],
                            message_count: int = 0, importance: float = 0.5) -> int:
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO conversation_memories (group_id, topic, summary, participants,
           key_facts, message_count, importance, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (group_id, topic, summary, json.dumps(participants, ensure_ascii=False),
         json.dumps(key_facts, ensure_ascii=False), message_count,
         float(importance), now))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


def get_recent_memories(group_id: int, limit: int = 10, since: Optional[str] = None) -> list[dict]:
    """最近对话记忆。

    since: 可选，形如 "2026-08-11 00:00:00" 的起始时间字符串；
           传入则只返回 created_at >= since 的记忆（用于日报只取今天）。
           默认 None 保持旧行为，不影响其他调用方。
    """
    conn = get_connection()
    if since:
        rows = conn.execute(
            """SELECT * FROM conversation_memories WHERE group_id=? AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (group_id, since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM conversation_memories WHERE group_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (group_id, limit),
        ).fetchall()
    conn.close()
    return [_memory_row_to_dict(r) for r in rows]


def get_today_message_stats(group_id: int) -> dict:
    """本群【今天】(上海时间) 的消息统计，数据来自 daily_user_stats 快照表。

    Returns:
        {"total": int, "users": [{"user_qq","user_name","count"}, ...]}
    """
    today = timeutil.today_shanghai().isoformat()
    conn = get_connection()
    total = conn.execute(
        "SELECT COALESCE(SUM(msg_count), 0) FROM daily_user_stats WHERE group_id=? AND stat_date=?",
        (group_id, today),
    ).fetchone()[0]
    users = [
        dict(r)
        for r in conn.execute(
            """SELECT user_qq, user_name, msg_count AS count FROM daily_user_stats
               WHERE group_id=? AND stat_date=? ORDER BY msg_count DESC LIMIT 10""",
            (group_id, today),
        ).fetchall()
    ]
    conn.close()
    return {"total": total or 0, "users": users}


def get_today_violation_count(group_id: int) -> int:
    """本群今天(上海日期前缀匹配 recorded_at) 新增的违规数。

    注：violations.recorded_at 由 datetime.now() 写入（系统本地时间）；
    若服务器时区非上海，此计数可能略有偏差，但不影响 daily_user_stats 的准确性。
    """
    today_prefix = timeutil.today_shanghai().isoformat()  # YYYY-MM-DD
    conn = get_connection()
    cnt = conn.execute(
        "SELECT COUNT(*) FROM violations WHERE group_id=? AND recorded_at LIKE ?",
        (group_id, today_prefix + "%"),
    ).fetchone()[0]
    conn.close()
    return cnt


def get_important_memories(group_id: int, limit: int = 8) -> list[dict]:
    """按重要性 + 时间排序拉记忆——高分记忆优先露脸。
    排序: 重要性 0.7 权重，1.0 权重*天数衰减。
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT *,
                  (importance * 0.7
                   + (1.0 / (1 + (julianday('now') - julianday(created_at)) / 7)) * 0.3
                  ) AS score
           FROM conversation_memories
           WHERE group_id=?
           ORDER BY score DESC LIMIT ?""",
        (group_id, limit)
    ).fetchall()
    conn.close()
    return [_memory_row_to_dict(r) for r in rows]


def search_memories(query: str, group_id: int = 0, limit: int = 20) -> list[dict]:
    conn = get_connection()
    if group_id:
        rows = conn.execute(
            """SELECT * FROM conversation_memories
               WHERE group_id=? AND (topic LIKE ? OR summary LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (group_id, f"%{query}%", f"%{query}%", limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM conversation_memories
               WHERE topic LIKE ? OR summary LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit)
        ).fetchall()
    conn.close()
    return [_memory_row_to_dict(r) for r in rows]


def get_memory_stats() -> dict:
    conn = get_connection()
    total_memories = conn.execute("SELECT COUNT(*) FROM conversation_memories").fetchone()[0]
    total_profiles = conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
    total_facts = conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0]
    conn.close()
    return {"total_memories": total_memories, "total_profiles": total_profiles,
            "total_facts": total_facts}


# ===================== Knowledge Facts =====================

def add_knowledge_fact(fact: str, category: str = "general", source_group: int = 0,
                       source_user: int = 0, source_user_name: str = "",
                       confidence: float = 0.5) -> int:
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 检查是否已存在相似事实
    existing = conn.execute(
        "SELECT id, confidence FROM knowledge_facts WHERE fact=? AND source_group=?",
        (fact, source_group)
    ).fetchone()
    if existing:
        new_conf = min(1.0, existing["confidence"] + 0.1)
        conn.execute(
            "UPDATE knowledge_facts SET confidence=?, updated_at=? WHERE id=?",
            (new_conf, now, existing["id"]))
        conn.commit()
        fid = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO knowledge_facts (fact, category, source_group, source_user,
               source_user_name, confidence, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (fact, category, source_group, source_user, source_user_name, confidence, now, now))
        conn.commit()
        fid = cur.lastrowid
    conn.close()
    return fid


def get_relevant_facts(query: str = "", group_id: int = 0, limit: int = 20) -> list[dict]:
    conn = get_connection()
    if query:
        rows = conn.execute(
            """SELECT * FROM knowledge_facts
               WHERE fact LIKE ? OR category LIKE ?
               ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit)
        ).fetchall()
    elif group_id:
        rows = conn.execute(
            """SELECT * FROM knowledge_facts WHERE source_group=?
               ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
            (group_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM knowledge_facts ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_facts_by_category(category: str, limit: int = 50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM knowledge_facts WHERE category=? ORDER BY confidence DESC LIMIT ?",
        (category, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===================== Episodes（结构化事件记忆） =====================

def add_episode(group_id: int, date: str = "", time: str = "",
                participants: str = "", keywords: list[str] = None,
                importance: int = 3, title: str = "", content: str = "",
                message_count: int = 0, episode_id: str = "") -> str:
    """写入一条结构化事件记忆，返回 episode id。"""
    import uuid
    eid = episode_id or str(uuid.uuid4())
    try:
        importance = max(1, min(5, int(importance)))
    except (TypeError, ValueError):
        importance = 3
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO episodes
           (id, group_id, date, time, participants, keywords, importance,
            title, content, message_count, created_at, last_recalled_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, group_id, date, time, participants,
         json.dumps(keywords or [], ensure_ascii=False),
         importance, title, content, message_count, now, now))
    conn.commit()
    conn.close()
    return eid


def get_episodes(group_id: int, limit: int = 200) -> list[dict]:
    """取某群的全部事件记忆（新的在前），keywords 已解析为 list。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE group_id=? ORDER BY created_at DESC LIMIT ?",
        (group_id, limit)).fetchall()
    conn.close()
    return [_episode_row_to_dict(r) for r in rows]


def get_episodes_since(group_id: int, since: str, limit: int = 20) -> list[dict]:
    """取某群 since（含）之后创建的事件记忆，旧的在前。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE group_id=? AND created_at>=? ORDER BY created_at ASC LIMIT ?",
        (group_id, since, limit)).fetchall()
    conn.close()
    return [_episode_row_to_dict(r) for r in rows]


def touch_episode_recall(episode_id: str):
    """回忆强化：命中检索后刷新 last_recalled_at。"""
    conn = get_connection()
    conn.execute(
        "UPDATE episodes SET last_recalled_at=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), episode_id))
    conn.commit()
    conn.close()


def _episode_row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["keywords"] = json.loads(d.get("keywords") or "[]")
    except Exception:
        d["keywords"] = []
    return d


# ===================== Understandings（稳定认知） =====================

def add_understanding(group_id: int, subject: str, keywords: list[str] = None,
                      content: str = "", linked_episode: str = "",
                      understanding_id: str = "") -> str:
    """新增一条稳定认知，返回 id。"""
    import uuid
    uid = understanding_id or str(uuid.uuid4())
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history = []
    if linked_episode:
        history.append({"episode_id": linked_episode, "date": now[:10],
                        "title": "初次形成", "content": content})
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO understandings
           (id, group_id, subject, keywords, content, linked_episodes,
            history, created_at, updated_at, recall_count, last_recalled_at)
           VALUES (?,?,?,?,?,?,?,?,?,0,'')""",
        (uid, group_id, subject,
         json.dumps(keywords or [], ensure_ascii=False), content,
         json.dumps([linked_episode] if linked_episode else [], ensure_ascii=False),
         json.dumps(history, ensure_ascii=False), now, now))
    conn.commit()
    conn.close()
    return uid


def get_understandings(group_id: int, limit: int = 200) -> list[dict]:
    """取某群的全部稳定认知（最近更新的在前），JSON 字段已解析。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM understandings WHERE group_id=? ORDER BY updated_at DESC LIMIT ?",
        (group_id, limit)).fetchall()
    conn.close()
    return [_understanding_row_to_dict(r) for r in rows]


def update_understanding(understanding_id: str, subject: str = "",
                         keywords: list[str] = None, content: str = "",
                         linked_episode: str = "", history_title: str = ""):
    """更新一条稳定认知。content 变化时在 history 追加演化快照；
    只印证（content 为空）时只改 subject/keywords，不追加历史。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM understandings WHERE id=?", (understanding_id,)).fetchone()
    if not row:
        conn.close()
        return
    d = _understanding_row_to_dict(row)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_subject = subject or d["subject"]
    new_keywords = keywords if keywords else d["keywords"]
    new_content = content or d["content"]

    linked = list(d["linked_episodes"])
    if linked_episode and linked_episode not in linked:
        linked.append(linked_episode)

    history = list(d["history"])
    if content and content != d["content"]:
        history.append({
            "episode_id": linked_episode or "",
            "date": now[:10],
            "title": history_title or "认知更新",
            "content": new_content,
        })

    conn.execute(
        """UPDATE understandings SET subject=?, keywords=?, content=?,
           linked_episodes=?, history=?, updated_at=? WHERE id=?""",
        (new_subject, json.dumps(new_keywords, ensure_ascii=False), new_content,
         json.dumps(linked, ensure_ascii=False),
         json.dumps(history, ensure_ascii=False), now, understanding_id))
    conn.commit()
    conn.close()


def touch_understanding_recall(understanding_id: str):
    """回忆强化：命中检索后刷新 recall_count / last_recalled_at。"""
    conn = get_connection()
    conn.execute(
        """UPDATE understandings SET recall_count=recall_count+1,
           last_recalled_at=? WHERE id=?""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), understanding_id))
    conn.commit()
    conn.close()


def _understanding_row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("keywords", "linked_episodes", "history"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except Exception:
            d[key] = []
    return d


# ===================== Helpers =====================

def _memory_row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("participants", "key_facts"):
        if key in d and isinstance(d[key], str):
            try: d[key] = json.loads(d[key])
            except: pass
    return d


# ===================== Training Data (手动训练数据) =====================

def training_data_add(message_text: str, label: str = "violation",
                      category: str = "", keywords: str = "", notes: str = "") -> int:
    """添加一条手动训练数据"""
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO training_data (message_text, label, category, keywords, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (message_text, label, category, keywords, notes, now, now))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def training_data_update(tid: int, message_text: str = "", label: str = "",
                         category: str = "", keywords: str = "", notes: str = "") -> bool:
    """更新一条训练数据"""
    conn = get_connection()
    existing = conn.execute("SELECT * FROM training_data WHERE id=?", (tid,)).fetchone()
    if not existing:
        conn.close()
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = {"updated_at": now}
    if message_text: fields["message_text"] = message_text
    if label: fields["label"] = label
    if category: fields["category"] = category
    if keywords: fields["keywords"] = keywords
    if notes: fields["notes"] = notes
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE training_data SET {sets} WHERE id=?", (*fields.values(), tid))
    conn.commit()
    conn.close()
    return True


def training_data_delete(tid: int) -> bool:
    """删除一条训练数据"""
    conn = get_connection()
    conn.execute("DELETE FROM training_data WHERE id=?", (tid,))
    ok = conn.total_changes > 0
    conn.commit()
    conn.close()
    return ok


def training_data_list(label: str = "", category: str = "",
                       applied: Optional[int] = None,
                       limit: int = 50, offset: int = 0) -> list[dict]:
    """列出训练数据，支持筛选"""
    conn = get_connection()
    where, params = [], []
    if label: where.append("label=?"); params.append(label)
    if category: where.append("category=?"); params.append(category)
    if applied is not None: where.append("applied=?"); params.append(applied)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM training_data {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def training_data_search(q: str, limit: int = 50) -> list[dict]:
    """搜索训练数据"""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM training_data WHERE message_text LIKE ? OR keywords LIKE ?
           OR notes LIKE ? OR category LIKE ? ORDER BY created_at DESC LIMIT ?""",
        (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def training_data_stats() -> dict:
    """训练数据统计"""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM training_data").fetchone()[0]
    violation_count = conn.execute(
        "SELECT COUNT(*) FROM training_data WHERE label='violation'").fetchone()[0]
    normal_count = conn.execute(
        "SELECT COUNT(*) FROM training_data WHERE label='normal'").fetchone()[0]
    applied_count = conn.execute(
        "SELECT COUNT(*) FROM training_data WHERE applied=1").fetchone()[0]
    unapplied_count = total - applied_count
    # 按分类统计
    cat_rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM training_data WHERE category!='' GROUP BY category ORDER BY cnt DESC").fetchall()
    conn.close()
    return {
        "total": total,
        "violation_count": violation_count,
        "normal_count": normal_count,
        "applied_count": applied_count,
        "unapplied_count": unapplied_count,
        "categories": [{"category": r["category"], "count": r["cnt"]} for r in cat_rows],
    }


def training_data_batch_add(items: list[dict]) -> int:
    """批量添加训练数据，返回成功条数"""
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for item in items:
        try:
            conn.execute(
                """INSERT INTO training_data (message_text, label, category, keywords, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (item.get("message_text", ""), item.get("label", "violation"),
                 item.get("category", ""), item.get("keywords", ""),
                 item.get("notes", ""), now, now))
            count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return count


def training_data_mark_applied(tids: list[int]) -> int:
    """标记训练数据为已应用，返回更新条数"""
    if not tids:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" for _ in tids)
    conn.execute(
        f"UPDATE training_data SET applied=1, updated_at=? WHERE id IN ({placeholders})",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *tids))
    count = conn.total_changes
    conn.commit()
    conn.close()
    return count


def training_data_get_unapplied(limit: int = 200) -> list[dict]:
    """获取未应用的训练数据"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM training_data WHERE applied=0 ORDER BY created_at ASC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===================== AI Audit (破坏性动作审计) =====================

def record_ai_audit(group_id: int, action: str, target_qq: int = None,
                    reason: str = "", verdict: str = "",
                    master_instructed: bool = False) -> int:
    """记录一条 AI 破坏性动作审计，返回 audit id"""
    conn = get_connection()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO ai_audit (ts, group_id, action, target_qq, reason,
           judge_verdict, master_instructed) VALUES (?,?,?,?,?,?,?)""",
        (ts, int(group_id), action,
         int(target_qq) if target_qq else None,
         reason[:500], verdict, 1 if master_instructed else 0))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def list_ai_audit(target_qq: int = None, action: str = None,
                  group_id: int = None, limit: int = 50,
                  only_undone: bool = False) -> list[dict]:
    """查询审计记录，按 id 倒序（最新在前）。only_undone=True 只看未撤销的"""
    conn = get_connection()
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
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_ai_audit_undone(audit_id: int) -> bool:
    """标记某条审计已撤销（主人执行了解禁/撤精华后调）。返回是否有行被更新"""
    conn = get_connection()
    cur = conn.execute("UPDATE ai_audit SET undone = 1 WHERE id = ?", (int(audit_id),))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


# ===================== 每小时用量统计（面板图表） =====================

def usage_hour_add(tokens_in: int = 0, tokens_cached: int = 0, tokens_out: int = 0,
                   llm_calls: int = 0, msgs_in: int = 0):
    """向当前小时聚合一行累加用量。统计失败静默——绝不能影响主流程。"""
    try:
        from . import timeutil
        hour = timeutil.now_shanghai().strftime("%Y-%m-%d %H:00")
        conn = get_connection()
        conn.execute(
            """INSERT INTO hourly_usage (hour, llm_calls, tokens_in, tokens_cached, tokens_out, msgs_in)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(hour) DO UPDATE SET
                 llm_calls     = llm_calls     + excluded.llm_calls,
                 tokens_in     = tokens_in     + excluded.tokens_in,
                 tokens_cached = tokens_cached + excluded.tokens_cached,
                 tokens_out    = tokens_out    + excluded.tokens_out,
                 msgs_in       = msgs_in       + excluded.msgs_in""",
            (hour, int(llm_calls), int(tokens_in), int(tokens_cached),
             int(tokens_out), int(msgs_in)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def usage_hourly(hours: int = 48) -> list[dict]:
    """最近 N 小时逐小时用量（空小时补零，时间正序）。"""
    from datetime import timedelta
    from . import timeutil
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM hourly_usage ORDER BY hour DESC LIMIT ?", (int(hours),)
    ).fetchall()
    conn.close()
    by_hour = {r["hour"]: dict(r) for r in rows}
    out = []
    cur = timeutil.now_shanghai().replace(minute=0, second=0, microsecond=0)
    for i in range(int(hours) - 1, -1, -1):
        h = (cur - timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
        r = by_hour.get(h)
        out.append({
            "hour": h,
            "llm_calls": r["llm_calls"] if r else 0,
            "tokens_in": r["tokens_in"] if r else 0,
            "tokens_cached": r["tokens_cached"] if r else 0,
            "tokens_out": r["tokens_out"] if r else 0,
            "msgs_in": r["msgs_in"] if r else 0,
        })
    return out


def usage_summary(hours: int = 24) -> dict:
    """最近 N 小时汇总（含缓存命中率）。"""
    total = {"llm_calls": 0, "tokens_in": 0, "tokens_cached": 0,
             "tokens_out": 0, "msgs_in": 0}
    for r in usage_hourly(hours):
        for k in total:
            total[k] += r[k]
    all_in = total["tokens_in"] + total["tokens_cached"]
    total["cache_hit_rate"] = round(total["tokens_cached"] / all_in * 100, 1) if all_in else 0.0
    return total
