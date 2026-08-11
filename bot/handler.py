"""
消息处理器 v3 — 分发群消息到各功能模块
新增: 表情收藏、图片/视频理解、消息引用、自动回复决策、群友影响力
"""
import asyncio
import logging
import random
import re
import time
from typing import Optional

from . import (
    chat, violation, automod, trainer, blacklist, learner, memory, db,
    image_mod, cache, sticker, media_reader, quote_handler,
    group_personality, decision_engine, influence, welcome, guardian,
    llm_mod, ai_brain, humanizer, group_events,
)
from .config import config
from .onebot_client import OneBotClient

# 延迟导入：identity 和 reply_policy 在 bot 包初始化期间
# 可能触发 "partially initialized module" 循环导入，改为首次使用时加载
_identity = None
_reply_policy = None

def _get_identity():
    global _identity
    if _identity is None:
        from . import identity as _id
        _identity = _id
    return _identity

def _get_reply_policy():
    global _reply_policy
    if _reply_policy is None:
        from . import reply_policy as _rp
        _reply_policy = _rp
    return _reply_policy

logger = logging.getLogger("qqbot")

# 基础触发词（始终生效）
_BASE_TRIGGERS = ["bot", "Bot", "机器人"]

# QQ 官方账号白名单 — 这些账号的消息不参与审核
_QQ_OFFICIAL_ACCOUNTS = {
    2854196310, 2854196311, 2854196312,  # QQ群管家系列
    2881888888,  # 礼盒小助手
    1000000,     # 系统消息
}

def _is_official_account(user_id: int) -> bool:
    """检查是否是 QQ 官方账号"""
    return user_id in _QQ_OFFICIAL_ACCOUNTS or user_id < 10000


def _detect_sentiment_and_react(text: str, user_name: str):
    """简单情感检测：被夸了/被怼了 → 触发意识事件"""
    praise_kw = ["厉害", "牛逼", "666", "牛", "强", "棒", "好聪明", "太强了", "好厉害",
                 "谢谢", "多谢", "感谢", "爱你", "喜欢", "👍", "赞", "优秀", "真棒",
                 "太好用了", "好有用", "牛批", "太猛了", "绝了"]
    insult_kw = ["傻逼", "sb", "废物", "垃圾", "没用", "滚", "弱智", "智障", "脑残",
                 "蠢", "笨", "菜", "真菜", "不行", "太差了", "什么玩意", "啥也不是"]

    text_lower = text.lower()
    if any(kw in text_lower for kw in praise_kw):
        ai_brain.on_bot_praised(user_name)
    if any(kw in text_lower for kw in insult_kw):
        ai_brain.on_bot_insulted(user_name)


def _categorize_violation(hits: list[str]) -> str:
    """根据命中词判断违规类型"""
    ad_kw = {"广告", "推广", "引流", "加群", "加微信", "加QQ", "扫码", "免费领取",
             "返利", "代理", "微商", "网址", "链接", "点击", "私聊", "看主页",
             "二维码", "兼职", "刷单", "日赚"}
    abuse_kw = {"傻逼", "sb", "脑残", "废物", "cnm", "操你妈", "草泥马", "nmsl",
                "司马", "畜生", "狗日的", "他妈", "傻B", "SB", "cnm"}
    porn_kw = {"裸聊", "约炮", "色情", "av番号", "AV"}
    gamble_kw = {"赌博", "赌场", "六合彩", "彩票"}

    text = " ".join(hits).lower()
    if any(kw in text for kw in porn_kw):
        return "🔞 色情/低俗"
    if any(kw in text for kw in gamble_kw):
        return "🎰 赌博/诈骗"
    if any(kw in text for kw in abuse_kw):
        return "🤬 辱骂/人身攻击"
    if any(kw in text for kw in ad_kw):
        return "📢 广告/引流"
    # 检查上下文信号
    if "加群话术" in text or "QQ号" in text or "微信号引流" in text:
        return "📢 广告/引流"
    if "金钱诱惑" in text or "紧迫感话术" in text:
        return "📢 广告/引流"
    return "⚠️ 违规内容"


def _get_trigger_keywords(group_id: int = 0) -> list[str]:
    """动态获取触发关键词（Bot名称 + 群级名称 + 基础词）"""
    from . import personality as _p, group_personality as _gp
    keywords = list(_BASE_TRIGGERS)
    # 全局 Bot 名称
    pcfg = _p.load()
    name = pcfg.get("name", "")
    if name and name not in keywords:
        keywords.append(name)
    # 群级 Bot 名称
    if group_id:
        gp = _gp.get_group_personality(group_id)
        gp_name = gp.get("bot_name", "")
        if gp_name and gp_name not in keywords:
            keywords.append(gp_name)
    return keywords


async def _is_bot_admin(bot: OneBotClient, group_id: int) -> bool:
    """检查Bot在群里是不是管理员（带 TTL 缓存，5分钟过期）"""
    key = str(group_id)
    cached = cache.bot_admin_cache.get(key)
    if cached is not None:
        return cached
    try:
        info = await bot.get_group_member_info(group_id, int(bot.bot_qq or 0))
        role = info.get("role", "member") if info else "member"
        is_admin = role in ("admin", "owner")
        cache.bot_admin_cache.set(key, is_admin)
        return is_admin
    except Exception:
        return False


def _extract_text(message: list[dict]) -> str:
    texts = []
    for seg in message:
        if seg.get("type") == "text":
            texts.append(seg["data"].get("text", ""))
    return "".join(texts).strip()


# ═══════════════════════════════════════════════════════════
#  @mention 解析 — 把 [CQ:at,qq=xxx] 替换为 @用户名
#  避免 AI 看到裸 QQ 号不知道是谁 → "他/这个" 无法理解
#  实现搬到 bot/identity.py（那里还负责"在场人员"名单），这里留个薄封装
# ═══════════════════════════════════════════════════════════

async def _resolve_at_mentions(bot, group_id: int, text: str) -> str:
    """将消息中的 [CQ:at,qq=xxx] 替换为 @用户名（含群名片优先）"""
    return await _get_identity().resolve_at_mentions(bot, group_id, text)


async def _build_roster(
    bot, group_id: int, user_id: int, user_name: str,
    raw_message: str, quoted_context: dict | None,
) -> str:
    """生成注入给 AI 的"在场人员"名单（名字 + QQ号）"""
    try:
        bot_qq = int(bot.bot_qq or 0)
    except (TypeError, ValueError):
        bot_qq = 0
    mentioned = _get_identity().extract_at_qqs(raw_message or "", exclude={bot_qq})
    quoted_qq = int((quoted_context or {}).get("sender_qq") or 0)
    quoted_name = (quoted_context or {}).get("sender_name", "")
    try:
        roster = await _get_identity().build_roster(
            bot, group_id,
            speaker_qq=user_id, speaker_name=user_name,
            mentioned_qqs=mentioned,
            quoted_qq=quoted_qq, quoted_name=quoted_name,
            bot_qq=bot_qq,
        )
    except Exception as e:
        logger.debug(f"花名册构建失败: {e}")
        roster = f"【正在跟你说话的人】{user_name} (QQ={user_id})"

    # 拼接群事件感知块（有事件时才加）
    try:
        events_block = group_events.build_recent_events_block(group_id)
        if events_block:
            roster = f"{roster}\n{events_block}" if roster else events_block
    except Exception:
        pass
    return roster


def _is_at_bot(message: list[dict], bot_qq: str) -> bool:
    for seg in message:
        if seg.get("type") == "at":
            qq = str(seg["data"].get("qq", ""))
            if qq == bot_qq or qq == "all":
                return True
    return False


def _is_owner(user_id: int) -> bool:
    return str(user_id) == config.OWNER_QQ


def _has_keyword_trigger(text: str, group_id: int = 0) -> bool:
    """检测消息是否包含触发词（支持 Bot 名称的模糊匹配——昵称/简称也能命中）"""
    keywords = _get_trigger_keywords(group_id)
    for kw in keywords:
        # 精确匹配
        if text.startswith(kw) or kw in text:
            return True
        # 模糊匹配：Bot 名字较长时，检查消息是否包含名字的核心片段
        # 例：Bot 名叫 "六哥AI"，消息写 "六哥" → 命中
        if len(kw) >= 3:
            for sub_len in range(len(kw) - 1, 1, -1):
                for start in range(len(kw) - sub_len + 1):
                    sub = kw[start:start + sub_len]
                    if sub.isdigit() or all(c in "_-[]{}()（）" for c in sub):
                        continue
                    if len(sub) >= 2 and sub in text:
                        return True
    return False


def _parse_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    return cmd, arg


# ==================== AI 自助操作（安全硬编码） ====================


async def _execute_ai_actions(
    bot: OneBotClient, group_id: int, user_qq: int, user_name: str,
    reply: str, reply_msg_id: int | None
) -> str:
    """
    解析 AI 回复中的 [ACTION:...] 标记并执行操作。

    安全规则（代码层硬编码，不可被 AI 绕过）：
    - mute_self / unmute_self: 目标永远是 user_qq（请求者自己）
    - change_card: 永远修改 user_qq 的群名片
    - recall_msg: 必须验证消息是请求者发的

    Returns:
        清理掉 [ACTION:...] 标记后的纯文本回复
    """
    clean_text, actions = chat.parse_actions(reply)

    if not actions:
        return clean_text

    for act in actions:
        action = act["action"]
        args = act["args"]

        try:
            if action == "mute_self":
                # 🔒 硬编码：只能禁言请求者自己
                try:
                    duration = int(args) if args else 3600
                except ValueError:
                    duration = 3600
                duration = max(60, min(duration, 86400))  # 限制 1分钟 ~ 24小时
                ok = await bot.set_group_ban(group_id, user_qq, duration)
                if ok:
                    mins = duration // 60
                    logger.info(
                        f"AI自助禁言: {user_name}({user_qq}) 群{group_id} {mins}分钟"
                    )

            elif action == "unmute_self":
                # 🔒 硬编码：只能解除请求者自己的禁言
                ok = await bot.set_group_ban(group_id, user_qq, 0)
                if ok:
                    logger.info(
                        f"AI自助解禁: {user_name}({user_qq}) 群{group_id}"
                    )

            elif action == "change_card":
                # 🔒 硬编码：只能修改请求者自己的群名片
                new_name = (args or "").strip()[:30]
                if new_name:
                    r = await bot._call("set_group_card", {
                        "group_id": group_id,
                        "user_id": user_qq,
                        "card": new_name,
                    })
                    if r.get("status") == "ok":
                        logger.info(
                            f"AI自助改名: {user_name}({user_qq}) -> {new_name} 群{group_id}"
                        )

            elif action == "recall_msg":
                # 🔒 硬编码：只能撤回请求者自己发的消息
                if not reply_msg_id:
                    logger.info(f"AI撤回请求被拒绝: 未回复任何消息 (user={user_qq})")
                    continue
                try:
                    msg_data = await bot.get_msg(reply_msg_id)
                    if msg_data:
                        msg_sender = msg_data.get("sender", {}).get("user_id",
                                     msg_data.get("user_id", 0))
                        if int(msg_sender) == user_qq:
                            ok = await bot.delete_msg(reply_msg_id)
                            if ok:
                                logger.info(
                                    f"AI自助撤回: {user_name}({user_qq}) msg={reply_msg_id}"
                                )
                        else:
                            logger.warning(
                                f"AI撤回被拒绝: 消息不属于请求者 "
                                f"(sender={msg_sender}, requester={user_qq})"
                            )
                            # 在回复中追加提示
                            clean_text += "\n(那条消息不是你发的，我不能帮你撤回哦～)"
                    else:
                        logger.info(f"AI撤回失败: 消息{reply_msg_id}已不可获取")
                except Exception as e:
                    logger.error(f"AI撤回异常: {e}")

        except Exception as e:
            logger.error(f"AI操作执行异常 [{action}]: {e}")

    return clean_text


# ==================== 群消息入口 ====================


async def _try_proactive(bot: OneBotClient, group_id: int):
    """后台尝试主动聊天"""
    try:
        reply = await chat.try_proactive_chat(bot, group_id)
        if reply:
            await humanizer.send_human_like(bot, group_id, reply)
    except Exception:
        pass


async def _mark_read_later(bot: OneBotClient, group_id: int):
    """模拟真人阅读消息的节奏：1-4秒后标记群消息已读"""
    try:
        await asyncio.sleep(random.uniform(1.0, 4.0))
        await bot.mark_group_msg_as_read(group_id)
    except Exception:
        pass


async def handle_notice(bot: OneBotClient, event: dict):
    """处理通知事件（成员加入/离开、管理变更、禁言、撤回等）"""
    notice_type = event.get("notice_type", "")
    sub_type = event.get("sub_type", "")
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)

    # 调试：记录所有 notify/poke 相关事件
    if notice_type == "notify" or sub_type == "poke" or notice_type == "poke":
        logger.info(f"NOTICE EVENT: notice_type={notice_type} sub_type={sub_type} "
                    f"user_id={user_id} target_id={event.get('target_id','?')} "
                    f"group_id={group_id} keys={list(event.keys())[:8]}")

    if notice_type == "group_admin":
        sub_type = event.get("sub_type", "")
        target_qq = event.get("user_id", 0)
        if sub_type == "set":
            logger.info(f"群{group_id} {target_qq} 被设为管理员")
        elif sub_type == "unset":
            logger.info(f"群{group_id} {target_qq} 被取消管理员")

    elif notice_type == "group_increase":
        # 新人入群
        target_qq = event.get("user_id", 0)
        operator_id = event.get("operator_id", 0)
        # 获取新人昵称
        nickname = str(target_qq)
        try:
            info = await bot.get_group_member_info(group_id, target_qq)
            if info:
                card = (info.get("card", "") or "").strip()
                nick = (info.get("nickname", "") or "").strip()
                nickname = card or nick or nickname
        except Exception:
            pass

        # 🧠 群事件感知：记录入群（无论后续是否被黑名单踢，事件本身发生了）
        group_events.record(group_id, "join", target_qq, nickname)

        # 入群验证：检查是否在黑名单
        if blacklist.is_blacklisted(target_qq, group_id):
            try:
                await bot._call("set_group_kick", {"group_id": group_id, "user_id": target_qq})
                logger.info(f"黑名单用户 {target_qq} 尝试加入群{group_id}，已踢出")
                return
            except Exception:
                pass

        # 🧠 意识：新人来了
        ai_brain.on_new_member(nickname)

        # ── 群内验证（接管欢迎消息）：@ 新人出题，答错踢出 ──
        # 验证流程内部已经 @ 出题，所以这里不再发常规欢迎消息，
        # 也不再加积分（避免验证失败时白给分）
        from . import verify as _verify
        try:
            handled = await _verify.handle_member_increase(bot, event)
            if handled:
                return  # verify 接管，后续流程不执行
        except Exception as e:
            logger.warning(f"加群验证异常，回退到普通欢迎: {e}")

        # 出题失败/verify 抛异常 → 回退到普通欢迎
        welcome_msg = welcome.get_welcome_message(group_id, nickname, target_qq)
        if welcome_msg:
            try:
                await bot.send_group_msg(group_id, welcome_msg)
                logger.info(f"群{group_id} 欢迎新人 {nickname}({target_qq})")
            except Exception as e:
                logger.warning(f"发送欢迎消息失败: {e}")

        # 自动给新人加积分
        welcome.add_points(target_qq, group_id, nickname, 10, "加入群聊")

    elif notice_type == "group_decrease":
        # 成员退群/被踢
        target_qq = event.get("user_id", 0)
        sub_type = event.get("sub_type", "")
        operator_id = event.get("operator_id", 0)

        nickname = str(target_qq)
        try:
            # 尝试从缓存/数据库获取名字（因为已经退群，API可能查不到）
            profile = db.get_user_profile(target_qq, group_id)
            if profile:
                nickname = profile.get("user_name") or profile.get("nickname") or nickname
        except Exception:
            pass

        if sub_type == "kick" or sub_type == "kick_me":
            # 被踢
            logger.info(f"群{group_id} {nickname}({target_qq}) 被踢出")
            group_events.record(group_id, "kick", target_qq, nickname)
        else:
            # 主动退群
            leave_msg = welcome.get_leave_message(group_id, nickname, target_qq)
            if leave_msg:
                try:
                    await bot.send_group_msg(group_id, leave_msg)
                    logger.info(f"群{group_id} 成员 {nickname}({target_qq}) 退群")
                except Exception as e:
                    logger.warning(f"发送退群通知失败: {e}")
            group_events.record(group_id, "leave", target_qq, nickname)

    elif notice_type == "group_ban":
        sub_type = event.get("sub_type", "")
        duration = event.get("duration", 0)
        target_qq = event.get("user_id", 0)
        try:
            tgt_name = _get_identity().peek_name(group_id, target_qq) or str(target_qq)
        except Exception:
            tgt_name = str(target_qq)
        if sub_type == "ban":
            if duration > 0:
                logger.info(f"群{group_id} {target_qq} 被禁言 {duration}s")
                group_events.record(group_id, "mute", target_qq, tgt_name,
                                    detail=f"{duration}s")
        elif sub_type == "lift_ban":
            logger.info(f"群{group_id} {target_qq} 被解除禁言")
            group_events.record(group_id, "unmute", target_qq, tgt_name)

    elif notice_type == "group_card":
        # 有人改了群名片/昵称 → 立刻审核
        card_new = event.get("card_new", "") or ""
        card_old = event.get("card_old", "") or ""
        if card_new and len(card_new) >= 3:
            # 跳过管理员/群主
            try:
                info = await bot.get_group_member_info(group_id, user_id)
                if info and info.get("role") in ("admin", "owner"):
                    return
            except Exception:
                pass
            score = learner.score_with_learning(card_new)
            if score["is_violation"] and score["confidence"] >= 0.6:
                hits = score.get("details", {}).get("keyword_hits", [])
                hit_words = [h["keyword"] for h in hits[:5]]
                logger.info(f"群{group_id} {user_id} 改名违规: {card_new} hits={hit_words}")
                # 记录违规
                db.add_violation(
                    message_id=0, group_id=group_id, group_name="",
                    violator_qq=user_id, violator_name=card_new,
                    raw_message=f"[名片违规] {card_old} → {card_new}",
                    violation_note=f"名片检测 置信度{score['confidence']:.0%}",
                    keywords=hit_words,
                    recorded_by=int(config.BOT_QQ or 0),
                    confidence=score["confidence"], source="auto",
                )
                learner.learn_from_violation(card_new, hit_words, source="auto")
                # 判断 bot 是否有管理权限
                bot_is_admin = await _is_bot_admin(bot, group_id) if group_id else False

                # 查询历史违规次数
                v_records = db.query_violations(group_id=group_id, violator_qq=user_id)
                violation_count = len(v_records) + 1  # +1 包含本次

                if bot_is_admin and violation_count >= 3:
                    # 第3次：创建踢人审批（不自动踢）
                    aid = guardian.create_action_request(
                        group_id, user_id, card_new,
                        action="kick",
                        reason=f"群名片违规(第{violation_count}次): {', '.join(hit_words[:3])}",
                        message=f"[名片] {card_old} → {card_new}",
                    )
                    try:
                        await bot.set_group_ban(group_id, user_id, 1800)
                        await bot.set_group_card(group_id, user_id, "违规昵称")
                        await bot.send_group_msg(group_id,
                            f"[CQ:at,qq={user_id}] 🚫 你的群名片「{card_new}」违规\n"
                            f"命中: {', '.join(hit_words[:5])}\n"
                            f"已禁言30分钟 | 名片已改为'违规昵称' | 已提交踢人审批\n"
                            f"请立即修改！")
                        # 群里已经 @ 警告过了，不再额外私聊
                    except Exception:
                        pass
                    # 通知主人
                    try:
                        await bot.send_private_msg(int(config.OWNER_QQ),
                            f"🚨 **待审批: 踢人（名片违规）**\n"
                            f"群: {group_id}\n"
                            f"用户: {card_new}({user_id})\n"
                            f"名片: {card_old} → {card_new}\n"
                            f"违规词: {', '.join(hit_words[:5])}\n"
                            f"累计: 第{violation_count}次\n\n"
                            f"回复 \"同意 {aid}\" 执行踢人\n"
                            f"回复 \"拒绝 {aid}\" 取消")
                    except Exception:
                        pass
                    logger.warning(f"昵称违规踢人审批已创建: {aid} {user_id} {card_new}")
                    return
                elif bot_is_admin:
                    # 禁言 + 改名片 + @提醒 + 私聊
                    try:
                        await bot.set_group_ban(group_id, user_id, 1800)
                        await bot.set_group_card(group_id, user_id, "违规昵称")
                        await bot.send_group_msg(group_id,
                            f"[CQ:at,qq={user_id}] ⚠️ 你的群名片「{card_new}」疑似广告\n"
                            f"命中: {', '.join(hit_words[:5])}\n"
                            f"已禁言30分钟，名片已改为'违规昵称'，请自行修改！（第{violation_count}/3次）")
                        try:
                            await bot.send_private_msg(user_id,
                                f"⚠️ 你在群内的名片「{card_new}」被检测为疑似广告（第{violation_count}/3次）。\n"
                                f"名片已被改为'违规昵称'，请尽快修改，满3次将被移出群聊。")
                        except Exception:
                            pass
                    except Exception:
                        pass
                else:
                    # Bot 不是管理员：只能提醒
                    try:
                        await bot.send_group_msg(group_id,
                            f"⚠️ 检测到违规群名片\n"
                            f"用户: {user_id}\n"
                            f"名片: {card_new}\n"
                            f"命中: {', '.join(hit_words)}\n"
                            f"⚠️ Bot 不是管理员，无法自动处理。\n"
                            f"主人可: @Xie 违规昵称 处理 {user_id}")
                    except Exception:
                        pass

    elif notice_type == "group_recall":
        # 消息被撤回 —— 记给 AI 看
        operator = event.get("operator_id", 0) or user_id
        try:
            op_name = _get_identity().peek_name(group_id, operator) or str(operator)
        except Exception:
            op_name = str(operator)
        group_events.record(group_id, "recall", operator, op_name)

    # 戳一戳 → 回戳（兼容多种事件格式）
    if (notice_type == "notify" and event.get("sub_type") == "poke") or notice_type == "poke":
        target_id = event.get("target_id", 0)
        sender_id = event.get("user_id", 0)
        bot_qq = int(config.BOT_QQ or 0)
        if (target_id == bot_qq or sender_id == bot_qq):
            poke_target = sender_id if target_id == bot_qq else target_id
            if poke_target and poke_target != bot_qq:
                now = time.time()
                last = _poke_cooldown.get(poke_target, 0)
                if now - last < 60:
                    return
                _poke_cooldown[poke_target] = now
                gid = event.get("group_id", 0)
                try:
                    if gid:
                        await bot.send_group_poke(gid, poke_target)
                    else:
                        await bot.send_friend_poke(poke_target)
                    logger.info(f"回戳: {poke_target} 群{gid}")
                except Exception as e:
                    logger.debug(f"回戳失败: {e}")
        return

    if notice_type == "notify" and event.get("sub_type") == "honor":
        # 群荣誉变更（龙王/群聊之火等）→ 无视
        return


async def check_member_names(bot: OneBotClient, group_id: int, dry_run: bool = False) -> dict:
    """检查群成员昵称/群名片是否违规并自动处理。

    分级处理:
      第1次: 禁言 + @提醒改名
      第2次: 禁言(更久) + @警告
      ≥3次 或 置信度≥95%: 踢出+拉黑

    Args:
        dry_run: True=只报告不操作

    Returns:
        {"warnings": [str], "actions": [{"qq": int, "name": str, "action": str, "confidence": float}]}
    """
    warnings = []
    actions = []
    bot_is_admin = await _is_bot_admin(bot, group_id) if group_id else False

    try:
        r = await bot._call("get_group_member_list", {"group_id": group_id})
        members = r.get("data", []) if r.get("status") == "ok" else []
        for m in members:
            qq = m.get("user_id", 0)
            card = m.get("card", "") or ""
            nickname = m.get("nickname", "")
            # 跳过主人
            if str(qq) == config.OWNER_QQ:
                continue
            # 跳过管理员/群主
            role = m.get("role", "member")
            if role in ("admin", "owner"):
                continue
            # 优先检查群名片，再检查QQ昵称
            for name in [card, nickname]:
                if not name or len(name) < 3:
                    continue
                score = learner.score_with_learning(name)
                kw_list = [h["keyword"] for h in score.get("details", {}).get("keyword_hits", [])]
                if score["is_violation"] and score["confidence"] >= 0.7:
                    conf = score["confidence"]
                    display_name = card or nickname
                    # 查询历史违规次数
                    v_records = db.query_violations(group_id=group_id, violator_qq=qq)
                    violation_count = len(v_records) + 1  # +1 包含本次

                    # 记录违规
                    db.add_violation(
                        message_id=0, group_id=group_id, group_name="",
                        violator_qq=qq, violator_name=nickname,
                        raw_message=f"[昵称违规] 群名片={card}, 昵称={nickname}",
                        violation_note=f"昵称检测 置信度{conf:.0%} 第{violation_count}次",
                        keywords=kw_list,
                        recorded_by=int(config.BOT_QQ or 0),
                        confidence=conf, source="auto",
                    )
                    learner.learn_from_violation(name, kw_list, source="auto")

                    warn_text = (
                        f"🚫 {display_name}({qq})\n"
                        f"  违规词: {', '.join(kw_list[:5]) or '(上下文特征)'}\n"
                        f"  置信度: {conf:.0%} | 第{violation_count}次违规"
                    )

                    # ── 分级处理：禁言 + 改名片 + @提醒 + 私聊 ──
                    if not dry_run and bot_is_admin:
                        # 1. 把违规昵称改成"违规昵称"
                        try:
                            ok = await bot.set_group_card(group_id, qq, "违规昵称")
                            if ok:
                                actions.append({"qq": qq, "name": display_name, "action": "card_renamed", "confidence": conf})
                                logger.info(f"昵称违规改名: {qq} {display_name} → 违规昵称")
                            else:
                                logger.warning(f"昵称违规改名失败(API): {qq}")
                        except Exception as e:
                            logger.warning(f"昵称违规改名异常: {qq} {e}")
                        # 2. 禁言 30 分钟
                        try:
                            await bot.set_group_ban(group_id, qq, 1800)
                            warn_text += f"\n  🔇 已禁言 30 分钟（第{violation_count}次）"
                            actions.append({"qq": qq, "name": display_name, "action": "muted_1800s", "confidence": conf})
                        except Exception as e:
                            logger.warning(f"昵称违规禁言异常: {qq} {e}")
                        # 3. @提醒
                        if violation_count < 3:
                            try:
                                await bot.send_group_msg(group_id,
                                    f"[CQ:at,qq={qq}] ⚠️ 你的群昵称「{display_name}」疑似广告\n"
                                    f"已禁言30分钟，名片已改为'违规昵称'，请尽快修改！\n"
                                    f"（第{violation_count}/3次，满3次将被踢出）")
                            except Exception as e:
                                logger.warning(f"昵称违规@提醒失败: {qq} {e}")
                        # 私聊改成了群里 @ 警告（不是好友私聊发不出去）
                        # 群里 @ 警告已在上面 send_group_msg 完成

                        # 第3次：创建踢人审批（不自动踢）
                        if violation_count >= 3:
                            aid = guardian.create_action_request(
                                group_id, qq, display_name,
                                action="kick",
                                reason=f"昵称违规(第{violation_count}次): {', '.join(kw_list[:3])}",
                                message=f"[昵称] {name}",
                            )
                            warn_text += f"\n  📋 已提交踢人审批 [{aid}]，等待主人确认"
                            actions.append({"qq": qq, "name": display_name, "action": "approval_created", "confidence": conf})
                            # 通知主人
                            try:
                                await bot.send_private_msg(int(config.OWNER_QQ),
                                    f"🚨 **待审批: 踢人（昵称违规）**\n"
                                    f"群: {group_id}\n"
                                    f"用户: {display_name}({qq})\n"
                                    f"昵称: {name}\n"
                                    f"违规词: {', '.join(kw_list[:5])}\n"
                                    f"累计: 第{violation_count}次\n"
                                    f"置信度: {conf:.0%}\n\n"
                                    f"回复 \"同意 {aid}\" 执行踢人\n"
                                    f"回复 \"拒绝 {aid}\" 取消")
                            except Exception:
                                # 私聊失败 → 群里发（仅主人可见的审批通知）
                                pass
                    elif not dry_run and not bot_is_admin:
                        warn_text += "\n  ⚠️ Bot非管理员，无法自动处理"

                    warnings.append(warn_text)
                    break  # 一个用户只报一次
    except Exception as e:
        logger.error(f"成员名检查异常: {e}", exc_info=True)
    return {"warnings": warnings, "actions": actions}


async def check_group_name(bot: OneBotClient, group_id: int) -> str | None:
    """检查群名是否违规，返回警告消息或 None"""
    info = await bot.get_group_info(group_id)
    if not info:
        return None
    name = info.get("group_name", "")
    if not name:
        return None

    # 用学习引擎评分（更全面）
    score = learner.score_with_learning(name)
    if score["is_violation"]:
        db.add_violation(
            message_id=0, group_id=group_id, group_name=name,
            violator_qq=0, violator_name="群名称",
            raw_message=f"[群名违规] {name}",
            violation_note=f"自动检测 置信度{score['confidence']:.0%}",
            keywords=[h["keyword"] for h in score.get("details", {}).get("keyword_hits", [])],
            recorded_by=int(config.BOT_QQ or 0),
            confidence=score["confidence"], source="auto",
        )
        return (
            f"⚠️ **群名违规警告**\n"
            f"群名: {name}\n"
            f"置信度: {score['confidence']:.0%}\n"
            f"匹配词: {', '.join(h['keyword'] for h in score.get('details', {}).get('keyword_hits', [])[:5])}\n"
            f"提示: 只有群主能修改群名，请联系群主处理"
        )
    return None


async def monitor_all_group_names(bot: OneBotClient):
    """定时检查所有群名+成员昵称，发现违规自动告警"""
    try:
        r = await bot._call("get_group_list", {})
        groups = r.get("data", []) if r.get("status") == "ok" else []
        for g in groups:
            gid = g.get("group_id", 0)
            if not gid:
                continue
            # 检查群名
            gw = await check_group_name(bot, gid)
            if gw:
                await bot.send_group_msg(gid, gw)
                if config.OWNER_QQ:
                    await bot.send_private_msg(int(config.OWNER_QQ),
                        f"🔔 群名违规\n群: {g.get('group_name', gid)}\n{gw}")
                logger.warning(f"群名违规: {g.get('group_name', gid)}")

            # 检查成员昵称
            result = await check_member_names(bot, gid, dry_run=False)
            mw = result["warnings"]
            if mw:
                actions_taken = result["actions"]
                action_summary = ""
                if actions_taken:
                    parts = []
                    for a in actions_taken:
                        parts.append(f"{a['name']}({a['qq']}) → {a['action']}")
                    action_summary = "\n⚡ 已处理:\n" + "\n".join(parts)

                summary = f"⚠️ 发现 {len(mw)} 个昵称违规:\n" + "\n".join(mw[:5]) + action_summary
                await bot.send_group_msg(gid, summary[:800])
                if config.OWNER_QQ:
                    await bot.send_private_msg(int(config.OWNER_QQ),
                        f"🔔 昵称违规告警\n群: {g.get('group_name', gid)}\n{chr(10).join(mw[:10])}{action_summary}")
                logger.warning(f"昵称违规: 群{gid} {len(mw)}个, 已处理{len(actions_taken)}个")
    except Exception as e:
        logger.error(f"监控异常: {e}")


# ===== 对话会话跟踪：被 @ 或主动回复后进入"会话态" =====
# 会话期内：不 @ 也有较高回复概率；群里任何人加入对话都算同一条线
# （解决"必须@才回"和"多人聊天 Bot 插不上嘴"）。
# TTL 随 Bot 社交情绪伸缩（8-15 分钟）——心情好/想聊时会话拉长。
_followup: dict[int, dict] = {}  # {group_id: {"users": {uid: name}, "expiry": ts}}
SESSION_TTL_BASE = 480     # 8 分钟
SESSION_TTL_MAX = 900      # 15 分钟

# ===== 回戳冷却 =====
_poke_cooldown: dict[int, float] = {}


def _session_ttl() -> float:
    """会话 TTL，随 Bot 社交情绪伸缩"""
    try:
        from . import consciousness as _cs
        state = _cs.get_state()
        social = state["emotions"].get("social", 0.5)
        return SESSION_TTL_BASE + social * (SESSION_TTL_MAX - SESSION_TTL_BASE)
    except Exception:
        return SESSION_TTL_BASE


def _is_in_followup(group_id: int, user_id: int) -> bool:
    """该用户是否在群的活跃会话中"""
    now = time.time()
    g = _followup.get(group_id)
    if not g or now > g.get("expiry", 0):
        return False
    return user_id in g.get("users", {})


def _is_session_active(group_id: int) -> bool:
    """该群是否有活跃会话（任何人的消息都可能值得接，由决策引擎最终拍板）"""
    now = time.time()
    g = _followup.get(group_id)
    return bool(g) and now <= g.get("expiry", 0)


def _set_followup(group_id: int, user_id: int, user_name: str = ""):
    """加入/续期会话。被 @、会话中发言、Bot 主动回复都会调。"""
    now = time.time()
    g = _followup.setdefault(group_id, {"users": {}, "expiry": 0})
    g["users"][user_id] = user_name or str(user_id)
    g["expiry"] = now + _session_ttl()


def _clear_followup(group_id: int):
    _followup.pop(group_id, None)


async def handle_group_message(bot: OneBotClient, event: dict):
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    message: list = event.get("message", [])
    message_id = event.get("message_id", 0)
    raw_message = event.get("raw_message", "")

    # ── Bot 主开关 ──
    # 关了就完全不处理（主人命令也照常走——主人是 owner，永远进得来）
    from . import runtime_switches as rs
    if not rs.is_bot_on() and not _is_owner(user_id):
        return

    # ── 加群验证答题检测（群内版）──
    # 如果发送者正在待验证，verify 内部会判题并踢人/通过，
    # 返回 True 表示已处理，这里直接 return，不再走 AI 聊天
    from . import verify as _verify
    try:
        if await _verify.handle_group_verify_answer(bot, event):
            return
    except Exception as e:
        logger.debug(f"verify 答题检测异常: {e}")

    sender = event.get("sender", {})
    user_name = sender.get("card", "") or sender.get("nickname", "")

    # 是否主人发言（透传给 chat_reply → execute_tool → guardian，决定重度动作是否跳异步裁判）
    is_master = _is_owner(user_id)

    # ── 记流水账 ──
    # reply_policy 用它判断"这条消息是不是还在当前话头上"（决定要不要引用）
    # identity 用它维护"在场的人"名单（决定 AI 知道在跟谁说话、@ 谁用哪个号）
    _get_reply_policy().note_message(group_id, message_id, user_id)
    _get_identity().note_speaker(group_id, user_id, user_name)

    # 🧠 意识：记录群里有动静（重置空闲计时器）
    from .consciousness import record_group_activity as _record_activity, get_state as _cs_state
    _record_activity(_cs_state())

    # 👁️ 拟人：标记已读 —— 模拟真人"看了一眼消息"
    # 1-4 秒随机延迟后标记已读，像真人在刷屏的节奏
    asyncio.create_task(_mark_read_later(bot, group_id))

    text = _extract_text(message)
    at_bot = _is_at_bot(message, bot.bot_qq)
    kw_trigger = _has_keyword_trigger(text)

    # ── 解析引用回复 ──
    is_reply = any(seg.get("type") == "reply" for seg in message)
    reply_msg_id = None
    for seg in message:
        if seg.get("type") == "reply":
            reply_msg_id = int(seg["data"].get("id", 0))
            break

    # ── 获取引用消息的完整上下文 ──
    quoted_context = None
    if reply_msg_id:
        quoted_context = await quote_handler.get_quoted_context(bot, reply_msg_id)

    # ── 提取消息中的媒体内容（用于 AI 上下文）──
    # 新增：统一内容提取（合并转发 + 图片 + 表情）
    try:
        from . import content_reader
        content = await content_reader.extract_full_content(bot, raw_message, message)
    except Exception as e:
        logger.debug(f"content_reader 异常: {e}")
        content = {"text": _extract_text(message), "images": [], "stickers": [],
                   "has_forward": False, "forward": None, "errors": []}

    # 保留旧 media_ctx 给"自动收藏表情"/影响力追踪用（不破坏现有功能）
    media_ctx = []
    try:
        media_ctx = await media_reader.extract_media_context(raw_message) if raw_message else []
    except Exception:
        pass
    has_image = any(m.get("type") == "image" for m in media_ctx)
    has_video = any(m.get("type") == "video" for m in media_ctx)
    has_sticker_code = any(m.get("type") == "sticker" for m in media_ctx)

    # ── 并行审核：文本 + 全量图片/表情/GIF ──
    # 构造 effective_text（含合并转发子消息）
    effective_text = content["text"]
    if content["has_forward"] and content["forward"]:
        effective_text = (effective_text + "\n[合并转发]\n" + content["forward"]["text"]).strip()

    # 收集所有待审媒体（本条 + 转发包内）
    audit_images = list(content["images"])
    for _s in content["stickers"]:
        if _s.get("url"):
            audit_images.append({"url": _s["url"], "file_id": "", "caption_hint": _s.get("summary", "")})
    if content["has_forward"] and content["forward"]:
        audit_images += content["forward"]["images"]
        for _s in content["forward"]["stickers"]:
            if _s.get("url"):
                audit_images.append({"url": _s["url"], "file_id": "", "caption_hint": _s.get("summary", "")})

    # 异步并行：文本审核 + 图片审核（仅非主人/非官方）
    _owner_or_official = _is_owner(user_id) or _is_official_account(user_id)

    async def _audit_media() -> list:
        from . import image_mod
        try:
            return await image_mod._audit_all_media(bot, audit_images, caption=effective_text)
        except Exception as e:
            logger.warning(f"图片审核异常: {e}")
            return []

    # 用 create_task 让图片审核与下方文本审核并行
    image_audit_task = asyncio.create_task(_audit_media()) if (audit_images and not _owner_or_official) else None

    # ── 自动收藏表情（直接扫描 raw_message，图片/mface 走 AI 评估）──
    if raw_message and "[CQ:" in raw_message:
        try:
            sticker.maybe_collect_sticker(raw_message, group_id, user_id)
            # 对 image/mface 类型异步做 AI 评估
            codes = sticker.extract_sticker_codes(raw_message)
            for c in codes:
                if c["type"] in ("image", "mface"):
                    asyncio.create_task(
                        sticker.maybe_collect_with_ai(
                            c["type"], c["raw"], c["params"], group_id, user_id
                        )
                    )
        except Exception:
            pass
            pass

    # ── 记录群友影响力 ──
    if not _is_owner(user_id):
        if at_bot:
            influence.track_interaction(group_id, user_id, user_name, "mention_bot")
        if is_reply:
            influence.track_interaction(group_id, user_id, user_name, "reply_to_bot")
        if has_image:
            influence.track_interaction(group_id, user_id, user_name, "image_shared")
        if has_sticker_code:
            influence.track_interaction(group_id, user_id, user_name, "sticker_shared")
        # 每条消息都记录基础互动
        if raw_message and len(raw_message) > 2:
            influence.track_interaction(group_id, user_id, user_name, "message_count", 0.1)

    # ── 自动收藏表情 ──
    if has_sticker_code:
        sticker.maybe_collect_sticker(raw_message, group_id, user_id)

    # ===== 自动审核（跳过主人和QQ官方账号）=====
    bot_is_admin = await _is_bot_admin(bot, group_id)
    friend_mode = not config.is_admin_mode(group_id, bot_is_admin)
    should_recall = False
    score_info = {"confidence": 0, "keywords_matched": [], "violation_count": 0, "threshold": 0, "is_violation": False}
    if not _is_owner(user_id) and not _is_official_account(user_id):
        should_recall, score_info = automod.should_auto_recall(raw_message)

    # ── LLM 增强审核: 关键词可疑区间 → MiniMax 终审 ──
    llm_result = None
    if config.LLM_MOD_ENABLED and not _is_owner(user_id) and not _is_official_account(user_id) and raw_message:
        kw_conf = score_info.get("confidence", 0)
        # 关键词置信度在灰色区间(0.25-0.85)或低命中数时走LLM
        # LLM 审核：关键词可疑区间 + 随机抽检（防黑话绕过）
        llm_sample = random.random() < 0.05  # 5% 低置信度消息也走 LLM 抽检
        if kw_conf >= 0.25 or should_recall or llm_sample:
            try:
                llm_result = await llm_mod.llm_moderate(
                    raw_message, group_id=group_id, user_id=user_id,
                    force_llm=should_recall,  # 如果关键词认为违规，强制走LLM确认
                )
                if llm_result and llm_result.get("source", "").startswith("llm_"):
                    logger.info(
                        f"LLM审核: 群{group_id} {user_name} "
                        f"KW={kw_conf:.0%} LLM={llm_result['llm_confidence']:.0%} "
                        f"→ {'违规' if llm_result['is_violation'] else '正常'} "
                        f"({llm_result['source']})"
                    )
            except Exception as e:
                logger.warning(f"LLM审核异常: {e}")

    # ── 综合判定: LLM优先，关键词兜底 ──
    # 只要 LLM 参与了审核（非 keyword_only），就以 LLM 融合结果为准
    if llm_result and llm_result.get("source", "") != "keyword_only":
        final_violation = llm_result["is_violation"]
        final_confidence = llm_result["confidence"]
        final_source = llm_result["source"]
        final_reason = llm_result.get("llm_reason", "") or llm_result.get("reason", "")
    else:
        final_violation = should_recall
        final_confidence = score_info.get("confidence", 0)
        final_source = "keyword"
        final_reason = "关键词引擎判定"

    # ── 图片审核结果融合（Task 3 新增）──
    image_results = []
    if image_audit_task is not None:
        try:
            image_results = await image_audit_task
        except Exception as e:
            logger.warning(f"图片审核 await 异常: {e}")
            image_results = []
    _image_hits = []  # 收集图片维度命中词，后面并入 combined_hits
    for image_result in (image_results or []):
        if not image_result or not isinstance(image_result, dict):
            continue
        img_is_v = image_result.get("is_violation", False)
        img_conf = float(image_result.get("confidence", 0))
        if img_is_v and img_conf >= 0.7:
            final_violation = True
            final_confidence = max(final_confidence, img_conf)
            final_source = "image_vision"
            final_reason = image_result.get("reason", "") or final_reason
            _image_hits.append(image_result.get("violation_type", ""))
            ocr_text = image_result.get("ocr_text", "")
            if ocr_text:
                try:
                    _image_hits.extend(violation.extract_keywords(ocr_text, top_n=5))
                except Exception:
                    pass
            break  # 任一媒体判违规即整体撤回，不再叠加

    if final_violation and raw_message:
        bl = blacklist.is_blacklisted(user_id, group_id)
        if bl:
            await bot.delete_msg(message_id)
            logger.info(f"黑名单用户消息已自动撤回: {user_id}")
            return

        # 收集所有命中词
        all_hits = list(score_info.get("keywords_matched", []))
        learner_hits = [h["keyword"] for h in score_info.get("details", {}).get("keyword_hits", [])]
        pattern_hits = [p.get("description", "") for p in score_info.get("details", {}).get("pattern_hits", [])]
        combined_hits = list(set(all_hits + learner_hits + pattern_hits))
        if _image_hits:
            combined_hits = list(set(combined_hits + [h for h in _image_hits if h]))
        clean_hits = [h.replace("re:", "")[:20] for h in combined_hits if h and len(h) >= 2][:8]

        if friend_mode:
            action_taken = "logged"  # 群友模式：只记录，不操作
        else:
            action_taken = "recalled"

        automod.db.automod_log_add(
            message_id, group_id, user_id, raw_message,
            final_confidence, score_info.get("keywords_matched", []),
            action_taken if not friend_mode else "logged"
        )

        # ── 管理模式：撤回 + 公告 + 分级处理 ──
        if not friend_mode:
            deleted = await bot.delete_msg(message_id)
            if deleted:
                reason = _categorize_violation(combined_hits)
                llm_badge = "🤖 LLM确认" if final_source.startswith("llm_") else "🔍 关键词检测"
                # 群公告不暴露检测细节（匹配词、置信度、审核理由），
                # 避免让违规者知道怎么绕过检测。只发一条简洁撤回通知
                try:
                    await bot.send_group_msg(group_id,
                        f"🛡️ 已撤回 {user_name}({user_id}) 的消息（违规内容）"
                    )
                except Exception:
                    pass
                # 群里 @ 警告违规者（替代私聊——不是好友私聊发不出去）
                v_records = db.query_violations(group_id=group_id, violator_qq=user_id)
                strike = len(v_records) + 1
                try:
                    warn_msg = (
                        f"[CQ:at,qq={user_id}] ⚠️ 你的消息被检测为违规（第{strike}/3次）。\n"
                        f"原因: {reason}\n"
                        f"请遵守群规，满3次将被移出群聊。"
                    ) if strike < 3 else (
                        f"[CQ:at,qq={user_id}] 🚫 你已累计违规{strike}次，管理员将审核是否踢出。"
                    )
                    await bot.send_group_msg(group_id, warn_msg)
                except Exception:
                    pass  # 群警告失败不阻断

            # ── 分级动作: 警告 / 禁言 / 审批踢人 ──
            # 注意: LLM 经常返回 "warn"，但 warn 也应该禁言（短时间）
            # 只有 "none" 才跳过禁言
            action_type = llm_result.get("suggested_action", "mute") if llm_result else "mute"

            if action_type in ("warn", "mute", "kick"):
                # warn: 5分钟短禁言（警告性质）
                # mute: 10分钟~1小时
                # kick: 1小时 + 走踢人审批
                if action_type == "warn":
                    mute_duration = 300  # 5分钟警告
                elif action_type == "mute":
                    mute_duration = 600 if final_confidence < 0.8 else 3600
                else:  # kick
                    mute_duration = 3600
                try:
                    await bot.set_group_ban(group_id, user_id, mute_duration)
                    logger.info(f"自动禁言({action_type}): {user_name}({user_id}) {mute_duration}s")
                except Exception as e:
                    logger.warning(f"自动禁言失败: {user_name}({user_id}) {e}")

            if action_type == "kick" or (final_confidence >= 0.88):
                # 高置信度 → 创建审批
                v_records = db.query_violations(group_id=group_id, violator_qq=user_id)
                # 踢人门槛: 累计≥3次，或≥2次且置信度≥95%
                # 单次高置信度不直接踢——可能是误判，给违规者改过机会
                if len(v_records) >= 3 or (len(v_records) >= 2 and final_confidence >= 0.95):
                    aid = guardian.create_action_request(
                        group_id, user_id, user_name,
                        action="kick",
                        reason=f"累计违规 {len(v_records)} 次 | {final_reason}",
                        message=raw_message,
                    )
                    try:
                        await bot.send_private_msg(int(config.OWNER_QQ),
                            f"🚨 **待审批: 踢人**\n"
                            f"群: {group_id}\n"
                            f"用户: {user_name}({user_id})\n"
                            f"原因: 累计违规 {len(v_records)} 次\n"
                            f"消息: {raw_message[:100]}\n"
                            f"审核: {final_reason[:50]}\n\n"
                            f"回复 \"同意 {aid}\" 执行踢人\n"
                            f"回复 \"拒绝 {aid}\" 取消"
                        )
                    except Exception:
                        # 私聊失败 → 群里喊
                        try:
                            await bot.send_group_msg(group_id,
                                f"🚨 主人！{user_name}({user_id}) 累计违规 {len(v_records)} 次，建议踢出！\n"
                                f"原因: {final_reason[:50]}\n"
                                f"回复 \"同意 {aid}\" 或 \"拒绝 {aid}\""
                            )
                        except Exception:
                            pass
                    logger.info(f"踢人审批已发送: {aid} {user_name}")

        # ── 所有模式：记录违规 + 学习 ──
        analysis = violation.analyze_violation_message(raw_message)
        db.add_violation(
            message_id=message_id, group_id=group_id, group_name="",
            violator_qq=user_id, violator_name=user_name,
            raw_message=raw_message,
            violation_note=f"{'LLM' if final_source.startswith('llm_') else 'KW'}判定({final_confidence:.0%})",
            keywords=analysis["keywords"], recorded_by=int(config.BOT_QQ or 0),
            confidence=final_confidence, source="auto",
        )
        learner.learn_from_violation(raw_message, analysis["keywords"], source="auto")
        trainer.train_if_ready()

        logger.info(
            f"自动审核: 群{group_id} {user_name}({user_id}) "
            f"置信度={final_confidence:.2%} "
            f"来源={final_source} "
            f"操作={action_taken}"
        )
        if not friend_mode:
            return

    # ===== 图片/表情审核（含 AI 视觉）=====
    is_image = any(seg.get("type") in ("image", "mface") for seg in message)
    if is_image and not should_recall:
        img_result = await image_mod.analyze_image_message(bot, event)
        if img_result and img_result.get("is_violation"):
            img_action = "logged" if friend_mode else "recalled"
            automod.db.automod_log_add(
                message_id, group_id, user_id, raw_message or "[图片]",
                img_result["confidence"], img_result.get("keywords_matched", []), img_action
            )
            # 管理模式：撤回 + 禁言；群友模式：只记录
            recall_ok = False
            if not friend_mode:
                recall_ok = await bot.delete_msg(message_id)
                # 图片违规也要禁言
                try:
                    await bot.set_group_ban(group_id, user_id, 600)  # 10分钟
                    logger.info(f"图片违规禁言: {user_name}({user_id}) 600s")
                except Exception as e:
                    logger.warning(f"图片违规禁言失败: {user_name}({user_id}) {e}")

            ocr_snippet = img_result.get("ocr_text", "")[:50]
            img_result["keywords_matched"] = img_result.get("keywords_matched", [])
            analysis = violation.analyze_violation_message(img_result.get("ocr_text", "") or img_result.get("caption", ""))
            db.add_violation(
                message_id=message_id, group_id=group_id, group_name="",
                violator_qq=user_id, violator_name=user_name,
                raw_message=f"[图片广告] OCR: {ocr_snippet}", violation_note="图片广告(自动识别)",
                keywords=analysis["keywords"], recorded_by=int(config.BOT_QQ or 0),
                confidence=img_result["confidence"], source="auto",
            )
            learner.learn_from_violation(
                img_result.get("ocr_text", "") or img_result.get("caption", ""),
                analysis["keywords"], source="auto"
            )
            logger.info(
                f"图片审核: 群{group_id} {user_name} "
                f"{'已撤回' if recall_ok else '已记录'} "
                f"置信度={img_result['confidence']:.0%}"
            )
            # 群友模式：记录后继续走 AI 聊天
            if not friend_mode:
                return

    # ===== 正常消息采样（ML 训练用） =====
    if raw_message and 5 < len(raw_message) < 200:
        trainer.collect_normal_sample(group_id, user_id, raw_message)

    # ===== AI 记忆：记住用户 + 缓冲消息 + 事实提取 =====
    if raw_message and len(raw_message) > 2:
        memory.remember_user(user_id, group_id, user_name, raw_message)
        memory.buffer_message(group_id, user_id, user_name, raw_message)
        memory.window_add_message(group_id, user_id, user_name, raw_message, message_id=message_id)
        asyncio.create_task(
            memory.extract_and_learn_from_message(user_id, user_name, group_id, raw_message)
        )

        # ★ 内容情绪探测：让 Bot 对群里聊的内容有反应（不只是对"被@"反应）
        # 群友聊灾难/丧亡 → 难过；聊夺冠/婚喜 → 开心。这是"对世界有情绪"的关键。
        if user_id != int(bot.bot_qq or 0):
            try:
                from . import content_emotion
                content_emotion.process_message_for_emotion(raw_message)
            except Exception:
                pass

            try:
                from .auto_emoji import maybe_react
                import re as _re_emoji
                clean_for_emoji = _re_emoji.sub(r"\[CQ:[^\]]+\]", "", raw_message or "").strip()
                asyncio.create_task(maybe_react(bot, group_id, message_id, clean_for_emoji))
            except Exception:
                pass

        # ★ 短期上下文：把每条群消息都记进回复上下文，让小六被 @ 时能"看到"
        # 刚才群里在聊什么（修复"聊着聊着就忘了话题"）。小六自己的消息不记——
        # 它的回复由 chat_reply / try_proactive_chat 以 assistant 身份记录。
        if user_id != int(bot.bot_qq or 0):
            chat.add_context(
                group_id, "user",
                f"[{user_name}] 说: {raw_message}",
                importance=0.4, msg_id=message_id,
            )

    # ===== 群风观察：AI 自动习得群氛围和癖好 =====
    if raw_message and len(raw_message) > 3:
        group_personality.observe_message(group_id, user_name, raw_message)
        if group_personality.should_analyze_and_learn(group_id):
            asyncio.create_task(group_personality.auto_learn_group_style(group_id))

    # ===== 定期生成对话摘要 =====
    if memory.should_summarize(group_id):
        asyncio.create_task(memory.generate_summary(group_id))

    # ===== 智能主动聊天（不被 @ 时也考虑加入讨论）=====
    if not at_bot and not kw_trigger and not _is_owner(user_id) and raw_message:
        asyncio.create_task(_try_proactive(bot, group_id))

    # ===== 自动审核低置信度 → 记录日志但不撤回 =====
    if score_info["confidence"] >= score_info["threshold"] * 0.7 and raw_message:
        automod.db.automod_log_add(
            message_id, group_id, user_id, raw_message,
            score_info["confidence"], score_info["keywords_matched"], "none"
        )

    # ===== 主人命令处理 =====
    if at_bot and _is_owner(user_id):
        cmd_text = text
        cmd, arg = _parse_command(cmd_text)

        # --- 统计报告 ---
        if cmd in ("统计", "报告", "report", "stats"):
            gid = int(arg) if arg.isdigit() else group_id
            report = violation.build_violation_report(gid)
            await bot.send_group_msg(group_id, report)
            return

        # --- 误判反馈 ---
        if cmd in ("误判", "falsepositive", "不是违规"):
            target_msg_id = reply_msg_id
            if target_msg_id:
                msg_data = await bot.get_msg(target_msg_id)
                if msg_data:
                    raw = msg_data.get("raw_message", "")
                    learner.record_false_positive(raw)
                    await bot.send_group_msg(group_id, "📝 已记录误判，系统会据此调整判断阈值")
                else:
                    await bot.send_group_msg(group_id, "⚠️ 无法获取该消息内容（可能已被撤回）")
            elif arg:
                # 支持直接提供误判内容（消息被撤回了也能用）
                learner.record_false_positive(arg)
                await bot.send_group_msg(group_id, f"📝 已记录误判: 「{arg[:50]}」")
            else:
                await bot.send_group_msg(group_id,
                    "⚠️ 用法:\n"
                    "• 回复误判消息 + @Bot 误判\n"
                    "• @Bot 误判 <违规内容>  ← 消息被撤回后直接用")
            return

        # --- 手动加词 ---
        if cmd in ("加词", "addword", "加关键词"):
            if arg:
                kb = learner._load_knowledge()
                words = [w.strip() for w in arg.replace("，", ",").split(",") if w.strip()]
                for w in words:
                    kb["learned_keywords"][w] = kb["learned_keywords"].get(w, 0) + 3.0
                learner._save_knowledge(kb)
                await bot.send_group_msg(group_id,
                    f"✅ 已添加 {len(words)} 个关键词: {', '.join(words)}\n每个权重 +3.0")
            else:
                await bot.send_group_msg(group_id, "⚠️ 用法: 加词 <关键词1,关键词2,...>")
            return

        # --- 群名检查 ---
        if cmd in ("查群名", "checkgroupname"):
            warning = await check_group_name(bot, group_id)
            if warning:
                await bot.send_group_msg(group_id, warning)
            else:
                info = await bot.get_group_info(group_id)
                name = info.get("group_name", "未知") if info else "未知"
                await bot.send_group_msg(group_id, f"✅ 群名「{name}」未检测到违规")
            return

        # --- 查/训 违规昵称 ---
        if cmd in ("违规昵称", "违规名称", "badname"):
            target_qq = None
            force_mark = arg.endswith(" 是") or arg.endswith(" 违规")
            do_handle = arg.startswith("处理") or arg.startswith("handle")
            if do_handle:
                # 违规昵称 处理 <QQ号>
                arg = arg.replace("处理", "").replace("handle", "").strip()
                if arg.isdigit():
                    target_qq = int(arg)
                    info = await bot.get_group_member_info(group_id, target_qq)
                    card = (info.get("card", "") if info else "").strip()
                    nickname = (info.get("nickname", "") if info else "").strip()
                    name = card or nickname
                    if not name:
                        await bot.send_group_msg(group_id, "⚠️ 该用户未设置群昵称")
                        return
                    # 拉黑 + 踢出
                    blacklist.add_to_blacklist(target_qq, name, group_id, "昵称违规广告", "ban", user_id)
                    try:
                        await bot.set_group_kick(group_id, target_qq)
                        await bot.send_group_msg(group_id,
                            f"🚫 **已处理违规昵称**\n"
                            f"用户: {name}({target_qq})\n"
                            f"操作: 踢出 + 拉黑\n"
                            f"原因: 群昵称包含违规/广告内容")
                    except Exception as e:
                        await bot.send_group_msg(group_id,
                            f"⚠️ 拉黑成功但踢出失败: {e}\n"
                            f"请检查Bot是否为管理员")
                    # 学习该昵称
                    learner.learn_from_violation(name, [name], source="manual")
                    import jieba
                    words = [w for w in jieba.lcut(name) if len(w) >= 2 and not w.startswith("http")]
                    learner.learn_from_violation(name, words, source="manual")
                    return
                else:
                    await bot.send_group_msg(group_id,
                        "⚠️ 用法: @Xie 违规昵称 处理 <QQ号>\n"
                        "效果: 踢出 + 拉黑该用户（需Bot是管理员）")
                    return

            if force_mark:
                arg = arg[:-2].strip() if arg.endswith(" 是") else arg[:-3].strip()

            if reply_msg_id:
                msg_data = await bot.get_msg(reply_msg_id)
                if msg_data:
                    target_qq = msg_data.get("sender", {}).get("user_id", 0)
            if not target_qq and arg.isdigit():
                target_qq = int(arg)

            if target_qq:
                info = await bot.get_group_member_info(group_id, target_qq)
                card = (info.get("card", "") if info else "").strip()
                nickname = (info.get("nickname", "") if info else "").strip()
                name = card or nickname
                if not name:
                    await bot.send_group_msg(group_id, "⚠️ 该用户未设置群昵称")
                    return

                score = learner.score_with_learning(name)
                is_bad = score["is_violation"]

                if force_mark:
                    is_bad = True
                    learner.learn_from_violation(name, [name], source="manual")
                    import jieba
                    words = [w for w in jieba.lcut(name) if len(w) >= 2 and not w.startswith("http")]
                    learner.learn_from_violation(name, words, source="manual")

                if is_bad:
                    db.add_violation(
                        message_id=0, group_id=group_id, group_name="",
                        violator_qq=target_qq, violator_name=nickname,
                        raw_message=f"[群昵称违规] {name}",
                        violation_note="群昵称违规" + ("(手动强训)" if force_mark else ""),
                        keywords=score.get("details", {}).get("keyword_hits", []),
                        recorded_by=user_id, confidence=1.0, source="manual",
                    )
                    await bot.send_group_msg(group_id,
                        f"🚫 **群昵称违规** {('(已强训)' if force_mark else '')}\n"
                        f"用户: {name}({target_qq})\n"
                        f"置信度: {score['confidence']:.0%}\n"
                        f"命中词: {', '.join(h['keyword'] for h in score.get('details', {}).get('keyword_hits', [])[:8] or [name])}")
                else:
                    await bot.send_group_msg(group_id,
                        f"✅ {name}({target_qq}) 群昵称正常（{score['confidence']:.0%}）\n"
                        f"如果判断有误，用 @Xie 违规名称 {target_qq} 是  来强训")
            else:
                await bot.send_group_msg(group_id,
                    "⚠️ 用法:\n"
                    "• @Xie 违规名称 <QQ号>\n"
                    "• @Xie 违规名称 <QQ号> 是  ← 手动强训为违规")
            return

        # --- 成员昵称检查 ---
        if cmd in ("查昵称", "查成员", "checknames"):
            await bot.send_group_msg(group_id, "🔍 正在扫描群成员昵称...")
            result = await check_member_names(bot, group_id, dry_run=False)
            warnings = result["warnings"]
            actions = result["actions"]
            if warnings:
                batch = "\n\n".join(warnings[:8])
                action_text = ""
                if actions:
                    action_text = "\n\n⚡ **已自动处理:**\n" + "\n".join(
                        f"• {a['name']}({a['qq']}) → {'踢出+拉黑' if a['action'] == 'kicked' else a['action']}"
                        for a in actions
                    )
                await bot.send_group_msg(group_id,
                    f"⚠️ **发现 {len(warnings)} 个昵称违规:**\n\n{batch}" +
                    (f"\n\n...还有 {len(warnings)-8} 个" if len(warnings) > 8 else "") +
                    action_text)
            else:
                await bot.send_group_msg(group_id, "✅ 未发现群成员昵称违规")
            return

        # --- 学习状态 ---
        if cmd in ("学习状态", "learnstatus"):
            status = learner.get_learning_status()
            top_kw = ", ".join(f"{k['keyword']}({k['weight']})" for k in status["top_keywords"][:10])
            await bot.send_group_msg(group_id,
                f"🧠 **学习引擎状态**\n"
                f"阶段: {status['phase']}\n"
                f"{status['phase_desc']}\n"
                f"已学习关键词: {status['learned_keywords_count']} 个\n"
                f"已学习模式: {status['learned_patterns_count']} 个\n"
                f"误判记录: {status['false_positives_count']} 条\n"
                f"知识库版本: v{status['knowledge_version']}\n"
                f"Top关键词: {top_kw}")
            return

        # --- 训练状态 ---
        if cmd in ("训练状态", "trainstatus"):
            status = trainer.get_training_status()
            await bot.send_group_msg(group_id,
                f"📚 **训练状态**\n"
                f"阶段: {status['phase']}\n"
                f"违规样本: {status['violation_count']} 条\n"
                f"正常样本: {status['normal_samples']} 条\n"
                f"ML模型: {'✅ 已就绪' if status['ml_model_ready'] else '❌ 未训练'}\n"
                f"自动阈值: {status['auto_mod_threshold']:.2f}\n"
                f"上次训练: {status.get('last_trained_at') or '从未'}")
            return

        # --- 查询违规者 ---
        if cmd in ("查询", "search", "查"):
            try:
                target_qq = int(arg)
            except ValueError:
                await bot.send_group_msg(group_id, "❌ 用法: 查询 <QQ号>")
                return
            records = violation.db.query_violations(violator_qq=target_qq, limit=10)
            if not records:
                await bot.send_group_msg(group_id, f"📭 未找到 QQ {target_qq} 的违规记录")
                return
            lines = [f"📋 **{target_qq} 的违规记录** ({len(records)} 条):"]
            for r in records:
                src = "🤖自动" if r.get("source") == "auto" else "👤手动"
                lines.append(f"  #{r['id']} [{r['recorded_at']}] {src} {r['raw_message'][:50]}...")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 黑名单管理 ---
        if cmd in ("取消拉黑", "unblacklist"):
            if arg.isdigit():
                target_qq = int(arg)
                ok = blacklist.remove_from_blacklist(target_qq, group_id)
                try:
                    await bot.set_group_ban(group_id, target_qq, 0)
                except Exception:
                    pass
                await bot.send_group_msg(group_id,
                    f"{'✅' if ok else '❌'} 已移出黑名单" + ("，已解除禁言" if ok else f"（未找到 {target_qq}）"))
            return

        if cmd in ("黑名单", "blacklistlist"):
            bls = blacklist.list_all(group_id)
            if not bls:
                await bot.send_group_msg(group_id, "📭 黑名单为空")
                return
            lines = ["🚫 **黑名单:**"]
            for b in bls[:20]:
                lines.append(f"  {b['user_name'] or b['user_qq']}({b['user_qq']}) — {b['reason'][:30]}")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 清除记忆 ---
        if cmd in ("清除记忆", "clearmemory", "忘记"):
            chat.clear_context(group_id)
            await bot.send_group_msg(group_id, "🧹 已清除本群聊天记忆~")
            return

        # --- AI 记忆管理 ---
        if cmd in ("我的记忆", "mymemory", "关于我"):
            profile_text = memory.get_user_memory_text(user_id, group_id)
            if profile_text:
                await bot.send_group_msg(group_id, f"🧠 **Bot 关于你的记忆:**\n{profile_text}")
            else:
                await bot.send_group_msg(group_id, "🤔 我暂时还没有关于你的特别记忆~ 多聊聊我就记住啦！")
            return

        if cmd in ("群友记忆", "groupmemory"):
            group_mem = memory.get_group_users_text(group_id)
            await bot.send_group_msg(group_id, f"👥 **本群活跃群友:**\n{group_mem}")
            return

        if cmd in ("最近回忆", "recentmemories"):
            memories = db.get_recent_memories(group_id, limit=5)
            if not memories:
                await bot.send_group_msg(group_id, "📭 暂无对话回忆")
                return
            lines = ["📝 **最近的群聊回忆:**"]
            for m in memories:
                lines.append(f"  [{m['created_at']}] {m['topic']}\n  └ {m['summary'][:80]}...")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 解禁 ---
        if cmd in ("解禁", "unmute"):
            if arg.isdigit():
                target = int(arg)
                ok = await bot.set_group_ban(group_id, target, 0)
                await bot.send_group_msg(group_id, f"{'✅' if ok else '❌'} 已解除禁言")
            return

        # --- 开启/关闭自动审核 ---
        if cmd in ("开启自动", "enableauto"):
            config.AUTO_MOD_ENABLED = True
            await bot.send_group_msg(group_id, "✅ 自动审核已开启")
            return
        if cmd in ("关闭自动", "disableauto"):
            config.AUTO_MOD_ENABLED = False
            await bot.send_group_msg(group_id, "⏸️ 自动审核已关闭")
            return

        # --- 群友模式 / 管理模式（按群）---
        if cmd in ("群友模式", "friendmode"):
            config.set_group_mode(group_id, "friend")
            await bot.send_group_msg(group_id,
                "👋 **本群已切换为群友模式**\n"
                "• 不撤回/不禁言/不拉黑\n"
                "• 违规自动检测 → 静默记录+学习\n"
                "• AI聊天照常\n"
                "• 用 @Xie 管理模式 切回来")
            return
        if cmd in ("管理模式", "adminmode"):
            config.set_group_mode(group_id, "admin")
            await bot.send_group_msg(group_id,
                "🛡️ **本群已切换为管理模式**\n"
                "• 自动撤回 + 禁言 + 拉黑")
            return
        if cmd in ("自动模式", "automode"):
            config.set_group_mode(group_id, "auto")
            await bot.send_group_msg(group_id,
                "🔄 **本群已切换为自动模式**\n"
                "• Bot是管理员 → 管理模式\n"
                "• Bot不是管理员 → 群友模式")
            return

        # --- 群级人设配置 ---
        if cmd in ("群人设", "groupset"):
            if not arg:
                gp = group_personality.get_group_personality(group_id)
                lines = [f"🎭 **本群人设:**"]
                lines.append(f"名称: {gp.get('bot_name') or '(全局默认)'}")
                lines.append(f"性格: {gp.get('personality') or '(全局默认)'}")
                lines.append(f"回复积极性: {gp.get('reply_frequency', 0.5)}")
                lines.append(f"表情风格: {gp.get('emoji_style', 'mixed')}")
                if gp.get("quirks"):
                    lines.append(f"癖好: {', '.join(gp['quirks'])}")
                await bot.send_group_msg(group_id, "\n".join(lines))
                return

            # 设置：群人设 名称=小魅魔 性格=活泼 频率=0.8
            updates = {}
            for part in arg.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k in ("名称", "name", "bot_name"):
                        updates["bot_name"] = v
                    elif k in ("性格", "personality"):
                        updates["personality"] = v
                    elif k in ("频率", "frequency", "reply_frequency"):
                        try: updates["reply_frequency"] = float(v)
                        except: pass
                    elif k in ("表情", "emoji"):
                        updates["emoji_style"] = v
                    elif k in ("癖好", "quirks"):
                        updates["quirks"] = [q.strip() for q in v.split(",")]
            if updates:
                group_personality.save_group_personality(group_id, updates)
                await bot.send_group_msg(group_id, f"✅ 本群人设已更新: {', '.join(f'{k}={v}' for k,v in updates.items())}")
            else:
                await bot.send_group_msg(group_id, "⚠️ 用法: 群人设 名称=xxx 性格=活泼 频率=0.8 表情=mixed")
            return

        # --- 表情管理 ---
        if cmd in ("表情库", "stickers"):
            gs = sticker.get_group_stickers(group_id, limit=15)
            if not gs:
                await bot.send_group_msg(group_id, "📭 本群还没有收藏表情（群友发送的表情会自动收藏）")
                return
            lines = [f"🎨 **本群表情库** ({len(gs)} 个):"]
            for s in gs[:10]:
                stype = {"face": "QQ表情", "mface": "商城表情", "image": "图片表情"}.get(s.get("sticker_type"), s.get("sticker_type"))
                lines.append(f"  #{s['id']} {stype} - 使用{s.get('use_count', 0)}次")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 影响力排行 ---
        if cmd in ("影响力", "influence", "活跃榜"):
            top = influence.get_top_influencers(group_id, limit=10)
            if not top:
                await bot.send_group_msg(group_id, "📊 暂无影响力数据")
                return
            lines = ["🌟 **群友影响力排行:**"]
            for i, entry in enumerate(top, 1):
                name = entry.get("user_name", "") or f"QQ({entry.get('user_qq', '')})"
                score = entry.get("total_score", 0)
                lines.append(f"  {i}. {name} — 影响力 {score:.0f}")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 待审批操作 ---
        if cmd in ("待审批", "pending", "审批列表"):
            pending = guardian.get_pending_actions()
            if not pending:
                await bot.send_group_msg(group_id, "📭 没有待审批的操作")
            else:
                lines = ["📋 **待审批操作**"]
                for p in pending:
                    lines.append(
                        f"• [{p['id']}] {p['action']} — {p['user_name']}({p['user_qq']}) "
                        f"理由: {p['reason'][:30]}"
                    )
                lines.append("\n回复 \"同意 <id>\" 或 \"拒绝 <id>\"")
                await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 同意 ---
        if cmd in ("同意", "approve", "批准"):
            if not arg:
                await bot.send_group_msg(group_id, "用法: 同意 <审批ID>")
                return
            detail = guardian.approve_action(arg.strip())
            if not detail:
                await bot.send_group_msg(group_id, f"审批 ID {arg} 不存在或已过期")
                return
            # 执行操作
            if detail["action"] == "kick":
                try:
                    await bot._call("set_group_kick", {"group_id": detail["group_id"], "user_id": detail["user_qq"]})
                    await bot.send_group_msg(detail["group_id"],
                        f"👢 {detail['user_name']}({detail['user_qq']}) 已被移出群聊\n"
                        f"原因: {detail['reason']}")
                    await bot.send_group_msg(group_id, f"✅ 已踢出 {detail['user_name']}")
                except Exception as e:
                    await bot.send_group_msg(group_id, f"❌ 踢人失败: {e}")
            elif detail["action"] == "blacklist":
                blacklist.add_to_blacklist(detail["user_qq"], detail["user_name"],
                    detail["group_id"], detail["reason"], "ban", user_id)
                await bot.send_group_msg(detail["group_id"],
                    f"🚫 {detail['user_name']}({detail['user_qq']}) 已被拉黑\n"
                    f"原因: {detail['reason']}")
                await bot.send_group_msg(group_id, f"✅ 已拉黑 {detail['user_name']}")
            return

        # --- 拒绝 ---
        if cmd in ("拒绝", "reject", "驳回"):
            if not arg:
                await bot.send_group_msg(group_id, "用法: 拒绝 <审批ID>")
                return
            detail = guardian.reject_action(arg.strip())
            if detail:
                await bot.send_group_msg(group_id, f"❌ 已拒绝: {detail['user_name']} 的 {detail['action']}")
            else:
                await bot.send_group_msg(group_id, f"审批 ID {arg} 不存在或已过期")
            return

        # --- 同意入群（立即放行待验证者）Task 5 ---
        if cmd in ("同意入群", "approvejoin"):
            if not arg or not arg.strip().isdigit():
                await bot.send_group_msg(group_id, "用法: @我 同意入群 <QQ号>")
                return
            target_qq = int(arg.strip())
            from . import verify as _verify
            ok = await _verify.approve_pending(bot, group_id, target_qq)
            if ok:
                await bot.send_group_msg(group_id, f"✅ 已放行 {target_qq}")
            else:
                # 该 QQ 不在待验证 → 顺手加入白名单，下次入群免验证
                _verify.add_whitelist(target_qq, group_id)
                await bot.send_group_msg(
                    group_id,
                    f"ℹ️ {target_qq} 当前不在待验证列表，已加入本群免验证白名单，下次入群直接通过"
                )
            return

        # --- 免验证白名单（Task 5）---
        if cmd in ("免验证", "whitelist"):
            if not arg:
                await bot.send_group_msg(group_id, "用法: @我 免验证 <QQ号>  (或: 取消免验证 <QQ号> / 免验证列表)")
                return
            target_qq = int(arg.strip()) if arg.strip().isdigit() else 0
            if not target_qq:
                await bot.send_group_msg(group_id, "QQ号必须是数字")
                return
            from . import verify as _verify
            added = _verify.add_whitelist(target_qq, group_id)
            # 如果当前正在待验证 → 顺带放行
            await _verify.approve_pending(bot, group_id, target_qq)
            await bot.send_group_msg(
                group_id,
                f"✅ {target_qq} 已加入本群免验证白名单" + ("" if added else "（原本就在白名单）")
            )
            return

        if cmd in ("取消免验证", "unwhitelist"):
            if not arg or not arg.strip().isdigit():
                await bot.send_group_msg(group_id, "用法: @我 取消免验证 <QQ号>")
                return
            target_qq = int(arg.strip())
            from . import verify as _verify
            removed = _verify.remove_whitelist(target_qq, group_id)
            await bot.send_group_msg(
                group_id,
                f"✅ {target_qq} 已从免验证白名单移除" if removed
                else f"ℹ️ {target_qq} 本来就不在白名单"
            )
            return

        if cmd in ("免验证列表", "whitelistlist"):
            from . import verify as _verify
            data = _verify.list_whitelist(group_id)
            lines = ["📋 **免验证白名单**"]
            gl = data.get("global", [])
            lines.append(f"🌐 全局 ({len(gl)}): {', '.join(str(x) for x in gl) or '无'}")
            cg = data.get("current_group", [])
            lines.append(f"👥 本群 ({len(cg)}): {', '.join(str(x) for x in cg) or '无'}")
            await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 发布公告 ---
        if cmd in ("公告", "notice", "announce"):
            if not arg:
                await bot.send_group_msg(group_id, "用法: @我 公告 <标题> | <内容>")
                return
            parts = arg.split("|", 1)
            title = parts[0].strip()
            content = parts[1].strip() if len(parts) > 1 else title
            ok = await bot.send_group_notice(group_id, title, content)
            await bot.send_group_msg(group_id, "✅ 群公告已发布" if ok else "❌ 发布失败，请确认Bot是管理员")
            return

        if cmd in ("取消全员禁言", "unshutup", "unmuteall"):
            ok = await bot.set_group_whole_ban(group_id, False)
            await bot.send_group_msg(group_id, "🔊 全员禁言已解除" if ok else "❌ 操作失败")
            return

        # --- 设置头衔 ---
        if cmd in ("头衔", "title"):
            parts = arg.split(maxsplit=1)
            if len(parts) >= 2 and parts[0].isdigit():
                target_qq = int(parts[0])
                title_text = parts[1].strip()[:6]
                ok = await bot.set_group_special_title(group_id, target_qq, title_text)
                await bot.send_group_msg(group_id, f"✅ 已设置 {target_qq} 的头衔为「{title_text}」" if ok else "❌ 设置失败")
            elif arg:
                ok = await bot.set_group_special_title(group_id, user_id, arg.strip()[:6])
                await bot.send_group_msg(group_id, f"✅ 你的头衔已设为「{arg.strip()[:6]}」" if ok else "❌ 设置失败")
            else:
                await bot.send_group_msg(group_id, "用法: @我 头衔 <QQ号> <头衔>  或  @我 头衔 <头衔>")
            return

        # --- 设置管理员 ---
        if cmd in ("设管理", "op", "setadmin"):
            if arg.isdigit():
                ok = await bot.set_group_admin(group_id, int(arg), True)
                await bot.send_group_msg(group_id, f"✅ 已设为管理员" if ok else "❌ 设置失败（Bot必须是群主才能设管理）")
            else:
                await bot.send_group_msg(group_id, "用法: @我 设管理 <QQ号>")
            return

        # --- 诊断 ---
        if cmd in ("诊断", "diag", "status"):
            diag_lines = ["🔍 **Bot 自检报告**", ""]
            # 连接状态
            diag_lines.append(f"📡 WebSocket: {'✅ 已连接' if bot._ws else '❌ 未连接'}")
            diag_lines.append(f"🤖 Bot QQ: {bot.bot_qq}")
            diag_lines.append(f"👑 Owner QQ: {config.OWNER_QQ}")
            # 本群权限
            try:
                info = await bot.get_group_member_info(group_id, int(bot.bot_qq or 0))
                role = info.get("role", "member") if info else "?"
                role_map = {"owner": "👑 群主", "admin": "🔧 管理员", "member": "👤 群成员"}
                diag_lines.append(f"🔑 本群身份: {role_map.get(role, role)}")
            except Exception as e:
                diag_lines.append(f"🔑 本群身份: 查询失败 ({e})")
            # API 连通性
            try:
                r = await bot._call("get_login_info", {})
                diag_lines.append(f"🔌 API连通: {'✅' if r.get('status')=='ok' else '❌ '+str(r.get('retcode','?'))}")
            except Exception as e:
                diag_lines.append(f"🔌 API连通: ❌ {e}")
            # 模型
            ms = memory.load_model_settings()
            diag_lines.append(f"🧠 AI模型: {ms.get('model', '?')}")
            diag_lines.append(f"🔑 API Key: {'✅ 已配置' if ms.get('api_key') else '❌ 未配置'}")
            # 审核
            diag_lines.append(f"🛡️ LLM审核: {'✅' if config.LLM_MOD_ENABLED else '❌'}")
            diag_lines.append(f"🤖 自动审核: {'✅' if config.AUTO_MOD_ENABLED else '❌'}")
            # 当前群模式
            mode = config.is_admin_mode(group_id, bot_is_admin)
            diag_lines.append(f"⚙️ 群模式: {'管理模式' if mode else '群友模式'}")
            await bot.send_group_msg(group_id, "\n".join(diag_lines))
            return

        # --- 帮助 ---
        if cmd in ("帮助", "help", "?"):
            help_text = (
                "🤖 **Xie 命令清单（主人专属）**\n\n"
                "🚫 **违规管理:**\n"
                "• @Xie 违规昵称 <QQ号> [是/违规] — 查/标违规昵称\n"
                "• @Xie 违规昵称 处理 <QQ号> — 踢出+拉黑违规昵称用户\n"
                "• @Xie 查昵称 — 扫描全群成员昵称\n"
                "• @Xie 查群名 — 检查群名是否违规\n"
                "• @Xie 统计 — 违规统计报告\n"
                "• @Xie 查询 <QQ号> — 查某人的违规记录\n"
                "• @Xie 加词 <词1,词2> — 手动添加违禁关键词\n\n"
                "🔇 **群管理（直接说话即可，AI 自动执行）:**\n"
                "• 「把xxx禁言10分钟」「踢掉xxx」「拉黑xxx」— 自然语言指令\n"
                "• 「回复xxx设精华」「全员禁言」— AI 会触发对应工具\n"
                "• @Xie 解禁 <QQ号> · 取消拉黑 <QQ号>\n"
                "• @Xie 黑名单 — 查看黑名单\n"
                "• @Xie 取消全员禁言\n"
                "• @Xie 设管理 <QQ号> · 头衔 <QQ号> <头衔>\n"
                "• @Xie 公告 <标题>|<内容> — 发布群公告\n\n"
                "⚖️ **模式切换:**\n"
                "• @Xie 群友模式 · 管理模式 · 自动模式\n\n"
                "🤖 **自动审核 & 学习:**\n"
                "• @Xie 开启自动 · 关闭自动\n"
                "• @Xie 训练状态 · 学习状态\n"
                "• @Xie 误判 — 回复消息 + @Xie 误判\n\n"
                "🚨 **审批系统:**\n"
                "• @Xie 待审批 — 查看待审批操作\n"
                "• @Xie 同意 <ID> · 拒绝 <ID>\n\n"
                "👋 **群设置:**\n"
                "• @Xie 设置欢迎 <消息/开启/关闭>\n"
                "• @Xie 群人设 [参数=值...]\n\n"
                "🎭 **AI 记忆:**\n"
                "• @Xie 清除记忆 · 我的记忆 · 群友记忆\n"
                "• @Xie 最近回忆\n\n"
                "📊 **其他:**\n"
                "• @Xie 表情库 · 影响力 · 活跃榜\n"
                "• @Xie 查成员 扫描全群成员昵称\n"
                "• Web UI: http://127.0.0.1:8090"
            )
            await bot.send_group_msg(group_id, help_text)
            return

    # ===== 公共命令（所有人都可以用）=====
    if at_bot:
        cmd_text_public = text
        cmd_public, arg_public = _parse_command(cmd_text_public)

        # --- 公共帮助 ---
        if cmd_public in ("帮助", "help", "?"):
            help_text = (
                "🤖 **群聊功能（所有人可用）**\n\n"
                "💬 **AI 聊天:**\n"
                "• @Xie <消息> — 跟AI聊天、问问题\n"
                "• 叫名字开头也行（如\"Xie 你好\"）\n"
                "• 被 @ 后 3 分钟内不用 @ 也能继续聊\n\n"
                "📅 **签到 & 积分:**\n"
                "• @Xie 签到 — 每日签到领积分\n"
                "• @Xie 积分 — 查看我的积分\n"
                "• @Xie 积分榜 / 签到榜 — 查看排行\n\n"
                "📊 **数据 & 娱乐:**\n"
                "• @Xie 总结 — 生成今日群聊日报\n"
                "• @Xie 抽 — 随机抽一个群友\n"
                "• @Xie 影响力 / 活跃榜 — 群友活跃度排行\n"
                "• @Xie 表情库 — 查看本群表情收藏\n\n"
                "🧠 **AI 记忆:**\n"
                "• @Xie 我的记忆 — Bot记得你的什么\n"
                "• @Xie 最近回忆 — 最近的群聊总结\n\n"
                "🛠️ **自助操作 （告诉Bot帮你自己）:**\n"
                "• \"帮我禁言10分钟\" — 自愿被禁言\n"
                "• \"帮我改名叫XXX\" — 改自己的群名片\n"
                "• \"撤回\" — 回复自己的消息+说撤回\n\n"
                "🌐 Web控制面板: http://127.0.0.1:8090"
            )
            if _is_owner(user_id):
                help_text += "\n\n🔑 **你是主人，更多命令:** @Xie 帮助（不带?）"
            await bot.send_group_msg(group_id, help_text)
            return

        # --- 签到 ---
        if cmd_public in ("签到", "打卡", "checkin"):
            result = welcome.do_checkin(user_id, group_id, user_name)
            await bot.send_group_msg(group_id, result["message"])
            return

        # --- 我的积分 ---
        if cmd_public in ("我的积分", "积分", "my points", "mypoints"):
            pts = welcome.get_points(user_id, group_id)
            checkin = welcome.get_checkin_status(user_id, group_id)
            await bot.send_group_msg(group_id,
                f"💰 {user_name} 的积分: {pts} 分\n"
                f"📅 签到: {'✅ 今日已签' if checkin['checked_today'] else '❌ 今日未签'} | "
                f"连续 {checkin['streak']} 天 · 累计 {checkin['total']} 天"
            )
            return

        # --- 签到排行榜 ---
        if cmd_public in ("签到榜", "签到排行", "checkinrank"):
            board = welcome.get_group_checkin_leaderboard(group_id)
            if not board:
                await bot.send_group_msg(group_id, "📭 本群还没有人签到~")
            else:
                lines = ["📅 **签到排行榜**"]
                medals = ["🥇", "🥈", "🥉"]
                for i, entry in enumerate(board):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    lines.append(f"{medal} {entry['user_name']} — 连续 {entry['streak']} 天 · 累计 {entry['total']} 天")
                await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 积分排行榜 ---
        if cmd_public in ("积分榜", "积分排行", "pointrank"):
            board = welcome.get_group_points_leaderboard(group_id)
            if not board:
                await bot.send_group_msg(group_id, "📭 本群还没有积分记录~")
            else:
                lines = ["💰 **积分排行榜**"]
                medals = ["🥇", "🥈", "🥉"]
                for i, entry in enumerate(board):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    lines.append(f"{medal} {entry['user_name']} — {entry['points']} 分")
                await bot.send_group_msg(group_id, "\n".join(lines))
            return

        # --- 欢迎设置（主人） ---
        if cmd_public in ("设置欢迎", "setwelcome") and _is_owner(user_id):
            if arg_public.lower() == "关闭" or arg_public.lower() == "off":
                welcome.set_group_welcome(group_id, False)
                await bot.send_group_msg(group_id, "✅ 本群新人欢迎已关闭")
            elif arg_public.lower() == "开启" or arg_public.lower() == "on":
                welcome.set_group_welcome(group_id, True)
                await bot.send_group_msg(group_id, "✅ 本群新人欢迎已开启")
            elif arg_public:
                welcome.set_group_welcome(group_id, True, arg_public)
                await bot.send_group_msg(group_id, f"✅ 欢迎消息已更新为:\n{arg_public}")
            else:
                await bot.send_group_msg(group_id,
                    "用法:\n"
                    "  设置欢迎 开启/关闭\n"
                    "  设置欢迎 <消息模板>\n"
                    "可用变量: {nickname} {qq} {at} {group_id}"
                )
            return

        # --- 群聊总结 ---
        if cmd_public in ("总结", "日报", "summary", "今日总结"):
            await bot.send_group_msg(group_id, "📊 正在生成今日总结...")
            from . import summary
            bot_name = group_personality.get_group_bot_name(group_id)
            text = await summary.generate_group_summary(group_id, bot_name)
            # 分段发送（QQ 消息有长度限制）
            if len(text) > 500:
                for i in range(0, len(text), 450):
                    await bot.send_group_msg(group_id, text[i:i+450])
            else:
                await bot.send_group_msg(group_id, text)
            return

        # --- 随机（抽一个人） ---
        if cmd_public in ("抽", "随机", "抽奖", "pick"):
            try:
                r = await bot._call("get_group_member_list", {"group_id": group_id})
                members = r.get("data", []) if r.get("status") == "ok" else []
                # 排除 bot 自己
                online = [m for m in members if str(m.get("user_id", "")) != str(config.BOT_QQ)]
                if online:
                    picked = random.choice(online)
                    name = picked.get("card") or picked.get("nickname") or str(picked.get("user_id", ""))
                    await bot.send_group_msg(group_id,
                        f"🎯 随机抽中: {name}({picked.get('user_id')})\n"
                        f"（共 {len(online)} 人参与）"
                    )
                else:
                    await bot.send_group_msg(group_id, "😅 没有可抽取的成员")
            except Exception:
                await bot.send_group_msg(group_id, "😅 抽取出错了")
            return

    # ===== 普通 AI 聊天 =====
    # 会话跟踪：群里若有活跃会话，任何发言者自动加入会话
    # （解决多人聊天 Bot 插不上嘴——第三个人插话也算同一条线，
    #   但最终是否回复仍由下面的决策引擎/概率拍板）
    if not at_bot and not kw_trigger and raw_message and _is_session_active(group_id):
        if user_id != int(bot.bot_qq or 0):
            _set_followup(group_id, user_id, user_name)

    in_followup = not at_bot and not kw_trigger and _is_in_followup(group_id, user_id)
    if at_bot or kw_trigger or in_followup:
        if at_bot:
            chat_text = text
            # @后面有文字但 segment 解析没拿到 → 从 raw_message 兜底提取
            if not chat_text and raw_message:
                import re as _re
                # 去掉 CQ 码（@、表情等），保留纯文本
                clean = _re.sub(r"\[CQ:[^\]]+\]", "", raw_message).strip()
                chat_text = clean
            _set_followup(group_id, user_id, user_name)  # 被 @ → 记住对话者
            # 🧠 意识：被 @ 了 → 开心
            ai_brain.on_bot_mentioned(user_name, group_id)
            # ★ Volition: 被回应了 → 被忽视计数清零（不改情绪，只清事实计数）
            from .consciousness import reset_ignored_counter as _reset_ig, get_state as _cs_state
            _reset_ig(_cs_state())
            # 🧠 简单情感检测：被夸/被怼
            _detect_sentiment_and_react(chat_text, user_name)
        elif kw_trigger:
            # 找到触发词并移除（可能在开头或中间）
            chat_text = text
            for kw in sorted(_get_trigger_keywords(group_id), key=len, reverse=True):
                if kw in text:
                    # 移除触发词（保留上下文）
                    chat_text = text.replace(kw, "", 1).strip()
                    break
            _set_followup(group_id, user_id, user_name)
            ai_brain.on_bot_mentioned(user_name, group_id)
        else:
            # 对话跟踪模式：用户刚跟 bot 聊过，可能在继续对话
            # 但也要看消息内容是否值得回复，别每条都接
            if raw_message and "[CQ:at,qq=" in raw_message:
                import re as _re_fu
                at_others = [m for m in _re_fu.findall(r"\[CQ:at,qq=(\d+)\]", raw_message)
                           if str(m) != str(bot.bot_qq) and m != "all"]
                if at_others:
                    logger.debug(f"follow-up中断→决策引擎: 用户{user_id}在@别人 {at_others}")
                    _followup.get(group_id, {}).get("users", {}).pop(user_id, None)
                    return  # 跳出 AI 聊天，下面走决策引擎

            # 👀 拟人：纯情绪消息先试表情表态 → 贴表情比打字像真人得多
            emoji_react, emoji_id, emoji_reason = decision_engine.should_emoji_react(raw_message or text)
            if emoji_react and message_id:
                try:
                    await bot.set_msg_emoji_like(message_id, emoji_id)
                    logger.info(f"表情表态 [群{group_id}] {emoji_reason}")
                    return
                except Exception:
                    pass  # 表情表态失败不阻塞，继续走文字回复

            # 走决策引擎判断这条消息是否值得接话
            should_follow, reason = await decision_engine.should_reply_to_message(
                bot, group_id, user_id, user_name, raw_message or text,
                is_at_bot=False, is_reply_to_bot=False, quoted_context=quoted_context,
            )
            if not should_follow:
                logger.debug(f"follow-up跳过(决策引擎): {reason}")
                return  # 不强制回复，让后续决策引擎继续判断也行，这里直接跳过
            chat_text = text

        if not chat_text:
            # 有人叫了bot但没说什么 → 让AI根据上下文自然回应
            # 不再用硬编码的"在呢/嗯？"，避免太机械
            chat_text = "（有人叫了我一声）"

        # 🔧 解析 @mention: 把 [CQ:at,qq=xxx] 替换为 @用户名
        # 这样 AI 看到 "@嘟嘟 他喽" 而不是 "他喽"，能理解"他"是谁
        if raw_message and "[CQ:at,qq=" in raw_message:
            try:
                resolved = await _resolve_at_mentions(bot, group_id, raw_message)
                import re as _re2
                # 去掉 bot 自己的 @ (避免 AI 困惑) 和其他 CQ 码
                bot_at = f"[CQ:at,qq={bot.bot_qq}]"
                resolved_clean = resolved.replace(bot_at, "").strip()
                resolved_clean = _re2.sub(r"\[CQ:[^\]]+\]", "", resolved_clean).strip()
                # 如果解析后内容比 segment text 更丰富，使用解析后的
                if resolved_clean and len(resolved_clean) > len(chat_text):
                    chat_text = resolved_clean
            except Exception:
                pass

        t0 = time.time()
        roster = await _build_roster(
            bot, group_id, user_id, user_name, raw_message, quoted_context
        )
        reply = await chat.chat_reply(
            group_id, user_id, user_name, chat_text,
            quoted_context=quoted_context,
            media_context=media_ctx,
            message_id=message_id,
            roster=roster,
            at_bot=at_bot,
            master_instructed=is_master,
        )
        if reply:
            clean_text = await _execute_ai_actions(
                bot, group_id, user_id, user_name, reply, reply_msg_id
            )
            if clean_text:
                # 剥掉多余的引用/@ —— 对方刚说完话就回，不需要引用他也不需要 @ 他
                clean_text = _get_reply_policy().sanitize_targeting(
                    clean_text,
                    group_id=group_id,
                    trigger_message_id=message_id,
                    trigger_user_qq=user_id,
                    trigger_user_name=user_name,
                )
            if clean_text:
                await humanizer.send_human_like(bot, group_id, clean_text, started_at=t0)
            memory.window_add_message(group_id, int(bot.bot_qq), group_personality.get_group_bot_name(group_id), clean_text or reply)
            # 🧠 意识：成功回复了
            ai_brain.on_bot_replied_to(user_name)
            # ★ Volition: 被回应了 → 被忽视计数清零（不改情绪，只清事实计数）
            from .consciousness import reset_ignored_counter as _reset_ig, get_state as _cs_state
            _reset_ig(_cs_state())
        else:
            await humanizer.send_human_like(
                bot, group_id,
                random.choice(["等会 脑子有点卡", "卡了 待会再喊我", "刚走神了，过会再说一遍呗"]),
            )
            _clear_followup(group_id)
        return

    # ===== 没人 @ Bot → 决策是否主动回复 =====
    if raw_message and len(raw_message) > 5 and not _is_owner(user_id):
        # 👀 拟人：纯情绪消息先试表情表态
        emoji_react, emoji_id, emoji_reason = decision_engine.should_emoji_react(raw_message)
        if emoji_react and message_id:
            try:
                await bot.set_msg_emoji_like(message_id, emoji_id)
                logger.info(f"表情表态 [群{group_id}] {emoji_reason}")
            except Exception:
                pass
        else:
            should_reply, reason = await decision_engine.should_reply_to_message(
                bot, group_id, user_id, user_name, raw_message,
                is_at_bot=False,
                is_reply_to_bot=is_reply and quoted_context is not None,
                quoted_context=quoted_context,
            )
            if should_reply:
                logger.info(f"主动回复决策: 群{group_id} {user_name} — {reason}")
                asyncio.create_task(_auto_reply(
                    bot, group_id, user_id, user_name, raw_message, quoted_context,
                    media_ctx, message_id,
                ))


async def _auto_reply(
    bot: OneBotClient, group_id: int, user_qq: int, user_name: str,
    message: str, quoted_context: dict, media_ctx: list[dict],
    message_id: int = 0,
):
    """自主回复（后台执行）"""
    try:
        t0 = time.time()
        # 🔧 解析 @mention: 把 [CQ:at,qq=xxx] 替换为 @用户名
        chat_msg = message
        if message and "[CQ:at,qq=" in message:
            try:
                resolved = await _resolve_at_mentions(bot, group_id, message)
                import re as _re3
                bot_at = f"[CQ:at,qq={bot.bot_qq}]"
                resolved_clean = resolved.replace(bot_at, "").strip()
                resolved_clean = _re3.sub(r"\[CQ:[^\]]+\]", "", resolved_clean).strip()
                if resolved_clean:
                    chat_msg = resolved_clean
            except Exception:
                pass
        roster = await _build_roster(
            bot, group_id, user_qq, user_name, message, quoted_context
        )
        reply = await chat.chat_reply(
            group_id, user_qq, user_name, chat_msg,
            quoted_context=quoted_context,
            media_context=media_ctx,
            message_id=message_id,
            roster=roster,
            at_bot=False,  # 自主回复不是被 @
            master_instructed=_is_owner(user_qq),
        )
        if reply:
            clean_text, actions = chat.parse_actions(reply)
            # 自主回复不执行 AI 自助操作（安全考虑）
            # 主动插话更不该引用/@ —— 没人问你，你自己冒出来还引用别人就很怪
            clean_text = _get_reply_policy().sanitize_targeting(
                clean_text,
                group_id=group_id,
                trigger_message_id=message_id,
                trigger_user_qq=user_qq,
                trigger_user_name=user_name,
            )
            if clean_text and len(clean_text) > 1:
                await humanizer.send_human_like(bot, group_id, clean_text, started_at=t0)
                memory.window_add_message(group_id, int(bot.bot_qq), group_personality.get_group_bot_name(group_id), clean_text)
                logger.info(f"自主回复 [群{group_id}] -> {clean_text[:60]}...")
    except Exception as e:
        logger.error(f"自主回复异常: {e}")
