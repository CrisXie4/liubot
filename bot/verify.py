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
from .config import config

logger = logging.getLogger("qqbot")

# ─── 待验证列表 ───
# {group_id: {user_id: {question, answer, user_name, created_at}}}
_pending: dict[int, dict[int, dict]] = {}

# ─── 已出过的题目（去重）───
_used_questions: set[str] = set()

# ─── 网站认证 pending（token → 挑战）───
# {token: {group_id, user_id, code, created_at, wrong_attempts, user_name, revealed}}
_web_pending: dict[str, dict] = {}

# ─── 网站认证最近用过的词（去重）───
_used_web_codes: set[str] = set()
_WEB_CODE_KEEP = 50  # 超过这个数清掉一半

# ─── 超时时间（秒）───
VERIFY_TIMEOUT = 300  # 5 分钟

# ─── 每群最多同时验证人数 ───
MAX_PER_GROUP = 5

# ─── 每个新人最多答错次数（第 N+1 次错才踢）───
MAX_WRONG_ATTEMPTS = 2

# ─── 网站认证错满 3 次才踢（3 次内都给机会）───
MAX_WRONG_ATTEMPTS_WEB = 3
# token 短截用于日志（避免泄露）
_TOKEN_LOG_LEN = 6
# 内置兜底词库（AI 失败时用，10 个常见双字词）
_WEB_CODE_FALLBACK = [
    "星空", "咖啡", "晚风", "远山", "清风",
    "晚霞", "月光", "晨雾", "海浪", "竹林",
]


def _get_client():
    """获取 Anthropic 客户端（委托给 llm_call 共享客户端）"""
    return llm_call.get_client()


# ═══════════════════════════════════════════════════════════
#  网站认证 — 配置读取 / 概率分流 / 词生成
# ═══════════════════════════════════════════════════════════

def _get_web_ratio() -> int:
    """读取当前网站模式概率（0-100）。先 env 后 settings.json。"""
    from .config import config
    env_ratio = config.WEB_VERIFY_RATIO  # env 默认 50
    # settings.json 覆盖（后台改的值）
    try:
        import os as _os, json as _json
        path = _os.path.join(config.DATA_DIR, "bot_settings.json")
        if _os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if "web_verify_ratio" in data:
                env_ratio = int(data["web_verify_ratio"])
    except Exception:
        pass
    return max(0, min(100, env_ratio))


def _pick_web_mode() -> bool:
    """掷骰子：命中网站模式返回 True"""
    import random
    return random.randint(1, 100) <= _get_web_ratio()


def _normalize_code(s: str) -> str:
    """normalize 验证词：去所有空白 + 中英文标点 + lower"""
    if not s:
        return ""
    # 去空白
    t = re.sub(r"\s+", "", s)
    # 去中英文标点
    t = re.sub(r"[，。！？、；：\u201c\u201d\u2018\u2019（）【】《》,.!?;:\"'()\[\]<>]", "", t)
    return t.lower()


async def _generate_web_code() -> str:
    """AI 生成一个 2-4 字中文词作为网站验证码。失败兜底。"""
    client = _get_client()
    if client is None:
        return _fallback_web_code()

    from .memory import load_model_settings
    ms = load_model_settings()

    used_hint = ""
    if _used_web_codes:
        recent = list(_used_web_codes)[-10:]
        used_hint = "\n\n【最近用过的词，不要重复】\n" + "\n".join(f"  - {c}" for c in recent)

    system_prompt = (
        "你是加群验证词生成器。生成一个 2-4 字的中文词作为验证码。\n"
        "硬性要求：\n"
        "- 常见、正面或中性、无歧义、易输入\n"
        "- 不要生僻字（必须在中日韩统一汉字常用区）\n"
        "- 不要数字、英文、标点\n"
        "- 不要敏感、政治、低俗内容\n"
        f"{used_hint}\n"
        '返回纯JSON，不要Markdown代码块: {"code": "星空"}'
    )

    try:
        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=50,
            temperature=0.8,
            system=system_prompt,
            messages=[{"role": "user", "content": "生成一个验证词"}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = re.sub(r"```", "", text).strip()
        result = json.loads(text)
        code = (result.get("code") or "").strip()
        # 校验：只允许 CJK 汉字 + 长度 2-4
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", code):
            logger.warning(f"AI 生成的词不合法: {code!r} → 走兜底")
            return _fallback_web_code()
        _used_web_codes.add(code)
        # 维护 _used_web_codes 不超长
        if len(_used_web_codes) > _WEB_CODE_KEEP:
            to_drop = list(_used_web_codes)[:_WEB_CODE_KEEP // 2]
            for c in to_drop:
                _used_web_codes.discard(c)
        return code
    except Exception as e:
        logger.warning(f"AI 生成验证词失败: {e} → 走兜底")
        return _fallback_web_code()


def _fallback_web_code() -> str:
    """内置兜底词库随机"""
    import random
    available = [c for c in _WEB_CODE_FALLBACK if c not in _used_web_codes]
    if not available:
        available = _WEB_CODE_FALLBACK
    code = random.choice(available)
    _used_web_codes.add(code)
    return code


# ═══════════════════════════════════════════════════════════
#  网站认证 — token 生命周期
# ═══════════════════════════════════════════════════════════

async def _verify_turnstile(cf_token: str, remote_ip: str = "") -> bool:
    """调 Cloudflare siteverify 校验。成功/降级返回 True，校验明确失败返回 False。"""
    from .config import config
    secret = config.TURNSTILE_SECRET_KEY
    if not secret:
        logger.warning("Turnstile secret 未配置，降级放行（生产环境应配置）")
        return True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {"secret": secret, "response": cf_token}
            if remote_ip:
                payload["remoteip"] = remote_ip
            async with session.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                result = await resp.json()
                return bool(result.get("success", False))
    except Exception as e:
        logger.warning(f"Turnstile 校验异常: {e} → 降级放行")
        return True


async def create_web_challenge(group_id: int, user_id: int, user_name: str) -> str:
    """为新人生成一个 web 验证挑战。返回 token。"""
    import secrets
    code = await _generate_web_code()
    token = secrets.token_urlsafe(16)  # 22 字符
    _web_pending[token] = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "code": code,
        "created_at": time.time(),
        "wrong_attempts": 0,
        "revealed": False,
    }
    logger.info(f"🌐 网站验证挑战: {user_name}({user_id}) 群{group_id} 词={code} token={token[:_TOKEN_LOG_LEN]}...")
    return token


async def reveal_web_code(token: str, cf_token: str, remote_ip: str = "") -> dict:
    """新人点完 Turnstile 后调，校验通过返回词。"""
    # 1. token 存在？
    entry = _web_pending.get(token)
    if not entry:
        return {"ok": False, "error": "token_not_found", "message": "验证码已失效，请联系管理员"}
    # 2. token 过期？
    if time.time() - entry["created_at"] > VERIFY_TIMEOUT:
        _cleanup_web_token(token)
        return {"ok": False, "error": "token_expired", "message": "验证码已过期，请重新触发"}
    # 3. Turnstile 校验
    if not await _verify_turnstile(cf_token, remote_ip):
        return {"ok": False, "error": "captcha_failed", "message": "人机验证未通过，请重试"}
    # 4. 标记已 reveal，返回词
    entry["revealed"] = True
    return {"ok": True, "code": entry["code"]}


async def consume_web_challenge(group_id: int, user_id: int, user_answer: str) -> dict:
    """新人群里发词时调，本地比对。返回 verdict + 元信息。

    verdict:
      - correct: 答对（token 已清理）
      - wrong: 答错，attempts 累加；若 attempts >= max 则 final=True 且 token 清理
      - smalltalk: 寒暄，不消耗次数
      - not_found: 该用户不在 _web_pending
    """
    # 按 (group, user) 找 token
    token = None
    entry = None
    for t, e in _web_pending.items():
        if e["group_id"] == group_id and e["user_id"] == user_id:
            token = t
            entry = e
            break
    if not entry:
        return {"verdict": "not_found"}

    # 过期兜底
    if time.time() - entry["created_at"] > VERIFY_TIMEOUT:
        _cleanup_web_token(token)
        return {"verdict": "not_found"}

    expected = entry["code"]
    # 寒暄识别（不消耗次数）
    if _looks_like_smalltalk(user_answer):
        return {"verdict": "smalltalk", "expected": expected}

    user_norm = _normalize_code(user_answer)
    expected_norm = _normalize_code(expected)

    if user_norm and user_norm == expected_norm:
        attempts = entry["wrong_attempts"] + 1
        _cleanup_web_token(token)
        return {"verdict": "correct", "expected": expected, "attempts": attempts}

    # 答错
    entry["wrong_attempts"] += 1
    attempts = entry["wrong_attempts"]
    if attempts >= MAX_WRONG_ATTEMPTS_WEB:
        _cleanup_web_token(token)
        return {"verdict": "wrong", "expected": expected, "attempts": attempts,
                "max": MAX_WRONG_ATTEMPTS_WEB, "final": True}
    return {"verdict": "wrong", "expected": expected, "attempts": attempts,
            "max": MAX_WRONG_ATTEMPTS_WEB}


def _cleanup_web_token(token: str) -> None:
    """删一个 token"""
    _web_pending.pop(token, None)


def _cleanup_web_user(group_id: int, user_id: int) -> None:
    """按 (group, user) 删 token"""
    to_drop = [t for t, e in _web_pending.items()
               if e["group_id"] == group_id and e["user_id"] == user_id]
    for t in to_drop:
        _web_pending.pop(t, None)


async def _handle_web_answer(bot, event: dict, group_id: int, user_id: int, raw: str) -> bool:
    """网站认证模式的答题处理。返回 True（已处理）。"""
    from .humanizer import send_human_like
    at_seg = f"[CQ:at,qq={user_id}]"

    # 查用户名（从 entry 拿）
    user_name = str(user_id)
    for e in _web_pending.values():
        if e["group_id"] == group_id and e["user_id"] == user_id:
            user_name = e.get("user_name", user_name)
            break

    logger.info(f"🌐 网站验证回答: {user_name}({user_id}) → \"{raw[:30]}\"")

    try:
        result = await consume_web_challenge(group_id, user_id, raw)
    except Exception as e:
        logger.error(f"网站判题异常: {e}")
        result = {"verdict": "not_found"}

    verdict = result.get("verdict")

    if verdict == "not_found":
        return False  # 兜底：异常时回落到出题流程（实际几乎不会发生）

    if verdict == "smalltalk":
        try:
            await send_human_like(
                bot, group_id,
                f"{at_seg} 请把网站上的词发到群里哦~ 📝",
            )
        except Exception:
            pass
        return True

    if verdict == "correct":
        attempts = result.get("attempts", 1)
        logger.info(f"✅ 网站验证通过: {user_name}({user_id}) 群{group_id} (第{attempts}次)")
        _log_verification(user_id, user_name, group_id, "approved_web",
                          result.get("expected", ""), raw, f"第{attempts}次答对",
                          method="web", attempts=attempts)
        try:
            await send_human_like(
                bot, group_id,
                f"✅ {at_seg} 回答正确，欢迎入群~ :96:",
            )
        except Exception:
            pass
        # 补发欢迎 + 加积分（与出题模式一致）
        try:
            from . import welcome as _welcome
            welcome_msg = _welcome.get_welcome_message(group_id, user_name, user_id)
            if welcome_msg:
                await send_human_like(bot, group_id, welcome_msg)
        except Exception as e:
            logger.debug(f"补发欢迎消息失败: {e}")
        try:
            from . import welcome as _welcome
            _welcome.add_points(user_id, group_id, user_name, 10, "通过加群验证（网站）")
        except Exception as e:
            logger.debug(f"加积分失败: {e}")
        return True

    if verdict == "wrong":
        attempts = result.get("attempts", 1)
        max_a = result.get("max", MAX_WRONG_ATTEMPTS_WEB)
        is_final = result.get("final", False)
        expected = result.get("expected", "")

        if is_final:
            logger.info(f"❌ 网站验证失败: {user_name}({user_id}) → 踢出 ({attempts}次错)")
            _log_verification(user_id, user_name, group_id, "rejected_web",
                              expected, raw, f"{attempts}次全错",
                              method="web", attempts=attempts)
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
        else:
            remaining = max_a - attempts
            logger.info(f"❌ 网站验证答错({attempts}/{max_a}): {user_name}({user_id})")
            try:
                await send_human_like(
                    bot, group_id,
                    f"❌ {at_seg} 码不对哦，再去网站仔细看看~ 还有 {remaining} 次机会",
                )
            except Exception:
                pass
        return True

    return False


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

async def handle_member_increase(bot, event: dict) -> bool:
    """
    新人入群 → 群里 @ 出题。

    OneBot 事件格式:
      post_type: "notice"
      notice_type: "group_increase"
      sub_type: "approve" / "invite"
      group_id, user_id, operator_id

    Returns:
        True  — verify 已接管（出题/发链接/白名单放行/队列满踢人/重复触发）。
                handler.py 收到 True 后不会再发"回退欢迎"，避免双发。
        False — 事件无效（user_id=0 或 bot 自己），handler 应走普通欢迎回退。
    """
    group_id = event.get("group_id", 0)
    user_id = event.get("user_id", 0)
    bot_qq = getattr(bot, "bot_qq", 0) or 0

    if not group_id or not user_id or user_id == bot_qq:
        return False

    # 免验证白名单 → 直接放行（Task 5 新增）
    if is_whitelisted(user_id, group_id):
        _log_verification(user_id, str(user_id), group_id, "auto_whitelist",
                          "", "", "免验证白名单")
        logger.info(f"免验证白名单: {user_id} 群{group_id} → 直接放行")
        try:
            from .humanizer import send_human_like
            from . import welcome as _welcome
            welcome_msg = _welcome.get_welcome_message(group_id, str(user_id), user_id)
            if welcome_msg:
                await send_human_like(bot, group_id, welcome_msg)
            _welcome.add_points(user_id, group_id, str(user_id), 10, "免验证入群")
        except Exception as e:
            logger.debug(f"白名单放行发欢迎失败: {e}")
        return True  # 已发欢迎，阻止 handler 重复发

    # 已经在验证中？跳过（防重复触发，两种模式都查）
    if _pending.get(group_id, {}).get(user_id):
        return True  # 已接管，防重复
    if any(e["group_id"] == group_id and e["user_id"] == user_id for e in _web_pending.values()):
        return True  # 已接管，防重复

    # 验证队列满了（两种模式合计）→ 直接踢掉，避免群里堆一堆未验证的人
    web_count = sum(1 for e in _web_pending.values() if e["group_id"] == group_id)
    if len(_pending.get(group_id, {})) + web_count >= MAX_PER_GROUP:
        logger.info(f"加群验证: 群{group_id} 队列已满，踢出 {user_id}")
        try:
            await bot._call("set_group_kick", {"group_id": group_id, "user_id": user_id})
            await bot.send_group_msg(group_id, f"⏳ 验证队列繁忙，请稍后再申请~")
        except Exception:
            pass
        return True  # 已踢，阻止 handler 发欢迎

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

    # ── 模式分流：网站认证 or AI 出题 ──
    if _pick_web_mode():
        # ── 网站认证模式 ──
        try:
            token = await create_web_challenge(group_id, user_id, user_name)
        except Exception as e:
            logger.error(f"创建网站验证挑战失败: {e} → 回落出题模式")
            token = None

        if token:
            from .humanizer import send_human_like
            at_seg = f"[CQ:at,qq={user_id}]"
            site_url = _build_site_url(token)
            msg = (
                f"{at_seg} 欢迎入群~\n"
                f"请打开下面的链接完成验证（5分钟内）：\n\n"
                f"🌐 {site_url}\n\n"
                f"打开后点击「我是人类」按钮，\n"
                f"把看到的词发到群里告诉我\n"
                f"答对留下 :96:\n"
                f"答错3次踢出 :178:"
            )
            try:
                await send_human_like(bot, group_id, msg)
                logger.info(f"网站验证: 已向 {user_name}({user_id}) 群{group_id} 发链接 token={token[:_TOKEN_LOG_LEN]}...")
            except Exception as e:
                logger.error(f"群内发送网站验证链接失败: {e}")
                _cleanup_web_user(group_id, user_id)
                return True  # 已尝试接管（虽失败），避免再发欢迎制造噪音
            asyncio.create_task(_timeout_kick_web(bot, group_id, user_id, user_name, token))
            return True  # 网站模式已发链接，阻止 handler 发欢迎

    # AI 出题
    try:
        question, answer = await _generate_question()
    except Exception as e:
        logger.error(f"AI 出题失败: {e}")
        question, answer = _fallback_question()

    if not question:
        # 连备用题库都没了 —— 保守起见放人进群，不踢
        logger.warning(f"出题失败且无备用，{user_name}({user_id}) 直接放行")
        return True  # 已决策放行，handler 不应插手

    # 记录待验证
    _pending.setdefault(group_id, {})[user_id] = {
        "question": question,
        "answer": answer,
        "user_name": user_name,
        "created_at": time.time(),
        "wrong_attempts": 0,
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
        return True  # 已尝试接管（虽失败），避免再发欢迎

    # 启动超时任务
    asyncio.create_task(_timeout_kick(bot, group_id, user_id, user_name))
    return True  # 已出题，阻止 handler 发欢迎


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


async def _timeout_kick_web(bot, group_id: int, user_id: int, user_name: str, token: str):
    """网站认证超时自动踢出"""
    await asyncio.sleep(VERIFY_TIMEOUT)
    if not _web_pending.get(token):
        return  # 已处理
    logger.info(f"🌐 网站验证超时: {user_name}({user_id}) → 自动踢出")
    _log_verification(user_id, user_name, group_id, "timeout_web",
                      "", "", "超时未完成", method="web", attempts=0)
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
    _cleanup_web_token(token)


def _build_site_url(token: str) -> str:
    """构造新人验证链接。

    优先用环境变量 WEB_VERIFY_SITE；否则回退到固定域名 qqbot.crisxie.top。
    """
    site = os.getenv("WEB_VERIFY_SITE", "https://qqbot.crisxie.top")
    return f"{site.rstrip('/')}/v/{token}"


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

    # ── 先查网站认证 ──
    web_entry_exists = any(
        e["group_id"] == group_id and e["user_id"] == user_id
        for e in _web_pending.values()
    )
    if web_entry_exists:
        return await _handle_web_answer(bot, event, group_id, user_id, raw)

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

    from .humanizer import send_human_like
    at_seg = f"[CQ:at,qq={user_id}]"

    # 本地快速识别寒暄 → 不消耗尝试机会，提示后等真正作答
    if _looks_like_smalltalk(raw):
        logger.info(f"💬 验证忽略寒暄: {user_name}({user_id}) → \"{raw[:20]}\"")
        try:
            await send_human_like(
                bot, group_id,
                f"{at_seg} 请直接回答验证题哦~ 📝 {question}",
            )
        except Exception:
            pass
        return True

    # AI 判题
    try:
        verdict, reason = await _judge_answer(question, expected, raw)
    except Exception as e:
        logger.error(f"AI 判题失败: {e}")
        verdict = "correct" if _simple_match(expected, raw) else "wrong"
        reason = "简单匹配"

    if verdict == "correct":
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

    elif verdict == "invalid":
        # 不是答题（寒暄/闲聊/提问），不消耗尝试，温和提示
        logger.info(f"💬 验证忽略非答案: {user_name}({user_id}) → \"{raw[:20]}\" ({reason})")
        try:
            await send_human_like(
                bot, group_id,
                f"{at_seg} 请回答验证题~ 📝 {question}",
            )
        except Exception:
            pass
        return True

    else:  # wrong
        pending["wrong_attempts"] = pending.get("wrong_attempts", 0) + 1
        attempts = pending["wrong_attempts"]

        if attempts < MAX_WRONG_ATTEMPTS:
            remaining = MAX_WRONG_ATTEMPTS - attempts
            logger.info(f"❌ 验证答错({attempts}/{MAX_WRONG_ATTEMPTS}): {user_name}({user_id}) → \"{raw[:20]}\"")
            try:
                await send_human_like(
                    bot, group_id,
                    f"❌ {at_seg} 答错啦，还有 {remaining} 次机会~ 📝 {question}",
                )
            except Exception:
                pass
            return True  # 继续等待重试

        logger.info(f"❌ 验证失败: {user_name}({user_id}) → 踢出 ({reason}, {attempts}次错)")
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
        "你是QQ群加群验证的出题助手。生成一道题验证申请人是否真人。\n\n"
        "硬性要求（违反就是bug）：\n"
        "- 答案必须唯一且明确，全宇宙只有一种正确表述\n"
        "- 答案不超过5个字\n"
        "- 普通人5秒内能答对\n"
        "- 只允许以下题型：\n"
        "    * 简单算术（如 7+8、12-5）\n"
        "    * 公认常识（如首都、季节、一周几天、太阳升起方向）\n"
        "    * 常识性事实（动物的叫声、常见物品颜色）\n"
        "- 题目用中文\n\n"
        "严禁出现（这些会导致用户答对却被误判踢出）：\n"
        "- 脑筋急转弯、谜语、歇后语\n"
        "- 文学/影视/历史/神话人物相关问题（如猪八戒、李白相关）\n"
        "- 答案有多个合理表述的题（如\"水中月/倒影/影子\"）\n"
        "- 主观题、开放题、需要解释的题\n"
        "- 个人信息题（名字、年龄等）\n"
        f"{used_hint}\n\n"
        '返回纯JSON，不要Markdown代码块: {"question": "题目", "answer": "答案"}'
    )

    response = await llm_call.llm_create(
        model=ms.get("model", "gemini-3.5-flash-free"),
        max_tokens=150,
        temperature=0.6,
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


async def _judge_answer(question: str, expected: str, user_answer: str) -> tuple[str, str]:
    """
    AI 判题。返回 (verdict, reason)。
    verdict ∈ {"correct", "wrong", "invalid"}
      - correct: 答案正确
      - wrong:   是在答题，但答错了
      - invalid: 根本不是在答题（寒暄、闲聊、表情、提问等），不应消耗尝试机会
    """
    client = _get_client()
    if client is None:
        # 兜底：无法判别时用简单匹配，匹配不上算 wrong（保守踢人由重试次数兜底）
        return ("correct" if _simple_match(expected, user_answer) else "wrong"), "简单匹配"

    from .memory import load_model_settings
    ms = load_model_settings()

    system_prompt = (
        "你在判QQ群加群验证的答题。分三步：\n"
        "1. 先判断用户是否真的在回答这道题。如果是寒暄（你好/哈喽/大家好/谢谢）、"
        "闲聊、提问、求助、表情符号、与题目无关的话，verdict=\"invalid\"。\n"
        "2. 如果是在答题，判断答案是否与正确答案语义一致（表述可不同，如\"4\"和\"四\"都算对）。\n"
        "   对 → verdict=\"correct\"，错 → verdict=\"wrong\"。\n"
        "3. 不确定时倾向判 invalid（给用户重试机会），不要轻易判 wrong。\n\n"
        '返回纯JSON: {"verdict": "correct|wrong|invalid", "reason": "一句话说明"}'
    )

    response = await llm_call.llm_create(
        model=ms.get("model", "gemini-3.5-flash-free"),
        max_tokens=100,
        temperature=0.1,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"题目: {question}\n正确答案: {expected}\n用户说的: {user_answer}"
        }],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()

    result = json.loads(text)
    verdict = result.get("verdict", "wrong").strip().lower()
    if verdict not in ("correct", "wrong", "invalid"):
        verdict = "wrong"
    return verdict, result.get("reason", "")


def _looks_like_smalltalk(text: str) -> bool:
    """本地快速识别明显寒暄，避免每条都打 AI（省钱+省时）"""
    t = re.sub(r"\s", "", text).lower()
    if not t or len(t) > 12:
        return False
    # 纯表情/CQ 码
    if re.fullmatch(r"(\[cq:[^\]]+\]|:\d+:|[\U0001F300-\U0001FAFF\U00002600-\U000027BF])+", text, re.I):
        return True
    smalltalk = {
        "你好", "您好", "哈喽", "hello", "hi", "嗨", "大家好", "各位好",
        "谢谢", "感谢", "thanks", "thx", "多谢",
        "在吗", "有人吗", "有人", "在",
        "好的", " ok", "ok", "嗯", "哦", "额", "嗨喽",
        "新人", "报到", "冒泡", "进群了",
    }
    return t in smalltalk


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
    """清理超时的待验证记录（周期调用）。同时清两个 pending。"""
    now = time.time()
    # ── 出题模式 pending ──
    expired_groups: list[int] = []
    for gid, users in list(_pending.items()):
        expired_users = [uid for uid, v in users.items() if now - v.get("created_at", 0) > VERIFY_TIMEOUT]
        for uid in expired_users:
            users.pop(uid, None)
        if not users:
            expired_groups.append(gid)
    for gid in expired_groups:
        _pending.pop(gid, None)

    # ── 网站模式 pending ──
    expired_tokens = [
        t for t, e in _web_pending.items()
        if now - e.get("created_at", 0) > VERIFY_TIMEOUT
    ]
    for t in expired_tokens:
        _web_pending.pop(t, None)


# ═══════════════════════════════════════════════════════════
#  验证历史记录（供 Web 后台查看）
# ═══════════════════════════════════════════════════════════

_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "verify_history.json"
)
_MAX_HISTORY = 500


def _log_verification(user_id: int, user_name: str, group_id: int,
                      result: str, question: str, answer: str, reason: str,
                      method: str = "quiz", attempts: int = 1):
    """记录一条验证历史。

    method: "quiz"（AI出题）/ "web"（网站认证）/ "whitelist"（白名单放行）
    attempts: 尝试次数（通过=第几次对，踢出=总错次）
    """
    from datetime import datetime
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "user_name": user_name,
        "group_id": group_id,
        "result": result,       # "approved" | "rejected" | "timeout" | *_web / manual_approved
        "question": question[:50],
        "answer": answer[:30],
        "reason": reason[:100],
        "method": method,
        "attempts": attempts,
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


# ═══════════════════════════════════════════════════════════
#  免验证白名单 + 立即放行（Task 5 新增）
# ═══════════════════════════════════════════════════════════

# 白名单持久化文件（沿用 data/ 目录）
_WHITELIST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "verify_whitelist.json"
)

# 内存缓存: {"global": [int], "by_group": {str(group_id): [int]}}
_whitelist_cache: dict | None = None


def _load_whitelist() -> dict:
    """加载白名单（懒加载，损坏时重建）"""
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    default = {"global": [], "by_group": {}}
    if not os.path.exists(_WHITELIST_FILE):
        _whitelist_cache = default
        return _whitelist_cache
    try:
        with open(_WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = default
        data.setdefault("global", [])
        data.setdefault("by_group", {})
        _whitelist_cache = data
    except Exception as e:
        logger.warning(f"白名单文件损坏，重建: {e}")
        _whitelist_cache = default
        try:
            _save_whitelist()
        except Exception:
            pass
    return _whitelist_cache


def _save_whitelist():
    """写盘"""
    global _whitelist_cache
    if _whitelist_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_WHITELIST_FILE), exist_ok=True)
        with open(_WHITELIST_FILE, "w", encoding="utf-8") as f:
            json.dump(_whitelist_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"白名单写入失败: {e}")


def is_whitelisted(user_id: int, group_id: int) -> bool:
    """检查是否在免验证白名单（global 或 by_group[group_id]）"""
    data = _load_whitelist()
    if user_id in data.get("global", []):
        return True
    return user_id in data.get("by_group", {}).get(str(group_id), [])


def add_whitelist(user_id: int, group_id: int = 0) -> bool:
    """
    加入白名单。group_id=0 表示全局；非 0 表示指定群。
    Returns: True 表示新增；False 表示已存在。
    """
    data = _load_whitelist()
    if group_id == 0:
        gl = data.setdefault("global", [])
        if user_id in gl:
            return False
        gl.append(user_id)
    else:
        key = str(group_id)
        lst = data.setdefault("by_group", {}).setdefault(key, [])
        if user_id in lst:
            return False
        lst.append(user_id)
    _save_whitelist()
    logger.info(f"免验证白名单 += {user_id} (group={group_id})")
    return True


def remove_whitelist(user_id: int, group_id: int = 0) -> bool:
    """从白名单移除。Returns: True 表示有移除；False 表示本来就不在。"""
    data = _load_whitelist()
    removed = False
    if group_id == 0:
        # 从 global 移除 + 所有 by_group 移除（彻底清）
        if user_id in data.get("global", []):
            data["global"].remove(user_id)
            removed = True
        for k, lst in data.get("by_group", {}).items():
            if user_id in lst:
                lst.remove(user_id)
                removed = True
    else:
        key = str(group_id)
        lst = data.get("by_group", {}).get(key, [])
        if user_id in lst:
            lst.remove(user_id)
            removed = True
    if removed:
        _save_whitelist()
        logger.info(f"免验证白名单 -= {user_id} (group={group_id})")
    return removed


def list_whitelist(group_id: int = 0) -> dict:
    """返回白名单字典。group_id 非 0 时附加该群的列表。"""
    data = _load_whitelist()
    result = {"global": list(data.get("global", [])),
              "by_group": dict(data.get("by_group", {}))}
    if group_id:
        result["current_group"] = list(data.get("by_group", {}).get(str(group_id), []))
    return result


async def approve_pending(bot, group_id: int, user_id: int) -> bool:
    """
    立即放行某个正在待验证的用户（两种模式都支持）。
    清理 _pending 和 _web_pending，记录 manual_approved，发欢迎消息 + 加积分。
    Returns: True 表示该用户在待验证中并已被放行；False 表示没找到。
    """
    # ── 先查网站模式 ──
    web_token = None
    web_entry = None
    for t, e in _web_pending.items():
        if e["group_id"] == group_id and e["user_id"] == user_id:
            web_token = t
            web_entry = e
            break

    # ── 再查出题模式 ──
    pending = _pending.get(group_id, {}).get(user_id)

    if not web_entry and not pending:
        return False

    user_name = (web_entry or pending).get("user_name", str(user_id))
    question = (pending or {}).get("question", "") or (web_entry or {}).get("code", "")

    # 清两边
    if web_token:
        _cleanup_web_token(web_token)
    if pending:
        _cleanup_user(group_id, user_id)

    method = "web" if web_entry else "quiz"
    _log_verification(user_id, user_name, group_id, "manual_approved",
                      question, "", "主人手动放行", method=method, attempts=0)
    logger.info(f"主人放行: {user_name}({user_id}) 群{group_id} ({method})")
    try:
        from .humanizer import send_human_like
        from . import welcome as _welcome
        await send_human_like(bot, group_id, f"✅ [CQ:at,qq={user_id}] 主人已放行，欢迎入群~ :96:")
        welcome_msg = _welcome.get_welcome_message(group_id, user_name, user_id)
        if welcome_msg:
            await send_human_like(bot, group_id, welcome_msg)
        _welcome.add_points(user_id, group_id, user_name, 10, "主人放行")
    except Exception as e:
        logger.warning(f"放行后发欢迎/加积分失败: {e}")
    return True
