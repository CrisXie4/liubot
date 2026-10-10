"""
违规处罚 —— 撤回 + 阶梯禁言 + 群里 @ 警告 + 记违规
====================================================
背景：以前违规处理走 send_private_msg 私聊警告，但 bot 跟群友不是好友，
私聊发不出去（"没办法跟他私聊"）。改成一站式工具：
撤回 → 算累犯 → 阶梯禁言 → 群里 @ 警告 → 记录到违规库 → 触发学习。

禁言阶梯（用户已确认）：
  第1次 30min → 第2次 1h → 第3次 3h → 第4次 24h → 第5次+ 创建踢人审批
"""
import logging
from typing import Optional

logger = logging.getLogger("qqbot")

# 阶梯禁言（秒）。下标 = 累犯次数 - 1
_MUTE_LADDER = [
    30 * 60,      # 1: 30 分钟
    60 * 60,      # 2: 1 小时
    3 * 60 * 60,  # 3: 3 小时
    24 * 60 * 60, # 4: 24 小时
]
# 第 5 次及以上 → 不禁言，创建踢人审批


def _fmt_duration(seconds: int) -> str:
    """120 -> '2分钟', 3600 -> '1小时', 86400 -> '24小时'"""
    if seconds >= 3600:
        h = seconds / 3600
        return f"{h:g}小时"
    return f"{seconds // 60}分钟"


def compute_mute_seconds(violation_count: int) -> tuple[int, str]:
    """按累犯次数算禁言时长。返回 (秒数, 中文描述)。
    返回 (0, '踢人审批') 表示该走踢人流程，不再禁言。"""
    if violation_count > len(_MUTE_LADDER):
        return 0, "踢人审批"
    sec = _MUTE_LADDER[min(violation_count, len(_MUTE_LADDER)) - 1]
    return sec, _fmt_duration(sec)


async def punish_violation(
    bot,
    group_id: int,
    user_id: int,
    user_name: str,
    message_id: int,
    reason: str,
    bot_is_admin: bool = True,
    keywords: Optional[list[str]] = None,
) -> str:
    """一站式违规处罚。返回给 AI 的结果描述。

    步骤：
    1. 撤回消息
    2. 查历史违规次数，算本次是第几次
    3. 按阶梯禁言（bot 是管理员时）
    4. 群里 @ 对方警告（替代私聊）
    5. 记录违规 + 触发学习
    """
    from . import db, learner, violation as _vio
    from .config import config

    keywords = keywords or []
    user_name = user_name or str(user_id)

    # 1. 撤回
    recalled = False
    if message_id:
        try:
            recalled = await bot.delete_msg(message_id)
        except Exception as e:
            logger.warning(f"punish 撤回失败: {e}")

    # 2. 算累犯（含本次）
    try:
        v_records = db.query_violations(group_id=group_id, violator_qq=user_id)
        violation_count = len(v_records) + 1
    except Exception:
        violation_count = 1

    # 3. 阶梯禁言
    mute_sec, mute_desc = compute_mute_seconds(violation_count)
    muted = False
    kick_approval = False
    if bot_is_admin:
        if mute_sec > 0:
            try:
                muted = await bot.set_group_ban(group_id, user_id, mute_sec)
            except Exception as e:
                logger.warning(f"punish 禁言失败: {e}")
        else:
            # 第5次+ → 创建踢人审批
            try:
                from . import guardian
                aid = guardian.create_action_request(
                    group_id, user_id, user_name,
                    action="kick",
                    reason=f"累计违规 {violation_count} 次 | {reason}",
                    message=reason,
                )
                kick_approval = True
                # 通知主人
                try:
                    nid = await bot.send_private_msg(int(config.OWNER_QQ),
                        f"🚨 **待审批: 踢人**\n"
                        f"群: {group_id}\n"
                        f"用户: {user_name}({user_id})\n"
                        f"原因: 累计违规 {violation_count} 次\n"
                        f"本次: {reason[:60]}\n\n"
                        f"回复 \"同意 {aid}\" 执行踢人\n"
                        f"回复 \"拒绝 {aid}\" 取消"
                    )
                    guardian.set_notify_msg(aid, nid)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"punish 创建踢人审批失败: {e}")

    # 4. 群里 @ 警告（替代私聊）
    try:
        if kick_approval:
            warn = (f"[CQ:at,qq={user_id}] 🚫 你的消息因「{reason}」被撤回"
                    f"（第 {violation_count} 次违规，已提交踢人审批）")
        elif muted:
            warn = (f"[CQ:at,qq={user_id}] 🛡️ 你的消息因「{reason}」被撤回"
                    f"并禁言 {mute_desc}（第 {violation_count} 次违规，再犯会加重）")
        else:
            warn = (f"[CQ:at,qq={user_id}] ⚠️ 你的消息因「{reason}」被撤回"
                    f"（第 {violation_count} 次违规）")
        await bot.send_group_msg(group_id, warn)
    except Exception as e:
        logger.warning(f"punish 群警告发送失败: {e}")

    # 5. 记录违规 + 学习
    try:
        db.add_violation(
            message_id=message_id, group_id=group_id, group_name="",
            violator_qq=user_id, violator_name=user_name,
            raw_message=reason,
            violation_note=f"AI标记({violation_count}次)",
            keywords=keywords, recorded_by=int(config.BOT_QQ or 0),
            confidence=1.0, source="ai",
        )
    except Exception as e:
        logger.warning(f"punish 记违规失败: {e}")

    try:
        learner.learn_from_violation(reason, keywords, source="ai")
    except Exception:
        pass

    # 返回给 AI 的结果
    parts = []
    if recalled:
        parts.append("已撤回")
    if kick_approval:
        parts.append(f"已提交踢人审批（第{violation_count}次）")
    elif muted:
        parts.append(f"禁言 {mute_desc}（第{violation_count}次）")
    elif bot_is_admin:
        parts.append(f"已处理（第{violation_count}次）")
    parts.append(f"原因: {reason}")
    result = " + ".join(parts) + " ✓"
    logger.warning(f"AI 执行违规处罚: user={user_id} {result}")
    return result
