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
    """生成群聊总结，返回格式化文本"""
    now = timeutil.now_shanghai()
    date_str = now.strftime("%Y年%m月%d日")

    # ── 收集数据 ──
    # 1. 最近消息窗口（最近 N 条原始消息）
    window = memory._group_message_window.get(group_id, [])
    recent_msgs = window[-50:] if len(window) > 50 else window

    # 2. 对话记忆
    memories = db.get_recent_memories(group_id, limit=5)

    # 3. 活跃用户
    profiles = db.get_group_profiles(group_id)
    active_users = [p for p in profiles if p.get("message_count", 0) > 5][:10]

    # 4. 违规统计
    v_stats = db.get_violation_stats(group_id=group_id)

    # 5. 知识碎片
    facts = db.get_relevant_facts(group_id=group_id, limit=10)

    # ── 构建上下文 ──
    lines = [f"【{date_str} 群聊数据】", ""]

    # 消息量
    msg_count = len(recent_msgs)
    lines.append(f"📊 最近消息数: {msg_count} 条")

    # 活跃成员
    if active_users:
        names = ", ".join(
            f"{p.get('user_name') or p.get('nickname') or p['user_qq']}({p.get('message_count',0)}条)"
            for p in active_users[:8]
        )
        lines.append(f"👥 活跃成员: {names}")

    # 最近话题（从记忆摘要提取）
    if memories:
        topics = [m.get("topic", "") for m in memories if m.get("topic")]
        if topics:
            lines.append(f"💬 最近话题: {', '.join(topics[:5])}")

    # 违规
    if v_stats.get("total", 0) > 0:
        lines.append(f"🛡️ 违规: {v_stats['total']} 条 (自动: {v_stats.get('auto_count',0)})")

    lines.append("")
    lines.append("---")
    lines.append("最近消息摘要:")
    for m in recent_msgs[-20:]:
        name = m.get("user_name", "") or str(m.get("user_qq", ""))
        msg = m.get("message", "")[:100]
        if msg:
            lines.append(f"  {name}: {msg}")

    context = "\n".join(lines)

    # ── 调用 LLM 生成总结 ──
    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return _fallback_summary(group_id, date_str, msg_count, active_users, v_stats)

    try:
        from . import llm_call

        response = await llm_call.llm_create(
            model=ms.get("model", "glm-5.2-free"),
            max_tokens=400,
            temperature=0.7,
            system=f"你是名叫「{bot_name or config.BOT_NAME}」的QQ群机器人。请根据提供的群聊数据生成一份简洁有趣的每日总结。风格活泼、带emoji、像朋友聊天。控制在200字以内。按以下格式：先总评（今天群里怎么样），再列出关键数据（消息量/活跃成员/热门话题/违规），最后来一句有趣的收尾。",
            messages=[{"role": "user", "content": context}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"AI 总结生成失败: {e}")
        return _fallback_summary(group_id, date_str, msg_count, active_users, v_stats)

def _fallback_summary(group_id: int, date: str, msg_count: int,
                       active_users: list, v_stats: dict) -> str:
    """LLM 不可用时的回退总结"""
    parts = [f"📊 **{date} 群聊日报**", ""]
    parts.append(f"💬 消息量: {msg_count} 条")
    if active_users:
        top3 = active_users[:3]
        names = ", ".join(p.get("user_name") or str(p.get("user_qq")) for p in top3)
        parts.append(f"👥 活跃: {names}")
    if v_stats.get("total", 0) > 0:
        parts.append(f"🛡️ 违规处理: {v_stats['total']} 条")
    parts.append("")
    parts.append("💡 提示: 配置 AI 大模型后可获得更智能的总结")
    return "\n".join(parts)
