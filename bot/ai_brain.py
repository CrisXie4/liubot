"""
AI 大脑 — 意识系统的对外接口
============================
v3.0: 全面接入自我意识引擎 (bot/consciousness.py)
  - 心情不再是简单的 happy/sad，而是 9 维情绪系统
  - 昼夜节律：早上迷糊→上午清醒→中午饭困→下午恢复→傍晚兴奋→晚上放松→深夜睡觉
  - 社交需求：被@开心、被忽视失落、被夸膨胀、被怼倔强
  - 日记系统：定期写"心里话"
  - 行为决策：无聊找人聊天、好奇去搜东西、累了不想说话
  - 新闻分享：AI 自己判断值不值得发，带图片
"""
import asyncio
import difflib
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional

from . import timeutil
from .config import config
from . import consciousness as cs
from . import memory
from . import llm_call
from . import guardian, influence

logger = logging.getLogger("qqbot")

# ═══════════════════════════════════════════════════════════
#  兼容旧接口 — 心情/状态
# ═══════════════════════════════════════════════════════════

def load_brain() -> dict:
    """兼容旧接口：从意识状态导出旧格式"""
    state = cs.get_state()
    mood = cs.get_dominant_mood(state)
    mood_score = cs.get_mood_score(state)
    return {
        "mood": mood,
        "mood_score": mood_score,
        "personality_traits": list(state.get("traits", {}).keys()),
        "known_topics": state.get("today", {}).get("interesting_topics", [])[-10:],
        "last_learned": state.get("last_thought_time", ""),
        "last_news_shared": "",  # 已不再存储
        "interests": ["AI", "科技", "编程", "游戏", "动漫", "新闻"],
    }

def save_brain(brain: dict):
    """兼容旧接口：不需要单独保存了"""
    pass  # 意识系统自己管理持久化

def track_bot_activity():
    """Bot 发消息时调用"""
    state = cs.get_state()
    cs.on_bot_sent_message(state)

def update_mood(event: str = "", impact: float = 0.05):
    """兼容旧接口：手动更新心情"""
    state = cs.get_state()
    if impact > 0:
        cs.apply_emotion_delta(state, "joy", impact)
        cs.apply_emotion_delta(state, "satisfaction", impact * 0.5)
    elif impact < 0:
        cs.apply_emotion_delta(state, "irritation", -impact)
        cs.apply_emotion_delta(state, "joy", impact * 0.8)

def needs_status_sync() -> bool:
    """检查是否需要同步 QQ 状态（基于心情变化）"""
    # 每次 tick 都检查，不需要单独标记
    return True

# 心情 → QQ 个性化在线状态 (status, ext_status, 文案)
# 关键：可见的个性化状态必须 status=10 且带上对应 ext_status；
# 只发 status=10/ext_status=0 只是"在线"，看不出任何变化（这正是"无法设置状态"的原因）。
# ext_status 取值来自 NapCat SetOnlineStatus 文档（均为免费个性化状态）。
MOOD_ONLINE_STATUS = {
    "excited":       (10, 1056, "嗨到飞起"),
    "happy":         (10, 1058, "元气满满"),
    "content":       (10, 1059, "悠哉哉"),
    "neutral_happy": (10, 1071, "好运锦鲤"),
    "neutral":       (10, 1300, "摸鱼中"),
    "bored":         (10, 1060, "无聊中"),
    "lonely":        (10, 1061, "想静静"),
    "curious":       (10, 1018, "学习中"),
    "irritated":     (10, 1401, "emo中"),
    "grumpy":        (10, 1063, "一言难尽"),
    "tired":         (10, 1032, "熬夜中"),
}


async def sync_qq_status(bot):
    """同步 QQ 在线状态 —— 用个性化在线状态让心情可见"""
    state = cs.get_state()
    mood = cs.get_dominant_mood(state)
    hour = timeutil.now_shanghai().hour

    if state.get("sleeping"):
        # 深夜/睡觉：白天累了显示"熬夜中"，睡觉时段显示"睡觉中"
        status, ext_status, label = 10, 1016, "睡觉中"
    elif mood == "tired" and (hour >= 22 or hour < 6):
        status, ext_status, label = 10, 1016, "睡觉中"
    else:
        status, ext_status, label = MOOD_ONLINE_STATUS.get(mood, (10, 1300, "摸鱼中"))

    try:
        ok = await bot.set_online_status(status, ext_status)
        # 个性化状态若被拒（极少数账号不支持某 ext_status），回退到基础在线状态
        if not ok and ext_status:
            ok = await bot.set_online_status(status, 0)
            label = "在线(回退)"
        if ok:
            logger.info(f"QQ状态同步成功: {mood} → 「{label}」(status={status}, ext={ext_status})")
        else:
            logger.warning(f"QQ状态同步失败: {mood} → 「{label}」(status={status}, ext={ext_status})")
    except Exception as e:
        logger.warning(f"QQ状态同步异常: {e}")

def get_mood_prompt() -> str:
    """获取心情注入提示词"""
    state = cs.get_state()
    return cs.get_consciousness_prompt(state)


# ═══════════════════════════════════════════════════════════
#  意识事件接口（供 handler 调用）
# ═══════════════════════════════════════════════════════════

def on_bot_mentioned(user_name: str, group_id: int):
    """被人 @ 了"""
    state = cs.get_state()
    if state.get("sleeping"):
        cs.wake_up(state, cranky=True)
        logger.info(f"💤 被 {user_name} 叫醒，带起床气")
    cs.on_mentioned(state, user_name, group_id)
    _check_mood_change(state, "被@")

def on_bot_replied_to(user_name: str):
    """有人回复了 Bot"""
    state = cs.get_state()
    cs.on_replied_to_bot(state, user_name)
    _check_mood_change(state, "被回复")

def on_bot_ignored():
    """发了消息没人理"""
    state = cs.get_state()
    cs.on_ignored(state)
    _check_mood_change(state, "被忽视")

def on_bot_praised(user_name: str):
    """被人夸了"""
    state = cs.get_state()
    cs.on_praised(state, user_name)
    _check_mood_change(state, "被夸")

def on_bot_insulted(user_name: str):
    """被人怼了"""
    state = cs.get_state()
    cs.on_insulted(state, user_name)
    _check_mood_change(state, "被怼")

def on_new_member(user_name: str):
    """新人入群"""
    state = cs.get_state()
    cs.on_new_friend(state, user_name)
    _check_mood_change(state, "新人")

def on_interesting_chat(topic: str):
    """聊到了有趣的话题"""
    state = cs.get_state()
    cs.on_interesting_topic(state, topic)

def on_learned(topic: str):
    """学到了新东西"""
    state = cs.get_state()
    cs.on_learned_something(state, topic)


def on_read_emotional_content(valence: float, arousal: float, topic: str = ""):
    """读到带有情绪的群聊内容（群友在聊的话题，不是对 Bot 说的）。

    让 Bot 对世界有反应——看到悲伤消息会难过，看到开心事会跟着开心。
    valence: -1(很负面)..+1(很正面)，arousal: 0(平淡)..1(激烈)。
    """
    state = cs.get_state()
    cs.on_read_emotional_content(state, valence, arousal, topic)

_pending_status_sync = False

def _check_mood_change(state: dict, trigger: str):
    """检查心情是否发生了明显变化"""
    global _pending_status_sync
    # 简单地标记为需要同步（下一分钟 tick 会处理）
    _pending_status_sync = True


# ═══════════════════════════════════════════════════════════
#  自主学习 + 新闻分享
# ═══════════════════════════════════════════════════════════

LEARN_INTERVAL = 60  # 防同一秒内连调
_last_learn_time = 0

# 新闻分享冷却：两条新闻路径（share_news_if_ready / maybe_proactive_action 的 learn 分支）共用
# 2 小时内只允许成功分享一次，避免刷屏（尤其夜间）。每次"真正发出去"后刷新时间戳。
NEWS_SHARE_COOLDOWN = 2 * 60 * 60
_last_news_share = 0.0


def _news_on_cooldown() -> bool:
    """距离上次成功分享新闻是否还没过冷却（2 小时）。"""
    return (time.time() - _last_news_share) < NEWS_SHARE_COOLDOWN


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


# ── 分享去重记忆（持久化在意识状态里，重启不丢）──
# 事故实录（2026-10-09/10）：同一条 Steam"720款新游"新闻连发两天，
# 根因是完全没有去重记忆（load_brain 里 last_news_shared 已废弃）。
# 现在存 {topic, text, ts} 到 consciousness 的 state（随 data/consciousness.json 持久化）。
NEWS_HISTORY_TTL = 12 * 24 * 3600   # 分享记忆保留 12 天
NEWS_HISTORY_CAP = 30               # 最多记 30 条
NEWS_DUP_RATIO = 0.55               # 与历史相似度超过该值视为同一事件
RECENT_TOPICS_TTL = 48 * 3600       # 选题 48 小时内不重选
RECENT_TOPICS_CAP = 8               # 最近选题记录上限


def _get_news_history(state: dict) -> list[dict]:
    """取有效的分享史（顺带清理过期条目）。存于 state["news_shared"]。"""
    history = state.setdefault("news_shared", [])
    now = time.time()
    history[:] = [h for h in history if now - h.get("ts", 0) < NEWS_HISTORY_TTL]
    return history


def _remember_shared_news(state: dict, topic: str, text: str) -> None:
    """记录一条已分享/已刷到的新闻并立即持久化（防止重启后重复分享）。"""
    if not text:
        return
    history = _get_news_history(state)
    history.append({"topic": (topic or "")[:30], "text": text[:120], "ts": time.time()})
    if len(history) > NEWS_HISTORY_CAP:
        del history[: len(history) - NEWS_HISTORY_CAP]
    cs.save()


def _is_duplicate_news(text: str, state: dict) -> bool:
    """新 summary 与历史 text 相似度 > NEWS_DUP_RATIO 视为同一事件，拦截。"""
    if not text:
        return False
    norm = re.sub(r"\s+", "", (text or "").lower())
    if len(norm) < 4:
        return False
    for h in _get_news_history(state):
        old = re.sub(r"\s+", "", (h.get("text", "") or "").lower())
        if not old:
            continue
        if difflib.SequenceMatcher(None, norm, old).ratio() > NEWS_DUP_RATIO:
            return True
    return False


def _get_recent_topic_names(state: dict) -> set:
    """48h 内用过的选题（不再重选）。存于 state["recent_topics"]。"""
    rec = state.setdefault("recent_topics", [])
    now = time.time()
    rec[:] = [r for r in rec if now - r.get("ts", 0) < RECENT_TOPICS_TTL]
    return {r.get("topic", "") for r in rec}


def _remember_topic_used(state: dict, topic: str) -> None:
    """记录一个刚用过的选题，供后续排除。"""
    if not topic:
        return
    rec = state.setdefault("recent_topics", [])
    rec.append({"topic": topic[:30], "ts": time.time()})
    if len(rec) > RECENT_TOPICS_CAP:
        del rec[: len(rec) - RECENT_TOPICS_CAP]


def _pick_topic(state: dict, topics_pool: list) -> str:
    """从候选里选一个选题，排除 48h 内用过的（全被排除才放回，保证总有得选）。"""
    recent = _get_recent_topic_names(state)
    fresh_pool = [t for t in topics_pool if t not in recent]
    topic = random.choice(fresh_pool or topics_pool)
    _remember_topic_used(state, topic)
    return topic


def _news_angles() -> list:
    """搜索角度池：月份词用当前时间动态生成，随月份自然轮换，
    替代写死的"最近/2026"（观感像模板的元凶之一）。"""
    now_dt = timeutil.now_shanghai()
    return ["最新", "今天", "本周", f"{now_dt.month}月", f"{now_dt.month % 12 + 1}月"]


def _recent_shared_prompt(state: dict) -> str:
    """构建"最近聊过这些别再选"的提示词块（提示词层去重，最省钱的拦截点）。"""
    history = _get_news_history(state)
    if not history:
        return ""
    lines = []
    for h in reversed(history[-8:]):  # 最近 5-8 条，最新的排前面
        t = (h.get("text", "") or "").strip()
        if t:
            lines.append(f"- {t[:40]}（主题：{h.get('topic', '') or '未知'}）")
    if not lines:
        return ""
    return (
        "\n🚫 重复豁免：下面是你最近几天已经搜过/聊过的事件，"
        "不要再选同类或同一事件，也别再说差不多的话：\n" + "\n".join(lines) + "\n\n"
    )


async def _fetch_image_segment(image_url: str) -> Optional[dict]:
    """下载外链图片并转成 base64 消息段。失败返回 None。

    为什么要下载再发：NapCat 直接拉取搜索引擎给的外链图片经常失败
    （防盗链、缺 Referer/UA、重定向、HTML 实体），导致图片被剥离只剩文字。
    服务端下好转 base64 交给 NapCat 最稳。
    """
    if not image_url:
        return None
    import base64 as _b64
    import html as _html
    from urllib.parse import urlparse

    clean_url = _html.unescape(image_url)
    try:
        p = urlparse(clean_url)
        referer = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else clean_url
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            resp = await http.get(clean_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            })
        if resp.status_code != 200:
            logger.debug(f"新闻配图下载失败 HTTP {resp.status_code}: {clean_url[:60]}")
            return None
        ct = resp.headers.get("content-type", "").lower()
        data = resp.content
        # 校验确实是图片，且大小合理（太小多半是占位/错误图，太大超 WS 帧）
        if "image" not in ct or len(data) < 1024 or len(data) > 3 * 1024 * 1024:
            logger.debug(f"新闻配图无效 (ct={ct}, size={len(data)}): {clean_url[:60]}")
            return None
        b64 = _b64.b64encode(data).decode("utf-8")
        return {"type": "image", "data": {"file": f"base64://{b64}"}}
    except Exception as e:
        logger.debug(f"新闻配图下载异常: {e}")
        return None


async def autonomous_learn(bot) -> Optional[dict]:
    """AI 自主上网学习。返回 {'text': ..., 'image_url': ..., 'decision': 'yes'/'no', 'topic': ...}"""
    global _last_learn_time
    now = time.time()
    if now - _last_learn_time < LEARN_INTERVAL:
        return None
    _last_learn_time = now

    state = cs.get_state()

    # 睡觉时不学习
    if state.get("sleeping"):
        return None

    # 精力太低也不想学
    if state["emotions"]["energy"] < 0.15:
        return None

    # 选一个感兴趣的方向 — 偶尔参考群友兴趣
    topics_pool = ["AI 最新进展", "本周科技新闻", "编程技术", "本周游戏发布", "本周新番动画",
                   "有趣的冷知识", "本周互联网热点", "本周数码产品"]
    # 30% 概率根据群友兴趣选题
    if random.random() < 0.3:
        try:
            from . import db as _db
            conn = _db.get_connection()
            rows = conn.execute(
                "SELECT interests FROM user_profiles WHERE interests IS NOT NULL AND interests != '' ORDER BY message_count DESC LIMIT 20"
            ).fetchall()
            conn.close()
            if rows:
                user_topics = []
                for r in rows:
                    parts = [p.strip() for p in r["interests"].replace("，", ",").split(",") if len(p.strip()) >= 2]
                    user_topics.extend(parts)
                if user_topics:
                    topics_pool.extend(user_topics)
        except Exception:
            pass
    # 去模板：选题时排除 48h 内用过的（近期老八股轮着抽就会被认出来）
    topic = _pick_topic(state, topics_pool)
    # angle 池：月份词动态生成，替代写死的"最近/2026"
    now_dt = timeutil.now_shanghai()
    angles = _news_angles()
    query = f"{topic} {random.choice(angles)}"

    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return None

    try:
        from . import web_search
        text_results = await web_search.search_recent(query, max_age_days=7, max_results=5)
        image_results = await web_search.search_images(query, max_results=3)

        if not text_results:
            return None

        search_text = "\n".join(
            f"{i+1}. {r['title']} (发布:{r.get('published','未知')}): {r['content'][:150]}"
            for i, r in enumerate(text_results[:3])
        )

        time_hint = (
            f"{now_dt.strftime('%m-%d %H:%M')}"
            f"（{['周一','周二','周三','周四','周五','周六','周日'][now_dt.weekday()]}）"
        )
        # 身份锚定：不写死名字，从人设配置动态取（Web 面板改名即生效），
        # 防止模型自报底层模型名（如"作为Qwythos"这种穿帮）
        from . import personality as _personality
        bot_name = _personality.load().get("name") or config.BOT_NAME

        # 提示词层去重：把最近聊过的注入决策 prompt，同一事件反复被搜到时在这里拦住
        recent_shared = _recent_shared_prompt(state)

        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=200,
            temperature=0.8,
            system=(
                "你是QQ群的一个群友。根据用户消息给出的搜索结果做两件事：\n"
                "1. 判断这些新闻是否值得分享给群友（够新鲜、够有趣、与群友兴趣相关）\n"
                "2. 用一句话（15~30字）说出最值得说的一条，像跟熟人随口提一嘴\n\n"
                "✍️ 说话方式（极重要）：像随手甩在群里的闲聊，不是新闻播报。\n"
                "- 禁止播报腔词：「资讯」「更新了」「最新」「分享给大家」「推荐」\n"
                "- 禁止：感叹号轰炸、任何emoji/符号开头、念来源网站名当开场\n"
                "- 不要总结陈词，说你觉得有意思的那个点就行\n"
                "- 说具体的数字/细节，别空泛\n"
                "口吻参考：\n"
                "「刚瞄到 OpenAI 放出新路线图 有点东西」\n"
                "「下个月新番有两部能追 别问我怎么知道的」\n"
                "「这新闻有点离谱 你们自己看」\n\n"
                f"{recent_shared}"
                "⚠️ 新鲜度硬约束：任何一条发布日期早于 7 天前都不能算\"够新鲜\"，必须 DECISION: no。\n"
                "只有发布日期在 7 天内的才算新。没有日期的可以放宽。\n\n"
                "⏰ 时段自觉：深夜到清晨（23点-次日8点）群友多半在睡觉，除非真正紧急重大的突发，"
                "否则倾向 DECISION: no，别打扰大家休息。\n\n"
                "输出格式:\n"
                "DECISION: yes 或 no\n"
                "SUMMARY: 你的总结（15~30字）\n\n"
                "—— 以下为本次判断的数据 ——\n"
                f"你的名字：{bot_name}。你现在的心情：{cs.get_dominant_mood(state)}。\n"
                f"当前时间：{time_hint}。"
                f"时段：{'深夜到清晨，别打扰大家' if (now_dt.hour >= 23 or now_dt.hour < 8) else '白天/晚间'}。"
            ),
            messages=[{"role": "user", "content": f"搜索结果：\n{search_text}"}],
        )
        raw = response.content[0].text.strip()

        # 解析 AI 决策
        decision = "yes"
        summary = raw
        for line in raw.split("\n"):
            ls = line.strip()
            if ls.upper().startswith("DECISION:"):
                decision = ls.split(":", 1)[1].strip().lower()
            elif ls.upper().startswith("SUMMARY:"):
                summary = ls.split(":", 1)[1].strip()

        if decision.startswith("no"):
            logger.info(f"AI 决定不分享: {query} → 不够有趣")
            cs.on_learned_something(state, topic)
            cs.add_diary(state, f"查了下「{query[:20]}」，没什么好分享的", "meh")
            # 也记进分享史（记标题即可）：同一事件反复被搜到时，
            # 上面注入的"重复豁免"提示词就能直接拦住，不用等发出去再拦
            if text_results:
                _remember_shared_news(state, topic, text_results[0].get("title", ""))
            return {"text": "", "image_url": "", "decision": "no", "topic": topic}

        news_text = summary[:120]

        # 选图片：不再"没元数据就直接采用"——书封广告就是这么混进来的
        # （2026-09-06 实录：FBI 驾照泄露新闻配了张《冷知识》书封）。
        # 现在只产出候选列表（按元数据相关性排序），发送前还有视觉校验把关。
        image_candidates = _rank_image_candidates(image_results, news_text, topic)
        image_url = image_candidates[0] if image_candidates else ""

        # 日志：记录是否找到了匹配的图片
        if image_results and not image_url:
            logger.info(f"新闻配图跳过: 无相关图片 (topic={topic})")

        # 记录学习
        cs.on_learned_something(state, topic)
        cs.add_diary(state, f"学到了「{news_text[:40]}」，准备分享给群友", "curious")

        logger.info(f"AI 学习: {query} → {news_text[:60]}" + (" [图]" if image_url else ""))
        latest_pub = ""
        if text_results:
            pubs = [r.get("published", "") for r in text_results if r.get("published")]
            if pubs:
                latest_pub = max(pubs)
        return {"text": news_text, "image_url": image_url,
                "image_candidates": image_candidates, "decision": "yes",
                "topic": topic, "latest_published": latest_pub}

    except Exception as e:
        logger.debug(f"自主学习失败: {e}")
        return None


def _rank_image_candidates(image_results: list, news_text: str, topic: str) -> list[str]:
    """把图片搜索结果按图文相关性排序，返回最多 3 个候选 URL。

    - 有标题/来源元数据的：按与新闻关键词的重叠数排序，重叠 ≥2 的优先
    - 没元数据的：只能垫底（旧版直接采用是图文不符的元凶），由视觉校验兜底
    """
    import jieba as _jieba
    stopwords = {"的", "了", "是", "在", "和", "有", "就", "不", "也", "都", "这",
                 "我", "你", "他", "她", "它", "们", "个", "把", "被", "从", "到",
                 "对", "让", "给", "跟", "为", "以", "可以", "没有", "知道", "觉得",
                 "一个", "这个", "那个"}
    news_keywords = set(
        w for w in _jieba.lcut(news_text + topic)
        if len(w) >= 2 and not w.isdigit() and w not in stopwords
    )
    scored: list[tuple[int, str]] = []
    unscored: list[str] = []
    for img in image_results or []:
        url = img.get("img_src") or img.get("thumbnail_src") or ""
        if not url or url.lower().endswith(".svg"):
            continue
        img_title = img.get("title", "") or img.get("alt", "") or ""
        img_source = img.get("source", "") or img.get("origin", {}).get("title", "") or ""
        meta = f"{img_title} {img_source}".strip()
        if not meta:
            unscored.append(url)
            continue
        meta_kw = set(w for w in _jieba.lcut(meta) if len(w) >= 2)
        scored.append((len(news_keywords & meta_kw), url))

    scored.sort(key=lambda x: x[0], reverse=True)
    ranked = ([u for n, u in scored if n >= 2]
              + [u for n, u in scored if 0 < n < 2]
              + unscored)
    seen: set[str] = set()
    out: list[str] = []
    for u in ranked:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:3]


async def _verify_image_match(img_b64: str, content_type: str,
                              news_text: str, topic: str) -> bool:
    """发送前让视觉模型看一眼：这张图配这条新闻吗？（vision 主渠道 = DeepSeek）

    返回 False = 图文不符（不发图，只发文字）；LLM 不可用也返回 False（宁缺毋滥）。
    """
    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return False
    try:
        response = await llm_call.llm_create(
            priority="P2",
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=8,
            temperature=0.0,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": content_type or "image/jpeg",
                                             "data": img_b64}},
                {"type": "text", "text": (
                    "你要给一条新闻配图。判断这张图片是否与新闻内容直接相关。\n"
                    f"新闻：{news_text[:150]}（主题：{topic}）\n"
                    "相关 = 同一事件、同一领域的人物/场景/实物。\n"
                    "书籍封面、商品广告、二维码、无关风景、纯文字截图、表情包 → 一律 NO。\n"
                    "只回答一个词：MATCH 或 NO"
                )},
            ]}],
        )
        raw = response.content[0].text.strip().upper()
        ok = raw.startswith("MATCH")
        logger.info(f"新闻配图视觉校验: {'✓ MATCH' if ok else '✗ NO'} ({raw[:12]})")
        return ok
    except Exception as e:
        logger.debug(f"配图视觉校验失败: {e}")
        return False


async def _pick_verified_image_segment(result: dict) -> Optional[dict]:
    """从候选里挑第一张【视觉校验通过】的图，返回消息段；全不合适 → None（只发文字）。"""
    candidates = list(result.get("image_candidates") or [])
    first = result.get("image_url", "")
    if first and first not in candidates:
        candidates.insert(0, first)
    if not candidates:
        return None

    news_text = result.get("text", "")
    topic = result.get("topic", "")
    import base64 as _b64
    import html as _html
    from urllib.parse import urlparse

    for url in candidates[:3]:
        try:
            clean_url = _html.unescape(url)
            p = urlparse(clean_url)
            referer = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else clean_url
            import httpx
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
                resp = await http.get(clean_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                })
            if resp.status_code != 200:
                continue
            data = resp.content
            ct = resp.headers.get("content-type", "").lower()
            # 校验确实是图片，且大小合理（太小是占位图，太大超 WS 帧）
            if "image" not in ct or len(data) < 1024 or len(data) > 3 * 1024 * 1024:
                continue
            b64 = _b64.b64encode(data).decode("utf-8")
            if await _verify_image_match(b64, ct, news_text, topic):
                return {"type": "image", "data": {"file": f"base64://{b64}"}}
            logger.info(f"新闻配图文图不符，弃用: {clean_url[:60]}")
        except Exception as e:
            logger.debug(f"配图候选校验异常: {e}")
    logger.info("新闻配图全部候选未通过视觉校验，本次只发文字")
    return None


def _strip_newsletter_prefix(text: str) -> str:
    """剥掉模型偶发的简报腔前缀（📰📢🔥 等表情符号开头）——真人甩消息不会这么发。"""
    import re as _re
    text = (text or "").strip()
    # 仅剥"行首的 emoji/符号连串"，消息中间的情绪表达不动
    return _re.sub(r"^(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\s【】\[\]])+",
                   "", text).strip() or text


async def share_news_if_ready(bot):
    """分享新闻。受 2 小时冷却硬限制（两条新闻路径共用 _last_news_share）。
    返回消息(str 或消息段 list)或 None"""
    global _last_news_share

    state = cs.get_state()
    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"新闻分享跳过: {why}")
        return None

    if _news_on_cooldown():
        logger.debug("新闻分享跳过: 2 小时冷却中")
        return None

    result = await autonomous_learn(bot)
    if not result:
        return None

    if result.get("decision") == "no":
        return None

    latest_published = result.get("latest_published", "")
    if _is_news_too_old(latest_published, max_age_days=30):
        logger.info(f"新闻太旧，放弃分享: published={latest_published}")
        cs.add_diary(state, f"查到的都是旧闻（{latest_published}），算了", "meh")
        return None

    text = result.get("text", "")
    if not text:
        return None

    # ★ 二层去重（硬闸）：真正发出前，和历史分享比相似度，同一事件不重发
    # （提示词层可能被弱模型无视，这里是最后一道）
    if _is_duplicate_news(text, state):
        logger.info(f"新闻去重拦截（与近期分享相似度 > {NEWS_DUP_RATIO}）: {text[:50]}")
        cs.add_diary(state, f"「{text[:20]}」前几天刚聊过，不重复说了", "meh")
        return None

    image_url = result.get("image_url", "")
    topic = result.get("topic", "")
    # 个性化推送：检查是否有群友的兴趣匹配这条新闻
    if topic and text:
        asyncio.create_task(_push_personalized_news(bot, topic, text, image_url))

    seg = await _pick_verified_image_segment(result)
    # 即将真正发出去 → 刷新冷却时间戳（两条路径共用）
    _last_news_share = time.time()
    text = _strip_newsletter_prefix(text)
    # 记入分享史并立即持久化（提示词层与相似度层后续都靠它去重）
    _remember_shared_news(state, topic, text)
    if seg:
        return [seg, {"type": "text", "data": {"text": f"\n{text}"}}]
    return text


# ── 重大新闻检测（@全体用）──
_BIG_NEWS_KEYWORDS = (
    "灾难", "突发", "重大", "地震", "事故", "疫情", "警告", "警报",
    "海啸", "台风", "爆炸", "坍塌", "火灾", "恐袭", "战争",
)


def is_big_news(text: str) -> bool:
    """判断一段新闻文本是否属于“重大”（值得 @全体）。"""
    if not text:
        return False
    return any(kw in text for kw in _BIG_NEWS_KEYWORDS)


# 明显不够格设精华的整句 token（去空白后精确匹配，大小写不敏感）
_TRIVIAL_ESSENCE_TOKENS = {
    # 打招呼 / 问候
    "你好", "您好", "你们好", "你们好吗", "大家早", "大家早上好", "大家晚上好",
    "早上好", "早", "早安", "早呀", "中午好", "下午好", "晚上好", "晚安", "哈喽",
    "哈罗", "嗨", "嗨呀", "hi", "hello", "hey", "yo",
    # 应答 / 水
    "在吗", "在么", "在不在", "在", "嗯", "嗯嗯", "哦", "额", "啊", "哈", "唉",
    "哎", "哎呀", "好的", "好", "好呀", "ok", "okay", "okk", "yes", "收到",
    "了解", "知道", "知道啦", "明白", "行", "可以", "对的", "是的", "没问题",
    # 感谢 / 客套
    "谢谢", "谢了", "thanks", "thx", "多谢", "辛苦了", "麻烦你了",
    # 笑 / 语气 / 称赞（太短的）
    "哈哈", "哈哈哈", "哈哈哈哈", "呵呵", "嘻嘻", "嘿", "嘿嘿", "666", "6666",
    "6", "牛", "牛逼", "厉害", "瑞思拜", "卧槽", "我操", "草", "绝了",
    # 测试 / 探活
    "测试", "test", "1", "111", "？", "?", "你是谁", "你是机器人吗", "你是ai吗",
    "你是真的吗", "你是人吗", "你在吗", "活着吗", "在吗在吗", "你是活的吗",
}

# 纯笑声 / 纯标点 / 纯表情 的整句模式
_TRIVIAL_ESSENCE_RE = re.compile(
    r"^(哈+|呵+|嘻+|嘿+|啊+|哦+|嗯+|哎+|唉+|嘤+|噢+|呜+|嘿+"
    r"|！+|!+|\?+|？+|。+|,+|，+|~+|～+|＝+|=>+|>+|<+|6+"
    r"|🎉+|🔥+|👍+|💯+|✨+|😍+|😂+|🤣+|❤+|💕+|🐶+|狗头+|滑稽+|草+|卧槽+|牛+|哔+)+$",
    re.UNICODE,
)


def is_essence_worthy(text: str) -> bool:
    """判断一条消息内容是否「够格」被设为精华。

    只拦截明显的水内容：空、纯 CQ 码小表情、过短（≤4 个硬字符）、
    纯打招呼/应答/客套/称赞、纯笑声/标点/表情。其余一律放行，
    交给 AI 自己判断是否真的精彩。
    """
    if not text:
        return False
    # 剥掉所有 CQ 码（图片/表情/引用/@ 等），只看纯文本
    clean = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
    if not clean:
        # 整条没文字：图片/合并转发可能值得精华，放行；纯小表情/戳一戳不放行
        return ("[CQ:image" in text) or ("[CQ:forward" in text)
    # 去掉所有空白后的硬字符长度
    hard = re.sub(r"\s+", "", clean)
    if len(hard) <= 4:
        return False
    if hard.lower() in _TRIVIAL_ESSENCE_TOKENS:
        return False
    if _TRIVIAL_ESSENCE_RE.match(clean):
        return False
    return True


async def _get_message_text_by_id(bot, group_id: int, message_id: int) -> str:
    """尽量取出某条消息的文本，供精华门槛判断。先查内存窗口，再回 OneBot 查。"""
    try:
        from . import memory
        win = memory._group_message_window.get(group_id, [])
        for m in reversed(win):
            if m.get("message_id") == int(message_id):
                return m.get("message", "") or ""
    except Exception:
        pass
    try:
        data = await bot.get_msg(int(message_id))
        if data:
            # raw_message 是含 CQ 码的字符串，is_essence_worthy 会自动剥
            return data.get("raw_message", "") or ""
    except Exception:
        pass
    return ""


# 已被 LLM 判过"够不够格设精华"的 message_id，避免重复判定、重复调 LLM。
# 用 set 存 int，超过上限就清空最旧的一半（按插入顺序）。
_judged_essence_ids: set = set()
_JUDGED_ESSENCE_CAP = 500


async def _llm_judge_essence(text: str) -> bool:
    """让 LLM 严格判断一条消息是否真的"够格设为群精华"。

    旧逻辑只挡纯水（打招呼/过短/纯表情），其余一律放行 → 活跃成员随便一句话也被设精。
    现在补上正向判断：只有真正精彩/搞笑/信息量大/说得特别到位的才返回 True。
    """
    if not text:
        return False
    try:
        ms = memory.load_model_settings()
        if not ms.get("api_key"):
            return False
        # 只看纯文本（剥 CQ 码），限长省 token
        clean = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
        if not clean or len(re.sub(r"\s+", "", clean)) <= 4:
            return False

        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=8,
            temperature=0.2,
            system=(
                "你判断这条群聊消息是否够格设为「群精华」。标准极严：只有真正精彩、搞笑、"
                "信息量大、说得特别到位、值得群友回头再看一眼的才够格。\n"
                "普通闲聊、陈述、提问、应答、客套、感慨、水一句、打招呼、日常报备——一律不够格。\n"
                "只回一个字：1（够格）或 0（不够格）。"
            ),
            messages=[{"role": "user", "content": clean[:200]}],
        )
        raw = response.content[0].text.strip()
        return raw.startswith("1")
    except Exception as e:
        logger.debug(f"精华 LLM 判定异常: {e}")
        return False


def _remember_judged(message_id) -> None:
    """记录已判过的 message_id，防止重复判定。"""
    if len(_judged_essence_ids) >= _JUDGED_ESSENCE_CAP:
        _judged_essence_ids.clear()
    _judged_essence_ids.add(int(message_id))


async def maybe_auto_essence(bot) -> int:
    """扫所有群，对近 N 分钟高影响力消息主动设精华。返回设精数目。

    三道闸门（任一不通过就跳过）：
      1. 作者影响力 ≥ 阈值（尊重/活跃成员优先）
      2. is_essence_worthy 反向过滤（挡纯水：打招呼/过短/纯表情/纯标点）
      3. ★ LLM 正向判断（这是补的关键一环）——只有真正精彩/搞笑/信息量大的才设
    旧版只做了 1+2，结果任何活跃成员 >4 字的非打招呼话都被设精，看起来像乱设。
    """
    count = 0
    try:
        r = await bot._call("get_group_list", {})
        groups = r.get("data", []) if r.get("status") == "ok" else []

        # 阈值可由 ai_guard_config.json 调整
        cfg = guardian.load_config().get("thresholds", {})
        min_influence = cfg.get("essence_min_author_influence", 50.0)
        lookback_secs = cfg.get("essence_lookback_seconds", 1800)

        now_ts = time.time()
        cutoff = now_ts - lookback_secs

        for g in groups:
            try:
                gid = g.get("group_id", 0)
                if not gid:
                    continue
                # 每群每天 N 次限速
                if not guardian.check_essence_rate(gid):
                    continue

                recent = memory._group_message_window.get(gid, [])
                if not recent:
                    continue

                # 取最近 50 条里时间窗内、有 message_id 的候选
                candidates = [
                    m for m in recent[-50:]
                    if m.get("message_id")
                    and m.get("ts", 0) > cutoff
                    and m.get("user_qq")
                ]
                if not candidates:
                    continue

                # 按作者影响力排序，挑最高
                best = max(
                    candidates,
                    key=lambda m: influence.get_influence(gid, m.get("user_qq", 0)),
                )
                author_score = influence.get_influence(gid, best.get("user_qq", 0))
                if author_score < min_influence:
                    continue

                # 内容门槛：水内容（打招呼/过短/纯表情等）不设精华
                best_text = best.get("message", "") or ""
                if not is_essence_worthy(best_text):
                    logger.debug(
                        f"自主设精华跳过 群{gid}: 内容不够格 «{best_text[:20]}»"
                    )
                    continue

                # 已判过的不再重复判（省 token、防重复设精）
                best_mid = best["message_id"]
                if best_mid in _judged_essence_ids:
                    continue

                # ★ 第三道闸门：LLM 正向判断是否真的够格设精华
                # （旧版只有上面两道，导致活跃成员随便一句话也被设精）
                if not await _llm_judge_essence(best_text):
                    _remember_judged(best_mid)
                    logger.debug(
                        f"自主设精华跳过 群{gid}: LLM 判定不够格 «{best_text[:20]}»"
                    )
                    continue
                _remember_judged(best_mid)

                try:
                    ok = await bot.set_essence_msg(best_mid)
                    if ok:
                        count += 1
                        logger.info(
                            f"⭐ 自主设精华 群{gid}: msg={best['message_id']} "
                            f"作者={best.get('user_name')} 影响力={author_score:.1f}"
                        )
                        await asyncio.sleep(random.uniform(3, 6))
                except Exception as e:
                    logger.debug(f"自主设精华失败 群{gid}: {e}")
            except Exception as e:
                logger.debug(f"maybe_auto_essence 单群异常 群{g.get('group_id')}: {e}")
    except Exception as e:
        logger.debug(f"maybe_auto_essence 异常: {e}")
    return count


async def _push_personalized_news(bot, topic: str, news_text: str, image_url: str = ""):
    """根据群友兴趣，AI 自主决定是否私聊推送新闻"""
    try:
        from . import db as _db
        # 获取所有有记录的用户
        conn = _db.get_connection()
        rows = conn.execute(
            "SELECT user_qq, user_name, interests FROM user_profiles WHERE interests IS NOT NULL AND interests != '' ORDER BY message_count DESC LIMIT 50"
        ).fetchall()
        conn.close()

        if not rows:
            return

        # 让 AI 判断这条新闻跟哪些人相关，是否值得私推
        user_list = "\n".join(
            f"{r['user_qq']} {r['user_name'] or ''}: {r['interests'][:100]}"
            for r in rows[:20]
        )

        ms = memory.load_model_settings()
        if not ms.get("api_key"):
            return

        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=150,
            temperature=0.7,
            system="你有机会把一条新闻私聊推给感兴趣的群友。不是每个群友都需要推送——"
                   "只有新闻内容和ta的兴趣真正相关时才推。最多推给2个人。宁可少推不要滥推。\n"
                   "输出格式: 每行一个QQ号，或者输出 SKIP 表示不推送任何人",
            messages=[{"role": "user", "content": (
                f"新闻主题: {topic}\n"
                f"新闻内容: {news_text[:150]}\n\n"
                f"群友及其兴趣:\n{user_list}\n\n"
                f"谁值得收到这条新闻的私聊推送？（最多2人，或不推则回复SKIP）"
            )}],
        )

        raw = response.content[0].text.strip()
        if raw.upper().startswith("SKIP") or not raw:
            return

        # 解析 QQ 号
        import re
        qq_numbers = re.findall(r"[1-9]\d{4,10}", raw)
        pushed = set()

        for qq_str in qq_numbers[:2]:
            qq = int(qq_str)
            if qq in pushed:
                continue
            pushed.add(qq)

            # 发送私聊
            try:
                msg = f"诶 刚刷到这个 {news_text}"
                await bot.send_private_msg(qq, msg)
                logger.info(f"个性化推送: {qq} ← {topic}")
                await asyncio.sleep(2)
            except Exception as e:
                logger.debug(f"个性化推送失败 {qq}: {e}")

    except Exception as e:
        logger.debug(f"个性化推送异常: {e}")


# ═══════════════════════════════════════════════════════════
#  主动行为（由意识驱动）
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  主动发言防刷屏（硬闸）
# ═══════════════════════════════════════════════════════════
# 日志实录（2026-09-06）：「群里静悄悄，正合适摸鱼～」45 分钟内重复 4 次；
# 连续 3 次被忽视后 LLM 虽选过一次 SILENT，8 分钟后又开始复读。
# 结论：弱模型面前"只给被忽视事实"挡不住刷屏，需要代码层硬闸：
#   1) 被忽视退避：连续 ≥3 次主动发话没人接 → 2 小时内不再主动开口
#   2) 近重复拦截：与 6 小时内发过的主动发言高度相似 → 视为复读，不发送
IGNORED_BACKOFF_THRESHOLD = 3
IGNORED_BACKOFF_SECS = 2 * 60 * 60
SPEAK_DEDUPE_WINDOW = 6 * 60 * 60
SPEAK_SIMILARITY = 0.3

_recent_speaks: list[dict] = []   # [{"ts": float, "norm": str}]，跨调用累积
_last_speak_sent: float = 0.0


def _normalize_speak(text: str) -> str:
    """去掉标点/空白/大小写的规整文本，用于复读比对。"""
    return re.sub(r"[^\w一-鿿]+", "", (text or "").lower())


def _is_near_duplicate_speak(text: str) -> bool:
    """判断主动发言是否与最近发过的高度相似（字符二元组 Jaccard）。"""
    norm = _normalize_speak(text)
    if len(norm) < 4:
        return False
    now = time.time()
    while _recent_speaks and now - _recent_speaks[0]["ts"] > SPEAK_DEDUPE_WINDOW:
        _recent_speaks.pop(0)
    grams = {norm[i:i + 2] for i in range(len(norm) - 1)}
    for old in _recent_speaks:
        if old["norm"] == norm:
            return True
        on = old["norm"]
        old_grams = {on[i:i + 2] for i in range(len(on) - 1)}
        if old_grams and grams:
            jaccard = len(grams & old_grams) / len(grams | old_grams)
            if jaccard >= SPEAK_SIMILARITY:
                return True
    return False


def _remember_speak(text: str):
    """记录一条已发送的主动发言（近重复比对用）。"""
    _recent_speaks.append({"ts": time.time(), "norm": _normalize_speak(text)})
    if len(_recent_speaks) > 20:
        del _recent_speaks[:-20]


async def decide_and_speak(bot):
    """LLM 自主决定要不要主动发言（Volition 系统）。

    替换旧的"情绪阈值过就自动发"逻辑。一次 LLM 调用同时产出
    "内心想法 + 要不要说出来"，输出严格 SPEAK/SILENT 协议。

    喂给 LLM 的"事实包"（只读，不改情绪数值）：
      - 当前心情 + 各情绪数值（原样读 consciousness.emotions）
      - 时间事实（防穿帮）
      - 最近 2-3 条日记
      - "你已经连续 N 次发话没人接"（关键事实）
      - 群活跃度（最近 X 分钟没动静 / 群里正在聊 Y）

    只要 LLM 说 SPEAK 就立即发，无任何上限/冷却。
    返回消息(str 或消息段 list)或 None（SILENT）。
    """
    global _last_speak_sent
    state = cs.get_state()

    # 睡觉/深夜静默时段/精力耗尽 → 什么都不发
    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"volition 跳过: {why}")
        return None

    # 硬闸 1：连续被忽视 → 退避期内不再主动开口
    # （2026-09-06 日志：连续 3 次石沉大海后 LLM 只安静了 8 分钟又开始复读）
    consecutive_ignored = (state.get("today") or {}).get("consecutive_ignored", 0)
    if (consecutive_ignored >= IGNORED_BACKOFF_THRESHOLD
            and time.time() - _last_speak_sent < IGNORED_BACKOFF_SECS):
        logger.info(
            f"🤫 Volition 退避: 连续被忽视 {consecutive_ignored} 次，"
            f"{int(IGNORED_BACKOFF_SECS - (time.time() - _last_speak_sent))}s 内不再主动开口"
        )
        return None

    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return None

    try:
        mood = cs.get_dominant_mood(state)
        e = state["emotions"]
        today = state.get("today", {})
        consecutive_ignored = today.get("consecutive_ignored", 0)
        diary = state.get("diary", [])[:3]
        diary_text = " | ".join(
            f"({d.get('mood', '')}) {d.get('thought', '')[:40]}" for d in diary
        ) if diary else "（今天还没写过日记）"

        # 群活跃度事实
        from . import memory as _mem
        group_activity_fact = _build_group_activity_fact()

        # ★ 关键：被忽视事实（不改情绪，只告诉 LLM）
        ignored_fact = ""
        if consecutive_ignored > 0:
            ignored_fact = (
                f"\n⚠️ 事实：你已经连续 {consecutive_ignored} 次主动发话没人接。"
                "（不是说你必须闭嘴——你自己判断要不要再说）"
            )

        prompt = (
            "你现在一个人待着，脑子里在想事情。要不要在群里说句话？"
            "这是你自己的真实决定，不是任务。\n\n"
            f"【当前心情】{mood}\n"
            f"【情绪数值】快乐{e['joy']:.0%} 精力{e['energy']:.0%} 社交欲{e['social']:.0%} "
            f"无聊{e['boredom']:.0%} 孤独{e['loneliness']:.0%}"
            f"{' 难过'+format(e['sadness'], '.0%') if e.get('sadness', 0) > 0.3 else ''}\n\n"
            f"{cs.get_time_facts_prompt()}\n\n"
            f"【你最近的内心活动（只是状态，不要复读）】\n{diary_text}\n\n"
            f"{group_activity_fact}"
            f"{ignored_fact}\n\n"
            "——\n"
            "你想说话就说，不想说就不说。没人逼你。\n"
            "想说 → 回复：SPEAK: <内容，20字以内，要跟当前时间/心情对得上>\n"
            "不想说 → 回复：SILENT: <一句话原因，写给自己看>\n"
            f"想睡觉 → 回复：SLEEP: <分钟数，30-480>（你现在困意 {state.get('sleepiness', 0):.0%}）\n"
            "格式必须严格按上面来，不要加别的。"
        )

        response = await llm_call.llm_create(
            priority="P2",
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=80,
            temperature=0.9,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # 解析协议
        upper = raw.upper()
        if upper.startswith("SPEAK:"):
            text = raw.split(":", 1)[1].strip().strip('"').strip("'").strip()
            # 去掉可能的换行（只取第一行）
            text = text.split("\n")[0].strip()
            if text and 2 <= len(text) <= 30:
                # 硬闸 2：近重复拦截——复读机式发言直接咽回去
                if _is_near_duplicate_speak(text):
                    cs.add_diary(state, "想说的话跟刚才说的差不多，咽回去了", mood)
                    logger.info(f"🤫 Volition 近重复拦截: {text}")
                    return None
                _remember_speak(text)
                _last_speak_sent = time.time()
                cs.add_diary(state, f"主动说：「{text}」", mood)
                logger.info(f"🎙️ Volition SPEAK [{mood}]: {text}")
                # 主动发言的逐群记录在 main.py 发送循环里做（record_proactive_message）
                return text
            else:
                logger.debug(f"Volition SPEAK 内容长度异常: «{text[:30]}»")
                cs.add_diary(state, "想说什么但没组织好", mood)
                return None
        elif upper.startswith("SILENT:"):
            reason = raw.split(":", 1)[1].strip()[:50]
            cs.add_diary(state, f"选择沉默：{reason}", mood)
            logger.info(f"🤫 Volition SILENT [{mood}]: {reason}")
            return None
        elif upper.startswith("SLEEP:"):
            minutes_str = raw.split(":", 1)[1].strip()
            try:
                minutes = int("".join(c for c in minutes_str if c.isdigit()))
            except Exception:
                minutes = 90
            cs.go_to_sleep(state, minutes)
            logger.info(f"😴 Volition SLEEP: 自主决定睡 {minutes} 分钟")
            return None
        else:
            # 格式不符 → 默认按 SILENT 处理
            logger.debug(f"Volition 输出格式不符，按 SILENT: {raw[:60]}")
            cs.add_diary(state, "想说话但脑子卡了", mood)
            return None

    except Exception as exc:
        logger.debug(f"volition 决策失败: {exc}")
        return None


def _build_group_activity_fact() -> str:
    """构建"群活跃度事实"——告诉 LLM 现在群里什么情况。
    只讲事实，不做判断。
    """
    try:
        from . import memory as _mem
        # 取所有群的最近消息窗口（取第一个有数据的群）
        windows = getattr(_mem, "_group_message_window", {})
        for gid, win in windows.items():
            if not win:
                continue
            now = time.time()
            # 过滤掉 bot 自己的消息
            recent_human = [
                m for m in win[-10:]
                if m.get("user_qq") and not m.get("_is_bot", False)
            ]
            if not recent_human:
                continue
            last_ts = recent_human[-1].get("ts", 0)
            minsquiet = (now - last_ts) / 60 if last_ts else 999
            if minsquiet > 30:
                return f"【群里情况】已经 {int(minsquiet)} 分钟没人说话了，很安静"
            elif minsquiet > 10:
                # 最近几条聊的话题摘要
                topics = []
                for m in recent_human[-3:]:
                    txt = (m.get("message", "") or "").strip()
                    # 剥 CQ 码
                    import re as _re
                    txt = _re.sub(r"\[CQ:[^\]]+\]", "", txt).strip()
                    if txt and len(txt) >= 2:
                        topics.append(txt[:15])
                if topics:
                    return f"【群里情况】最近在聊：{'、'.join(topics)}"
                return "【群里情况】最近有人在说话"
            else:
                topics = []
                for m in recent_human[-3:]:
                    txt = (m.get("message", "") or "").strip()
                    import re as _re
                    txt = _re.sub(r"\[CQ:[^\]]+\]", "", txt).strip()
                    if txt and len(txt) >= 2:
                        topics.append(txt[:15])
                if topics:
                    return f"【群里情况】群里正在聊：{'、'.join(topics)}"
                return "【群里情况】群里挺热闹"
        return "【群里情况】（暂无可读的群消息）"
    except Exception:
        return ""


async def maybe_proactive_action(bot):
    """根据意识状态决定是否主动做点什么。
    优先级：睡觉 > 学习（新闻分享）> volition 自主发言
    返回消息(str 或消息段 list)或 None
    """
    state = cs.get_state()

    # 睡觉/深夜静默时段/精力耗尽 → 什么都不发
    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"主动行为跳过: {why}")
        return None

    behavior = cs.decide_behavior(state)

    # 想学习 → 触发自主学习（保留原逻辑）
    # ★ 受 2 小时新闻冷却限制：冷却中直接跳过，不再搜网/调 LLM，落到 volition
    if behavior["action"] == "learn_something" and behavior["priority"] > 0.35 and not _news_on_cooldown():
        result = await autonomous_learn(bot)
        if result and result.get("decision") == "yes" and result.get("text"):
            # 二层去重（硬闸）：与 share_news_if_ready 同一道，别让这条路径漏过去
            if _is_duplicate_news(result["text"], state):
                logger.info(f"新闻去重拦截（主动学习路径）: {result['text'][:50]}")
                cs.add_diary(state, f"「{result['text'][:20]}」前几天说过了，咽回去", "meh")
            else:
                global _last_news_share
                _last_news_share = time.time()
                _remember_shared_news(state, result.get("topic", ""), result["text"])
                seg = await _pick_verified_image_segment(result)
                if seg:
                    return [seg, {"type": "text", "data": {"text": f"\n💡 闲来无事刷到一条有意思的：{result['text']}"}}]
                return f"💡 刷到一条：{result['text']}"

    # 其余情况交给 volition（LLM 自主决定说不说话）
    # 删除了原来"阈值过就自动发"的 initiate_chat / seek_attention 分支
    return await decide_and_speak(bot)


# ═══════════════════════════════════════════════════════════
#  每日总结（意识驱动）
# ═══════════════════════════════════════════════════════════

async def daily_self_summary() -> Optional[str]:
    """生成一天的自我总结（调用自省）"""
    state = cs.get_state()
    reflection = await cs.self_reflection(state)
    cs.save()
    return reflection


# ═══════════════════════════════════════════════════════════
#  AI 工具: set_mood（让 AI 自己调节心情）
# ═══════════════════════════════════════════════════════════

def ai_set_mood(change: str):
    """AI 调用 set_mood 工具时触发"""
    state = cs.get_state()
    impacts = {
        "happier": ("joy", 0.15),
        "sadder": ("joy", -0.15),
        "excited": ("joy", 0.20),
        "grumpy": ("irritation", 0.15),
        "calm": ("calm", 0.10),
        "neutral": ("joy", 0),
        "curious": ("curiosity", 0.15),
        "tired": ("energy", -0.20),
        "refreshed": ("energy", 0.20),
    }
    if change in impacts:
        em, delta = impacts[change]
        cs.apply_emotion_delta(state, em, delta)
        cs.add_diary(state, f"主动调整了心情: {change}", change)
    logger.info(f"AI 自主调整心情: {change}")


# ═══════════════════════════════════════════════════════════
#  @全体成员（管理模式）
# ═══════════════════════════════════════════════════════════

async def at_all(bot, group_id: int, message: str) -> bool:
    """@全体成员并发送消息"""
    segments = [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "text", "data": {"text": " " + message}},
    ]
    r = await bot._call("send_group_msg", {"group_id": group_id, "message": segments})
    return r.get("status") == "ok"

AT_ALL_TOOL = {
    "name": "at_all",
    "description": "@全体成员发重要通知。仅在管理模式下、有重要事情宣布时使用。不要滥用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "要通知的内容（50字以内）"},
        },
        "required": ["message"],
    },
}
