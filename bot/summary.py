"""
群聊总结模块
- @Bot 总结 → AI 生成群聊日报
- 每天 17:00 自动推送
"""
import logging
from datetime import datetime

from . import db, memory, timeutil
from .config import config

logger = logging.getLogger("qqbot")

async def generate_and_publish_summary(bot, group_id: int, bot_name: str = ""):
    """生成总结并同时发群公告 + 群消息"""
    text = await generate_group_summary(group_id, bot_name)
    if text:
        await bot.send_group_msg(group_id, text)
        # 同步发布群公告（取前100字为标题）
        title = text.split("\n")[0][:50] if text else "今日群聊日报"
        content = text[:500]
        try:
            await bot.send_group_notice(group_id, title, content)
        except Exception:
            pass  # 公告失败不影响消息发送
    return text

async def generate_group_summary(group_id: int, bot_name: str = "") -> str:
    """生成群聊总结，返回格式化文本。

    所有数字（消息量/活跃成员/违规）均限定为【今天】(上海时间)，
    避免再出现“总量 20 条但单人 617 条”这类历史累计与当日数据混搭的自相矛盾。
    """
    now = timeutil.now_shanghai()
    today = timeutil.today_shanghai()
    today_str = today.isoformat()                   # YYYY-MM-DD
    today_start = today_str + " 00:00:00"           # 用于 created_at >= ? 过滤
    date_str = today.strftime("%Y年%m月%d日")
    # 上海时间今日 0 点的时间戳，用于过滤内存窗口里的陈旧消息
    start_today_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ── 今日消息统计（每日快照表，真实今日数据；不再用内存窗口计数）──
    today_stats = db.get_today_message_stats(group_id)
    msg_count = today_stats["total"]
    active_users = today_stats["users"]

    # ── 今日对话记忆（只取今天创建的，避免把旧话题说成今天）──
    memories = db.get_recent_memories(group_id, limit=5, since=today_start)

    # ── 今日违规 ──
    today_violations = db.get_today_violation_count(group_id)

    # ── 最近原始消息窗口：只保留今天的，作 LLM 样例参考（非完整计数来源）──
    window = memory._group_message_window.get(group_id, [])
    recent_today = [m for m in window if (m.get("ts") or 0) >= start_today_ts][-20:]

    # ── 构建上下文（每个数字都明确标注“今日”，防止 LLM 误读时间范围）──
    lines = [f"【{date_str} 群聊数据（以下均为今日上海时间）】", ""]

    lines.append(f"📊 今日消息数: {msg_count} 条")
    if active_users:
        names = ", ".join(
            f"{u.get('user_name') or u['user_qq']}({u['count']}条)" for u in active_users[:8]
        )
        lines.append(f"👥 今日活跃成员: {names}")
    if today_violations > 0:
        lines.append(f"🛡️ 今日违规: {today_violations} 条")

    if memories:
        topics = []
        for m in memories:
            t = m.get("topic", "")
            if not t:
                continue
            when = (m.get("created_at") or "")[5:16]  # MM-DD HH:MM
            topics.append(f"{t}({when})" if when else t)
        if topics:
            lines.append(f"💬 今日话题: {', '.join(topics[:5])}")

    lines.append("")
    lines.append("---")
    lines.append("今日最近消息样例(部分):")
    for m in recent_today[-15:]:
        name = m.get("user_name", "") or str(m.get("user_qq", ""))
        msg = (m.get("message") or "")[:100]
        if msg:
            lines.append(f"  {name}: {msg}")

    context = "\n".join(lines)

    # ── 调用 LLM 生成总结 ──
    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return _fallback_summary(date_str, msg_count, active_users, today_violations)

    try:
        from . import llm_call

        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=400,
            temperature=0.7,
            system=(
                f"你是名叫「{bot_name or config.BOT_NAME}」的QQ群机器人。请根据提供的【今日】群聊数据"
                "生成一份简洁有趣的每日总结。风格活泼、带emoji、像朋友聊天，控制在200字以内。"
                "重要：只能使用数据里【今天】的内容。如果今日消息数为0或没有数据，"
                "请如实说『今天群里很安静』，绝不能编造话题或把别天的内容说成今天。"
                "格式：先总评（今天群里怎么样），再列关键数据（消息量/活跃成员/热门话题/违规，均仅限今天），"
                "最后一句有趣收尾。"
            ),
            messages=[{"role": "user", "content": context}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"AI 总结生成失败: {e}")
        return _fallback_summary(date_str, msg_count, active_users, today_violations)

def _fallback_summary(date: str, msg_count: int,
                       active_users: list, today_violations: int) -> str:
    """LLM 不可用时的回退总结（数字同样仅限今天）"""
    parts = [f"📊 **{date} 群聊日报**", ""]
    parts.append(f"💬 今日消息量: {msg_count} 条")
    if active_users:
        top3 = active_users[:3]
        names = ", ".join(
            f"{u.get('user_name') or str(u.get('user_qq'))}({u.get('count', 0)}条)" for u in top3
        )
        parts.append(f"👥 今日活跃: {names}")
    if today_violations > 0:
        parts.append(f"🛡️ 今日违规处理: {today_violations} 条")
    parts.append("")
    parts.append("💡 提示: 配置 AI 大模型后可获得更智能的总结")
    return "\n".join(parts)
