"""
加群验证模块 — 群内出题 + 群内判题（不再私聊）

流程（新）:
  1. 收到加群请求 → 直接同意进群
  2. 收到 group_increase 通知 → 在群里 @ 新人出题
  3. 监听群消息 → 判定新人回答
  4. 答对 → 留在群里
  5. 答错/超时 → 踢出群

题目会记录去重，避免重复出题。
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from . import llm_call

logger = logging.getLogger("qqbot")

# ─── 待验证列表 ───
# {group_id: {user_id: {question, answer, user_name, created_at}}}
_pending: dict[int, dict[int, dict]] = {}

# ─── 已出过的题目（去重）───
_used_questions: set[str] = set()

# ─── 超时时间（秒）───
VERIFY_TIMEOUT = 300  # 5 分钟

# ─── 每群最多同时验证人数 ───
MAX_PER_GROUP = 5


def _get_client():
    """获取 Anthropic 客户端（委托给 llm_call 共享客户端）"""
    return llm_call.get_client()


# ═══════════════════════════════════════════════════════════
#  入口 1: 加群请求 → 直接同意（验证在群内做）
# ═══════════════════════════════════════════════════════════

async def handle_group_add_request(bot, event: dict):
    """
    处理加群请求事件。

    旧流程：私聊出题 → 私聊判题 → 通过再同意
    新流程：直接同意进群 → 进群后群里 @ 出题 → 群里判题 → 不对就踢

    OneBot 事件格式:
      post_type: "request"
      request_type: "group"
      sub_type: "add" 或 "invite"
      group_id, user_id, comment, flag
    """
    sub_type = event.get("sub_type", "")

    # 只处理主动加群，不处理邀请
    if sub_type != "add":
        return

    user_id = event.get("user_id", 0)
    group_id = event.get("group_id", 0)
    flag = event.get("flag", "")

    if not user_id or not group_id or not flag:
        logger.warning(f"加群请求缺少必要字段: user={user_id} group={group_id} flag={flag}")
        return

    # 取昵称（仅用于日志）
    user_name = str(user_id)
    try:
        info = await bot.get_stranger_info(user_id)
        if info:
            user_name = info.get("nickname", "") or user_name
    except Exception:
        pass

    logger.info(f"🔑 加群请求: {user_name}({user_id}) 申请群{group_id} → 直接放行，进群后再验证")

    try:
        await bot.set_group_add_request(flag, approve=True, reason="请在群里完成验证")
    except Exception as e:
        logger.error(f"同意加群失败: {e}")


# ═══════════════════════════════════════════════════════════
#  入口 2: 成员入群通知 → 群里 @ 出题
# ═══════════════════════════════════════════════════════════

async def handle_member_increase(bot, event: dict):
    """
    新人入群 → 群里 @ 出题。

    OneBot 事件格式:
      post_type: "notice"
      notice_type: "group_increase"
      sub_type: "approve" / "invite"
      group_id, user_id, operator_id
    """
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    bot_qq = getattr(bot, "bot_qq", 0) or 0

    if not group_id or not user_id or user_id == bot_qq:
        return

    # 已经在验证中？跳过（防重复触发）
    if _pending.get(group_id, {}).get(user_id):
        return

    # 验证队列满了 → 直接踢掉，避免群里堆一堆未验证的人
    if len(_pending.get(group_id, {})) >= MAX_PER_GROUP:
        logger.info(f"加群验证: 群{group_id} 队列已满，踢出 {user_id}")
        try:
            await bot._call("set_group_kick", {"group_id": group_id, "user_id": user_id})
            await bot.send_group_msg(group_id, f"⏳ 验证队列繁忙，请稍后再申请~")
        except Exception:
            pass
        return

    # 取昵称
    user_name = str(user_id)
    try:
        info = await bot.get_group_member_info(group_id, user_id)
        if info:
            card = (info.get("card", "") or "").strip()
            nick = (info.get("nickname", "") or "").strip()
            user_name = card or nick or user_name
    except Exception:
        pass

    # AI 出题
    try:
        question, answer = await _generate_question()
    except Exception as e:
        logger.error(f"AI 出题失败: {e}")
        question, answer = _fallback_question()

    if not question:
        # 连备用题库都没了 —— 保守起见放人进群，不踢
        logger.warning(f"出题失败且无备用，{user_name}({user_id}) 直接放行")
        return

    # 记录待验证
    _pending.setdefault(group_id, {})[user_id] = {
        "question": question,
        "answer": answer,
        "user_name": user_name,
        "created_at": time.time(),
    }
    _used_questions.add(question)

    # 群里 @ 新人出题（走 humanizer 拆条 + 拟人节奏）
    from .humanizer import send_human_like
    at_seg = f"[CQ:at,qq={user_id}]"
    msg = (
        f"{at_seg} 欢迎入群~\n"
        f"请回答验证题（5分钟内）：\n\n"
        f"📝 {question}\n\n"
        f"答对留下 :96:\n"
        f"答错踢出 :178:"
    )
    try:
        await send_human_like(bot, group_id, msg)
        logger.info(f"加群验证: 已向 {user_name}({user_id}) 在群{group_id} 出题: {question[:30]}")
    except Exception as e:
        logger.error(f"群内发送验证题失败: {e}")
        _cleanup_user(group_id, user_id)
        return

    # 启动超时任务
    asyncio.create_task(_timeout_kick(bot, group_id, user_id, user_name))


async def _timeout_kick(bot, group_id: int, user_id: int, user_name: str):
    """超时自动踢出"""
    await asyncio.sleep(VERIFY_TIMEOUT)
    if not _pending.get(group_id, {}).get(user_id):
        return  # 已经处理掉了
    logger.info(f"加群验证超时: {user_name}({user_id}) → 自动踢出")
    _log_verification(user_id, user_name, group_id, "timeout", "", "", "超时未回答")
    try:
        await bot._call("set_group_kick", {"group_id": group_id, "user_id": user_id})
    except Exception as e:
        logger.error(f"超时踢人失败: {e}")
    try:
        from .humanizer import send_human_like
        await send_human_like(
            bot, group_id,
            f"⏰ [CQ:at,qq={user_id}] 验证超时，已请出群聊~",
        )
    except Exception:
        pass
    _cleanup_user(group_id, user_id)


# ═══════════════════════════════════════════════════════════
#  入口 3: 群消息回调 → 检测待验证人是否在答题
# ═══════════════════════════════════════════════════════════

async def handle_group_verify_answer(bot, event: dict) -> bool:
    """
    群消息回调中调用：检测发送者是否正在"待验证"，是则判题。

    Returns:
        True  = 是验证答题，已处理（调用方应 return，不再走正常 AI 聊天）
        False = 不是验证答题，调用方继续正常流程
    """
    user_id = event.get("user_id", 0)
    group_id = event.get("group_id", 0)
    raw = (event.get("raw_message") or "").strip()

    if not user_id or not group_id or not raw:
        return False

    pending = _pending.get(group_id, {}).get(user_id)
    if not pending:
        return False

    # 超时检查（理论上超时任务会清掉，这里兜底）
    if time.time() - pending["created_at"] > VERIFY_TIMEOUT:
        _cleanup_user(group_id, user_id)
        return False

    question = pending["question"]
    expected = pending["answer"]
    user_name = pending["user_name"]

    logger.info(f"🔑 验证回答: {user_name}({user_id}) → \"{raw[:30]}\" (题: {question[:20]})")

    # AI 判题
    try:
        is_correct, reason = await _judge_answer(question, expected, raw)
    except Exception as e:
        logger.error(f"AI 判题失败: {e}")
        is_correct = _simple_match(expected, raw)
        reason = "简单匹配"

    from .humanizer import send_human_like
    at_seg = f"[CQ:at,qq={user_id}]"

    if is_correct:
        logger.info(f"✅ 验证通过: {user_name}({user_id}) → 群{group_id} ({reason})")
        _log_verification(user_id, user_name, group_id, "approved", question, raw, reason)
        try:
            await send_human_like(
                bot, group_id,
                f"✅ {at_seg} 回答正确，欢迎入群~ :96:",
            )
        except Exception:
            pass

        # ── 补发欢迎消息 + 加积分（之前在 group_increase 时被 verify 接管了）──
        try:
            from . import welcome as _welcome
            welcome_msg = _welcome.get_welcome_message(group_id, user_name, user_id)
            if welcome_msg:
                await send_human_like(bot, group_id, welcome_msg)
        except Exception as e:
            logger.debug(f"补发欢迎消息失败: {e}")
        try:
            from . import welcome as _welcome
            _welcome.add_points(user_id, group_id, user_name, 10, "通过加群验证")
        except Exception as e:
            logger.debug(f"加积分失败: {e}")
    else:
        logger.info(f"❌ 验证失败: {user_name}({user_id}) → 踢出 ({reason})")
        _log_verification(user_id, user_name, group_id, "rejected", question, raw, reason)
        try:
            await send_human_like(
                bot, group_id,
                f"❌ {at_seg} 答错了，再见~ :178:",
            )
        except Exception:
            pass
        try:
            await bot._call("set_group_kick", {"group_id": group_id, "user_id": user_id})
        except Exception as e:
            logger.error(f"踢人失败: {e}")

    _cleanup_user(group_id, user_id)
    return True


# ═══════════════════════════════════════════════════════════
#  AI 出题 / 判题（保留原版逻辑）
# ═══════════════════════════════════════════════════════════

async def _generate_question() -> tuple[str, str]:
    """使用 AI 生成一个简单验证问题。返回 (question, answer)"""
    client = _get_client()
    if client is None:
        return _fallback_question()

    from .memory import load_model_settings
    ms = load_model_settings()

    used_hint = ""
    if _used_questions:
        recent = list(_used_questions)[-10:]
        used_hint = "\n\n【已出过的题目，不要重复】\n" + "\n".join(f"  - {q}" for q in recent)

    system_prompt = (
        "你是一个QQ群加群验证的出题助手。你需要生成一道简单的问题来验证申请人是否是真人。\n\n"
        "要求：\n"
        "- 题目简单，普通人5秒内能答对\n"
        "- 答案明确，不超过5个字\n"
        "- 不能问个人信息（名字、年龄、性别等）\n"
        "- 可以是：常识、简单计算、成语补全、猜谜、脑筋急转弯等\n"
        "- 题目用中文\n"
        "- 不要出太难的题目，目的只是过滤机器人\n"
        f"{used_hint}\n\n"
        '返回纯JSON，不要Markdown代码块: {"question": "题目", "answer": "答案"}'
    )

    response = await llm_call.llm_create(
        model=ms.get("model", "glm-5.2-free"),
        max_tokens=150,
        temperature=1.0,
        system=system_prompt,
        messages=[{"role": "user", "content": "出一道验证题"}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    result = json.loads(text)
    question = result.get("question", "").strip()
    answer = result.get("answer", "").strip()

    if not question or not answer:
        return _fallback_question()

    return question, answer


async def _judge_answer(question: str, expected: str, user_answer: str) -> tuple[bool, str]:
    """AI 判题（语义匹配，不要求字面完全一致）。"""
    client = _get_client()
    if client is None:
        return _simple_match(expected, user_answer), "简单匹配"

    from .memory import load_model_settings
    ms = load_model_settings()

    system_prompt = (
        "判断用户的回答是否与正确答案语义一致。"
        "答案的表述方式可以不同，只要意思对就算正确。"
        "比如正确答案是\"4\"，回答\"四\"也算对。"
        "比如正确答案是\"李白\"，回答\"诗仙李白\"也算对。"
        '返回纯JSON: {"correct": true/false, "reason": "一句话说明理由"}'
    )

    response = await llm_call.llm_create(
        model=ms.get("model", "glm-5.2-free"),
        max_tokens=100,
        temperature=0.1,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"问题: {question}\n正确答案: {expected}\n用户回答: {user_answer}"
        }],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    result = json.loads(text)
    return result.get("correct", False), result.get("reason", "")


def _simple_match(expected: str, user_answer: str) -> bool:
    """简单字符串匹配（兜底）"""
    e = expected.strip().lower()
    u = user_answer.strip().lower()
    return e == u or e in u or u in e


def _fallback_question() -> tuple[str, str]:
    """备用题库——当 AI 不可用时使用"""
    import random
    questions = [
        ("1 + 1 等于几？", "2"),
        ("一年有几个季节？", "四个"),
        ("太阳从哪个方向升起？", "东方"),
        ("猫怎么叫？（一个字）", "喵"),
        ("中国的首都是哪里？", "北京"),
        ("什么动物会飞？（答一种）", "鸟"),
        ("2 × 3 等于多少？", "6"),
        ("一周有几天？", "七天"),
    ]
    available = [(q, a) for q, a in questions if q not in _used_questions]
    if not available:
        available = questions
    q, a = random.choice(available)
    return q, a


# ═══════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════

def _cleanup_user(group_id: int, user_id: int):
    """清理某个群某用户的验证记录"""
    g = _pending.get(group_id)
    if g:
        g.pop(user_id, None)
        if not g:
            _pending.pop(group_id, None)


def cleanup_expired():
    """清理超时的待验证记录（周期调用）"""
    now = time.time()
    expired_groups: list[int] = []
    for gid, users in list(_pending.items()):
        expired_users = [uid for uid, v in users.items() if now - v.get("created_at", 0) > VERIFY_TIMEOUT]
        for uid in expired_users:
            users.pop(uid, None)
        if not users:
            expired_groups.append(gid)
    for gid in expired_groups:
        _pending.pop(gid, None)


# ═══════════════════════════════════════════════════════════
#  验证历史记录（供 Web 后台查看）
# ═══════════════════════════════════════════════════════════

_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "verify_history.json"
)
_MAX_HISTORY = 500


def _log_verification(user_id: int, user_name: str, group_id: int,
                      result: str, question: str, answer: str, reason: str):
    """记录一条验证历史"""
    from datetime import datetime
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "user_name": user_name,
        "group_id": group_id,
        "result": result,       # "approved" | "rejected" | "timeout"
        "question": question[:50],
        "answer": answer[:30],
        "reason": reason[:100],
    }

    history = []
    if os.path.exists(_HISTORY_FILE):
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    history.insert(0, entry)

    if len(history) > _MAX_HISTORY:
        history = history[:_MAX_HISTORY]

    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"验证历史写入失败: {e}")


def get_verification_history(limit: int = 50) -> list[dict]:
    """获取验证历史记录（供 Web API 调用）"""
    if not os.path.exists(_HISTORY_FILE):
        return []
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        return history[:limit]
    except Exception:
        return []
