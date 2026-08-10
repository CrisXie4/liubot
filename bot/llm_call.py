"""
统一 LLM 调用包装 — 429 重试 + 延迟补发队列
================================================
所有 client.messages.create 都应改走 llm_create，获得：
  - 429/5xx/超时/连接错误的指数退避重试（5s→15s→40s，最多3次）
  - 统一的共享客户端（设置变更时自动重建）

用户可见的主回复路径（chat.chat_reply）额外使用延迟补发队列：
  - 重试耗尽仍 429 → 回复存入 pending_replies.json
  - consciousness_loop 每分钟调 flush_pending_retries 尝试补发
  - 超过 1 小时的待发回复丢弃（过时了发出去反而怪）
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

from . import memory
from .config import config

logger = logging.getLogger("qqbot")

# ═══════════════════════════════════════════════════════════
#  共享客户端
# ═══════════════════════════════════════════════════════════

_client = None
_client_hash: str = ""


def get_client():
    """获取共享 Anthropic 客户端（设置变更时自动重建）。

    无 api_key 返回 None。各调用方应检查 None。
    """
    global _client, _client_hash
    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return None

    h = f"{ms['api_key']}:{ms['api_url']}"
    if _client is None or _client_hash != h:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=ms["api_key"], base_url=ms["api_url"])
        _client_hash = h
    return _client


# ═══════════════════════════════════════════════════════════
#  重试包装
# ═══════════════════════════════════════════════════════════

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = [5, 15, 40]  # 秒，指数退避


def _is_retryable(exc) -> bool:
    """判断异常是否值得重试（429/5xx/超时/连接错误）。"""
    status = getattr(exc, "status_code", None)
    if status is not None and status in _RETRYABLE_STATUS:
        return True
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError", "ReadTimeout",
                "ConnectTimeout", "TimeoutError", "ConnectionError"):
        return True
    # 字符串兜底（不同 SDK 版本异常类名不同）
    msg = str(exc).lower()
    if any(k in msg for k in ("429", "rate limit", "rate_limit",
                              "timeout", "timed out", "connection",
                              "overloaded", "service unavailable")):
        return True
    return False


def _is_auth_error(exc) -> bool:
    """鉴权/权限错误：换模型也救不了，不应该触发 fallback。"""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return True
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return True
    msg = str(exc).lower()
    if any(k in msg for k in ("unauthorized", "forbidden", "invalid api key",
                              "invalid_api_key", "authentication")):
        return True
    return False


async def llm_create(**kwargs):
    """包装 client.messages.create，带 429/5xx 重试 + 模型降级。

    - 429/5xx/超时/连接错误：指数退避重试（5s→15s→40s，最多3次）
    - 主模型彻底失败（重试耗尽或非可重试错误）→ 自动切换 fallback_model
    - fallback 也失败 → 抛出 fallback 的异常

    用户可见的主回复路径（chat.chat_reply）额外使用延迟补发队列：
      - 重试耗尽仍 429 → 回复存入 pending_replies.json
      - consciousness_loop 每分钟调 flush_pending_retries 尝试补发
      - 超过 1 小时的待发回复丢弃（过时了发出去反而怪）
    """
    client = get_client()
    if client is None:
        raise RuntimeError("LLM client unavailable (no api_key)")

    primary_model = kwargs.get("model")
    ms = memory.load_model_settings()
    fallback_model = ms.get("fallback_model") or ""

    # 主模型调用（含重试）
    try:
        return await _call_with_retries(client, **kwargs)
    except Exception as primary_exc:
        # 没有 fallback、fallback 与主模型相同、或是鉴权错误（换模型也救不了）→ 直接抛
        if not fallback_model or fallback_model == primary_model:
            raise
        if _is_auth_error(primary_exc):
            raise
        # 主模型不可用 → 降级到 fallback
        logger.warning(
            f"主模型 {primary_model} 调用失败，降级到 {fallback_model}: "
            f"{_short_exc(primary_exc)}"
        )
        fb_kwargs = dict(kwargs)
        fb_kwargs["model"] = fallback_model
        # fallback 也走完整重试，再失败就抛它自己的异常
        return await _call_with_retries(client, **fb_kwargs)


async def _call_with_retries(client, **kwargs):
    """单模型的指数退避重试调用。"""
    last_exc = None
    attempts = [0] + RETRY_DELAYS  # 首次不延迟
    for attempt, delay in enumerate(attempts):
        if delay > 0:
            logger.warning(
                f"LLM 调用重试 {attempt}/{len(RETRY_DELAYS)}，{delay}s 后重试"
                + (f"（{_short_exc(last_exc)}）" if last_exc else "")
            )
            await asyncio.sleep(delay)
        try:
            return await client.messages.create(**kwargs)
        except Exception as e:
            last_exc = e
            # 可重试且还有重试机会 → 继续
            if _is_retryable(e) and attempt < len(RETRY_DELAYS):
                continue
            # 不可重试，或重试耗尽 → 抛出
            raise
    raise last_exc  # 理论上到不了


def _short_exc(exc) -> str:
    if exc is None:
        return ""
    s = str(exc).replace("\n", " ").strip()
    return s[:80] if s else type(exc).__name__


# ═══════════════════════════════════════════════════════════
#  延迟补发队列（仅用于用户可见的主回复）
# ═══════════════════════════════════════════════════════════
#
#  注意：429 发生在"生成"阶段（调 LLM），不在"发送"阶段（发 QQ 消息）。
#  所以队列存的是"生成请求"（group_id/user_qq/message...），
#  补发时重新调 chat_reply 生成再发送。

PENDING_PATH = os.path.join(config.DATA_DIR, "pending_replies.json")
PENDING_TTL = 3600  # 1 小时后丢弃（过时的回复发出去反而怪）


def enqueue_pending_reply(group_id: int, user_qq: int, user_name: str,
                          message: str, message_id: int = 0):
    """把因 429 暂时生成不了的回复请求存起来，等后台定时器重试生成。"""
    if not message:
        return
    try:
        data = []
        if os.path.exists(PENDING_PATH):
            with open(PENDING_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        # 去重：同一群同一用户最近的相同消息不重复入队
        data = [d for d in data
                if not (d.get("group_id") == group_id
                        and d.get("user_qq") == user_qq
                        and d.get("message") == message)]
        data.append({
            "group_id": int(group_id),
            "user_qq": int(user_qq),
            "user_name": user_name or "",
            "message": message[:500],
            "message_id": int(message_id),
            "ts": time.time(),
        })
        os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"回复请求暂存待生成 [群{group_id} {user_name}]: {message[:40]}...")
    except Exception as e:
        logger.error(f"暂存回复请求失败: {e}")


async def flush_pending_replies(bot):
    """后台定时调用（建议每分钟）：尝试重新生成并补发暂存的回复。

    成功一条删一条；超时(>1h)丢弃；仍失败的保留到下次。
    """
    if not os.path.exists(PENDING_PATH):
        return
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not data:
        return

    # 延迟导入避免循环依赖
    from . import chat, humanizer

    now = time.time()
    kept: list[dict] = []
    sent = 0
    for item in data:
        age = now - item.get("ts", 0)
        if age > PENDING_TTL:
            logger.info(f"丢弃超时待生成回复: {item.get('message', '')[:40]}")
            continue
        gid = int(item.get("group_id", 0))
        if not gid:
            continue
        try:
            reply = await chat.chat_reply(
                gid,
                int(item.get("user_qq", 0)),
                item.get("user_name", ""),
                item.get("message", ""),
                message_id=int(item.get("message_id", 0)),
            )
            if reply:
                await humanizer.send_human_like(bot, gid, reply)
                sent += 1
            else:
                # 生成仍失败（可能还在 429），保留到下次
                kept.append(item)
        except Exception as e:
            logger.debug(f"补发生成仍失败，保留: {_short_exc(e)}")
            kept.append(item)

    try:
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    if sent:
        logger.info(f"补发生成了 {sent} 条暂存回复")
