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
    topic = random.choice(topics_pool)
    # 好奇心越强，搜索越认真
    angles = ["最新", "今天", "本周", "最近", "2026"]
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

        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=200,
            temperature=0.8,
            system=f"你现在{cs.get_dominant_mood(state)}。根据搜索结果做两件事：\n"
                   "1. 判断这些新闻是否值得分享给群友（够新鲜、够有趣、与群友兴趣相关）\n"
                   "2. 用一句话（50字内）总结其中最值得分享的一条，要有你的个人态度\n\n"
                   "⚠️ 新鲜度硬约束：任何一条发布日期早于 7 天前都不能算\"够新鲜\"，必须 DECISION: no。\n"
                   "只有发布日期在 7 天内的才算新。没有日期的可以放宽。\n\n"
                   "输出格式:\n"
                   "DECISION: yes 或 no\n"
                   "SUMMARY: 你的总结（50字内）",
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
            return {"text": "", "image_url": "", "decision": "no", "topic": topic}

        news_text = summary[:120]

        # 选图片：img_src 才是图片直链，url 是来源网页（当图片发会失败！）
        # 并且要做图文相关性校验——不能随手发一张跟新闻无关的图
        image_url = ""
        if image_results:
            # 提取新闻文本中的关键词用于匹配
            import jieba as _jieba
            news_keywords = set(
                w for w in _jieba.lcut(news_text + topic)
                if len(w) >= 2 and not w.isdigit() and w not in ("的", "了", "是", "在", "和", "有", "就", "不", "也", "都", "这", "我", "你", "他", "她", "它", "们", "个", "把", "被", "从", "到", "对", "让", "给", "跟", "为", "以", "可以", "没有", "知道", "觉得", "一个", "这个", "那个")
            )
            for img in image_results:
                url = img.get("img_src") or img.get("thumbnail_src") or ""
                if not url or url.lower().endswith(".svg"):
                    continue
                # 检查图片元数据与新闻的关联度
                img_title = img.get("title", "") or img.get("alt", "") or ""
                img_source = img.get("source", "") or img.get("origin", {}).get("title", "") or ""
                img_meta_text = f"{img_title} {img_source}"
                if img_meta_text.strip():
                    img_keywords = set(
                        w for w in _jieba.lcut(img_meta_text)
                        if len(w) >= 2
                    )
                    overlap = news_keywords & img_keywords
                    if len(overlap) >= 2:
                        image_url = url
                        logger.debug(f"图文匹配: overlap={overlap} img={img_title[:40]}")
                        break
                else:
                    # 没有元数据时也先保留（老图片搜索引擎结果可能不带标题）
                    image_url = url
                    break

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
        return {"text": news_text, "image_url": image_url, "decision": "yes",
                "topic": topic, "latest_published": latest_pub}

    except Exception as e:
        logger.debug(f"自主学习失败: {e}")
        return None


async def share_news_if_ready(bot):
    """分享新闻（无冷却硬限制，由软限速兜底）。返回消息(str 或消息段 list)或 None"""
    state = cs.get_state()

    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"新闻分享跳过: {why}")
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

    image_url = result.get("image_url", "")
    topic = result.get("topic", "")
    # 个性化推送：检查是否有群友的兴趣匹配这条新闻
    if topic and text:
        asyncio.create_task(_push_personalized_news(bot, topic, text, image_url))

    seg = await _fetch_image_segment(image_url) if image_url else None
    if seg:
        return [seg, {"type": "text", "data": {"text": f"\n📰 {text}"}}]
    return f"📰 {text}"


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


async def maybe_auto_essence(bot) -> int:
    """扫所有群，对近 N 分钟高影响力消息主动设精华。返回设精数目。

    适配说明：brief 假设有 `influence.score(text)`，实际没有。
    influence 体系是"按作者互动次数"打分（mention_bot/reply/total_score 等），
    没有"按文本质量"打分。所以这里改成：在最近消息窗口里，
    挑出**作者影响力最高**且**超过阈值**的消息设精华——
    即"尊重/活跃成员发的话优先被设为精华"，而非"最好的文本被设为精华"。

    阈值经实测数据校准（data/member_influence.json top15: 144..6018），
    默认 50.0 大约能覆盖每群 top 30-50 活跃成员。
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

                try:
                    ok = await bot.set_essence_msg(best["message_id"])
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
                msg = f"📰 刷到一条你可能感兴趣的：\n\n{news_text}\n\n（顺手分享，不感兴趣无视就好～）"
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
    state = cs.get_state()

    # 睡觉/深夜静默时段/精力耗尽 → 什么都不发
    ok, why = cs.can_speak_proactively(state)
    if not ok:
        logger.debug(f"volition 跳过: {why}")
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
                cs.add_diary(state, f"主动说：「{text}」", mood)
                logger.info(f"🎙️ Volition SPEAK [{mood}]: {text}")
                # 记录主动发言时间戳（用于被忽视追踪）
                cs.record_proactive_message(state)
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
    if behavior["action"] == "learn_something" and behavior["priority"] > 0.35:
        result = await autonomous_learn(bot)
        if result and result.get("decision") == "yes" and result.get("text"):
            global _last_news_share
            _last_news_share = time.time()
            image_url = result.get("image_url", "")
            seg = await _fetch_image_segment(image_url) if image_url else None
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
