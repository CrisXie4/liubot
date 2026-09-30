"""
统一 LLM 调用包装 — 429 重试 + 延迟补发队列 + 全局限速
=====================================================
所有 client.messages.create 都应改走 llm_create，获得：
  - 全局令牌桶限速（13/分钟，避免撞网关 15/min 红线）
  - 4 级优先级（P0 用户在等 / P1 vision / P2 后台 / P3 可跳过）
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
import re
import time
from collections import deque
from typing import Optional

from . import memory
from .config import config
from . import rate_limiter

logger = logging.getLogger("qqbot")

# ═══════════════════════════════════════════════════════════
#  共享客户端（多 provider 轮询）
# ═══════════════════════════════════════════════════════════

# 每个 provider 一个独立 client，按 hash 缓存
_clients: dict[str, object] = {}
# 轮询计数器（round-robin）
_round_robin_idx = 0


def _get_or_create_client(api_key: str, api_url: str):
    """获取/创建一个 provider 的客户端（按 key+url 缓存）。"""
    h = f"{api_key}:{api_url}"
    if h not in _clients:
        from anthropic import AsyncAnthropic
        _clients[h] = AsyncAnthropic(api_key=api_key, base_url=api_url)
    return _clients[h]


def get_client():
    """获取默认 provider 的客户端（向后兼容）。

    无 api_key 返回 None。各调用方应检查 None。
    多 provider 轮询请直接用 _get_providers() + _get_or_create_client()。
    """
    ms = memory.load_model_settings()
    if not ms.get("api_key"):
        return None
    return _get_or_create_client(ms["api_key"], ms["api_url"])


def _get_providers() -> list[dict]:
    """获取所有可用 provider 列表。每个 provider:
    {api_url, api_key, model, supports_vision}
    """
    ms = memory.load_model_settings()
    return [p for p in ms.get("providers", []) if p.get("api_key") and p.get("model")]


def _pick_provider(needs_vision: bool = False) -> tuple[dict, object] | None:
    """轮询选择下一个 provider（round-robin）。

    needs_vision=True 时，优先选 supports_vision=True 的 provider。
    返回 (provider_dict, client) 或 None（无可用 provider）。
    """
    global _round_robin_idx
    providers = _get_providers()
    if not providers:
        return None

    if needs_vision:
        # 优先选支持 vision 的
        vision_providers = [p for p in providers if p.get("supports_vision")]
        pool = vision_providers if vision_providers else providers
    else:
        pool = providers

    # round-robin
    idx = _round_robin_idx % len(pool)
    _round_robin_idx += 1
    provider = pool[idx]
    client = _get_or_create_client(provider["api_key"], provider["api_url"])
    return provider, client


# ═══════════════════════════════════════════════════════════
#  重试包装
# ═══════════════════════════════════════════════════════════

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 前 5 次退避：5s→15s→40s→60s→60s（约 3 分钟）
RETRY_DELAYS = [5, 15, 40, 60, 60]
# 重试耗尽后的循环间隔（fallback 无限重试时用）
RETRY_LOOP_DELAY = 60


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


def _is_context_overflow(exc) -> bool:
    """上下文超限错误（provider 特有，非调用方错误）。

    llama.cpp 返回 400 exceed_context_size_error（n_ctx 装不下请求），
    OpenAI 系网关返回 context_length_exceeded / maximum context length。
    这只说明"当前 provider 的窗口装不下"，重试无意义也不该中断整个
    调用——切到下一个 provider（窗口更大的）才有机会成功。
    """
    msg = str(exc).lower()
    return any(k in msg for k in (
        "exceed_context_size", "exceeds the available context",
        "context_length_exceeded", "maximum context length",
        "too many tokens",
    ))


def _is_empty_response(response) -> bool:
    """检查 LLM 是否返回了空内容（HTTP 200 + 合法 JSON，但没生成任何文本）。

    这种故障不会抛异常，会静默穿透所有重试/failover 逻辑，导致 Bot 完全不回话。
    检测到后视为 provider 故障，触发切换到下一个 provider。
    注意：含 tool_use block 的响应是合法响应（AI 想调工具），
    不能算空——否则工具调用会被误判成故障，failover 死循环挂死 P0 调用。
    """
    try:
        for block in (response.content or []):
            if getattr(block, "type", "") == "tool_use":
                return False  # 工具调用请求，合法
            text = getattr(block, "text", None)
            if text and text.strip():
                return False
        return True
    except Exception:
        return False


def _strip_thinking_blocks(response):
    """剥离 thinking block（backup provider 的 extended thinking 会扰乱下游 content[0].text）。

    claude-opus-5 等推理模型默认返回 content=[{type:"thinking",...}, {type:"text",...}]，
    导致代码库各处 response.content[0].text 取到的是 thinking block（无 text 属性）。
    这里统一把 thinking block 移除，让 content[0] 恒为正文 text block。
    """
    try:
        content = list(response.content or [])
        non_thinking = [b for b in content if getattr(b, "type", "") != "thinking"]
        if non_thinking and len(non_thinking) < len(content):
            response.content = non_thinking
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  正文内混入的思维链（2026-09-30 发疯事故）
# ═══════════════════════════════════════════════════════════
# 部分中转站（flowbit 等）会把推理模型的思考过程当纯正文塞进 text block
# ——没有 type:"thinking" block，_strip_thinking_blocks 拦不住，2452 字英文
# 推理直接发进群。两层防御：
#   1) 剥 <think>/<thinking>/<reasoning> 标签块（含未闭合剥到末尾），
#      剥完为空 → 视为空响应 failover；
#   2) 无标签的裸思考（"Let me look at the current situation... Wait,
#      Actually..."）用话语标记启发式识别，命中 → failover 换渠道重答。

_INLINE_THINK_PAIR_RE = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1\s*>", re.DOTALL | re.IGNORECASE
)
_INLINE_THINK_OPEN_RE = re.compile(
    r"<(think|thinking|reasoning)>.*\Z", re.DOTALL | re.IGNORECASE
)


def _strip_inline_thinking(response) -> bool:
    """剥掉 text block 里以标签包裹的思维链。返回是否有改动。"""
    changed = False
    try:
        for block in (response.content or []):
            if getattr(block, "type", "") != "text":
                continue
            text = getattr(block, "text", None)
            if not text or "<" not in text:
                continue
            cleaned = _INLINE_THINK_PAIR_RE.sub("", text)
            cleaned = _INLINE_THINK_OPEN_RE.sub("", cleaned)
            if cleaned != text:
                block.text = cleaned
                changed = True
    except Exception:
        pass
    return changed


# 裸思考的开场白：正文开头（跳过 CQ 码）直接就是推理话语，正常聊天回复不会这样说话
_REASONING_OPENER_RE = re.compile(
    r"^(?:\s|\[CQ:[^\]]*\])*"
    r"(?:Let me\b|Okay,|Hmm[,.]|Wait[,.]|Alright,|Looking at\b"
    r"|I need to\b|I should\b|First[,.]|On second thought)"
)
# 推理过程中反复出现的话语标记，窗口内共现 ≥3 个才判泄漏（防误伤正常英文回复）
_REASONING_MARKER_RE = re.compile(
    r"\b(?:Let me|Wait|Actually|Hmm|I need to|I should"
    r"|let me re-?read|look(?:ing)? at|my previous|the user|wait,)\b",
    re.IGNORECASE,
)
_REASONING_MIN_LEN = 300      # 短于这个长度的正常英文回复不判
_REASONING_WINDOW = 800       # 标记共现检查窗口（前 800 字符足够判断）


def _response_text(response) -> str:
    """拼接所有 text block（判泄漏用）。"""
    parts = []
    try:
        for block in (response.content or []):
            if getattr(block, "type", "") == "text":
                t = getattr(block, "text", "") or ""
                parts.append(t)
    except Exception:
        pass
    return "\n".join(parts)


def _looks_like_leaked_reasoning(text: str) -> bool:
    """识别无标签的裸思维链：英文推理开场白 + 窗口内多个话语标记共现。

    只抓英文（本次事故形态）；中文开场白（"嗯"/"让我看看"）与正常口语
    重叠太多，误伤率不划算，交给人设约束兜底。
    """
    if len(text) < _REASONING_MIN_LEN:
        return False
    if not _REASONING_OPENER_RE.match(text):
        return False
    window = text[:_REASONING_WINDOW]
    return len(set(_REASONING_MARKER_RE.findall(window))) >= 3


# ═══════════════════════════════════════════════════════════
#  思考模式（DeepSeek effort 映射）
# ═══════════════════════════════════════════════════════════

# provider 可声明 thinking_effort（low/medium/high/xhigh/max），
# 经 output_config={"effort": ...} 传给 Anthropic 兼容端点（DeepSeek 官方文档：
# output_config 仅支持 effort；不要同时传 thinking/reasoning_effort，会冲突报错）。
# 服务端实际映射：low→low, medium→high, high→high, xhigh→high, max→max，
# 客户端先做同样映射，让日志里记录的就是实际生效档位。
EFFORT_MAP = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}
# Anthropic 协议下思考 token 计入 max_tokens；聊天默认 max_tokens=800 会被
# 思考吃光，剥掉 thinking block 后变空响应 → 误判 provider 故障引发 failover。
# 2000 实测不够（2026-09-30 thinking_effort=high 一轮思考就吃光了，空响应
# 触发 failover 落到会泄漏思维链的中转渠道），保底抬到 4000。
THINKING_MIN_MAX_TOKENS = 4000


def _apply_thinking(call_kwargs: dict, provider: dict):
    """给声明了 thinking_effort 的 provider 注入 effort 与 max_tokens 保底。

    effort 经 extra_body 的 output_config 传递（Anthropic 官方参数），
    不用 SDK 的类型化关键字参数——服务器等环境的 anthropic SDK 可能较老
    （实测 0.40.0 不认识 output_config 参数会直接 TypeError），extra_body
    各版本都支持，请求体里的 JSON 字段是同一个。
    """
    effort = EFFORT_MAP.get(str(provider.get("thinking_effort") or "").lower())
    if not effort:
        return
    mt = call_kwargs.get("max_tokens") or THINKING_MIN_MAX_TOKENS
    call_kwargs["max_tokens"] = max(int(mt), THINKING_MIN_MAX_TOKENS)
    extra = dict(call_kwargs.get("extra_body") or {})
    extra["output_config"] = {"effort": effort}
    call_kwargs["extra_body"] = extra


class RateLimitSkipped(Exception):
    """P3 调用被限流跳过（不是错误，上层应静默返回 None）。"""


async def llm_create(**kwargs):
    """包装 client.messages.create，带多 provider failover + 全局限速 + 429/5xx 重试。

    参数:
        **kwargs: 透传给 client.messages.create
            额外支持 priority="P2"（关键字参数，不透传给 SDK）:
                P0 — 用户在等回复（chat_reply 等）
                P1 — vision 图片理解
                P2 — 后台思考（默认）
                P3 — 可跳过任务

    多 provider failover（主备，非轮询）:
      - providers[0]（默认 cccode）永远是主渠道，每次都先试它
      - 主渠道鉴权失败/重试耗尽 → 才 fall back 到下一个 provider（备胎）
      - 每个 provider 有自己的 api_key/api_url/model
      - 调用方传的 model 会被 provider 自己的 model 覆盖
      - 图片类调用（有 image content）只选 supports_vision 的 provider

    - 先取令牌（排队等到拿到为止，不超时不跳过）
    - 当前 provider 429/5xx：重试 5 次（5s→15s→40s→60s→60s）
    - 5 次仍失败 → 切到下一个 provider 继续重试
    - 所有 provider 都试完一轮 → 循环重来，直到成功（429 永不放弃）
    - 鉴权错误（401/403）立即切换下一 provider（不浪费时间重试坏 key）
    """
    # 弹出 priority（不透传给 SDK）
    priority = kwargs.pop("priority", "P2")
    if priority not in rate_limiter.PRIORITY_TIMEOUT:
        priority = "P2"

    # 取令牌（一直等到拿到为止）
    await rate_limiter.acquire_token(priority)

    # 判断是否需要 vision（messages 里有 image content）
    needs_vision = _kwargs_has_image(kwargs)

    providers = _get_providers()
    if not providers:
        raise RuntimeError("LLM 无可用 provider（未配置 api_key）")

    # 按 needs_vision 过滤候选 provider
    if needs_vision:
        candidates = [p for p in providers if p.get("supports_vision")] or providers
    else:
        candidates = providers

    # failover 起点永远从 index 0（首选 provider = cccode）开始：
    # 主渠道优先，只有主渠道鉴权失败/重试耗尽才 fall back 到下一个。
    # candidates[0] 就是 _get_providers() 返回的 default_provider（见 memory.load_model_settings）。
    global _round_robin_idx
    start_idx = 0

    # 依次尝试每个 provider，失败切下一个；全轮一遍后继续循环
    provider_loop_count = 0
    while True:
        overflow_count = 0
        permanent_failures: list[Exception] = []
        for offset in range(len(candidates)):
            provider = candidates[(start_idx + offset) % len(candidates)]
            client = _get_or_create_client(provider["api_key"], provider["api_url"])
            # 用 provider 自己的 model 覆盖
            call_kwargs = dict(kwargs)
            call_kwargs["model"] = provider["model"]
            # 声明了 thinking_effort 的渠道：注入 effort + max_tokens 保底
            _apply_thinking(call_kwargs, provider)

            try:
                response = await _call_with_retries(
                    client, max_retries=len(RETRY_DELAYS), **call_kwargs
                )
            except Exception as exc:
                if _is_auth_error(exc):
                    # 鉴权错误：这个 provider 的 key 有问题，记日志，试下一个
                    logger.warning(
                        f"provider {provider['api_url']} 鉴权失败，跳过: {_short_exc(exc)}"
                    )
                    continue
                if _is_context_overflow(exc):
                    # 上下文超限：这个 provider 窗口装不下，试下一个
                    overflow_count += 1
                    logger.warning(
                        f"provider {provider['model']}@{provider['api_url']} 上下文超限，"
                        f"切下一个: {_short_exc(exc)}"
                    )
                    continue
                if not _is_retryable(exc):
                    # 永久性错误（404 端点没了 / 网关返回 HTML 错误页 / 400 参数被拒）：
                    # 只说明这个渠道坏了，切下一个；整轮全非暂时性失败才抛出。
                    # （实例：d1kt 的 frp 隧道断开时 /v1/messages 返回 frp 404 页，
                    #   旧逻辑直接 raise，独占模式下整条链路瞬间哑掉。）
                    permanent_failures.append(exc)
                    logger.warning(
                        f"provider {provider['model']}@{provider['api_url']} "
                        f"永久性错误，切下一个: {_short_exc(exc)}"
                    )
                    continue
                # 可重试错误（5 次重试耗尽）→ 切下一个 provider
                logger.warning(
                    f"provider {provider['model']}@{provider['api_url']} 重试耗尽，切下一个: "
                    f"{_short_exc(exc)}"
                )
                continue

            # 空内容检测：200 OK 但生成 0 token → 视为该 provider 故障，切下一个
            # 不重试同一个 provider（模型/网关持续性故障，重试无用），直接 failover
            if _is_empty_response(response):
                logger.warning(
                    f"provider {provider['model']}@{provider['api_url']} 返回空内容"
                    f"（stop_reason={getattr(response, 'stop_reason', '?')}），"
                    f"切下一个 provider"
                )
                continue

            # 剥离 thinking block：让下游 content[0].text 恒取到正文
            _strip_thinking_blocks(response)
            # 剥正文里以 <think> 等标签包裹的思维链（中转站拼进正文的形态）
            if _strip_inline_thinking(response) and _is_empty_response(response):
                logger.warning(
                    f"provider {provider['model']}@{provider['api_url']} "
                    f"整条响应全是思维链（剥离后为空），切下一个 provider"
                )
                continue
            # 无标签的裸思维链（英文推理腔）：同样视为该渠道故障，换渠道重答
            leaked = _looks_like_leaked_reasoning(_response_text(response))
            if leaked:
                logger.warning(
                    f"provider {provider['model']}@{provider['api_url']} "
                    f"疑似思维链泄漏（裸推理文本），切下一个 provider"
                )
                continue

            # 📊 每小时用量统计（面板图表）+ 缓存命中率滚动日志。统计失败绝不影响调用。
            try:
                from . import db as _db
                u = getattr(response, "usage", None)
                if u is not None:
                    _db.usage_hour_add(
                        tokens_in=getattr(u, "input_tokens", 0) or 0,
                        tokens_cached=getattr(u, "cache_read_input_tokens", 0) or 0,
                        tokens_out=getattr(u, "output_tokens", 0) or 0,
                        llm_calls=1,
                    )
                    _note_cache_hit_rate(u)
            except Exception:
                pass

            return response

        total_permanent = len(permanent_failures) + overflow_count
        if total_permanent == len(candidates) and total_permanent > 0:
            # 整轮全是永久性错误（渠道全挂 / 窗口全装不下）：重试无意义，
            # 抛出让调用方处理——chat 会把请求暂存待发队列，渠道恢复后自动补发。
            if overflow_count == len(candidates):
                raise RuntimeError(
                    f"全部 {len(candidates)} 个 provider 上下文超限"
                    "（对话超过渠道窗口，请减小聊天上下文上限或换大窗口渠道）"
                )
            # 混合（404+超限等）或纯渠道故障：抛最后一个永久性错误，日志里最完整
            raise permanent_failures[-1] if permanent_failures else RuntimeError(
                f"全部 {len(candidates)} 个 provider 上下文超限"
            )

        # 所有 provider 都试了一遍，全失败 → 等 60s 再循环
        provider_loop_count += 1
        logger.warning(
            f"所有 {len(candidates)} 个 provider 均失败（第 {provider_loop_count} 轮），"
            f"60s 后重新开始"
        )
        await asyncio.sleep(RETRY_LOOP_DELAY)
        # 下一轮仍从首选 provider 重新开始（主渠道优先）
        start_idx = 0


def _kwargs_has_image(kwargs: dict) -> bool:
    """检查调用参数里是否包含图片内容（用于选 vision provider）。"""
    messages = kwargs.get("messages", [])
    if not isinstance(messages, list):
        return False
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image",):
                    return True
    return False


# ═══════════════════════════════════════════════════════════
#  缓存命中率滚动统计
# ═══════════════════════════════════════════════════════════
#  前缀缓存命中 ≈ 1 折输入价。滚动记录近 N 次调用的命中比例，每 50 次
#  调用打一条日志，直接观察优化效果（面板小时图表也有 tokens_cached 列）。

_cache_window: deque = deque(maxlen=50)
_cache_window_n = 0


def _note_cache_hit_rate(u) -> None:
    global _cache_window_n
    try:
        uncached = getattr(u, "input_tokens", 0) or 0
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        total = uncached + cached + write
        if total <= 0:
            return
        _cache_window.append((total, cached))
        _cache_window_n += 1
        if _cache_window_n % 50 == 0:
            t = sum(x for x, _ in _cache_window)
            c = sum(y for _, y in _cache_window)
            if t:
                logger.info(
                    f"💰 近{len(_cache_window)}次LLM调用前缀缓存命中率 "
                    f"{c / t * 100:.0f}%（命中 {c} / 总输入 {t} tokens）"
                )
    except Exception:
        pass


async def _call_with_retries(client, *, max_retries: int | None = None, **kwargs):
    """单模型的退避重试调用。

    max_retries:
        None — 无限重试（用于 fallback：429/5xx 永不放弃，每 60s 重试一次）
        N    — 最多重试 N 次（用于主模型：耗尽后切 fallback）
    """
    last_exc = None
    attempt = 0
    # 首次不延迟
    delays = [0] + RETRY_DELAYS

    while True:
        delay = delays[attempt] if attempt < len(delays) else RETRY_LOOP_DELAY
        if delay > 0:
            tag = f"{attempt}/{max_retries}" if max_retries else f"#{attempt}"
            logger.warning(
                f"LLM 调用重试 {tag}，{delay}s 后重试"
                + (f"（{_short_exc(last_exc)}）" if last_exc else "")
            )
            await asyncio.sleep(delay)
        try:
            return await client.messages.create(**kwargs)
        except Exception as e:
            last_exc = e
            if not _is_retryable(e):
                # 不可重试（400/401/403 等）→ 直接抛出
                raise
            attempt += 1
            if max_retries is not None and attempt > max_retries:
                # 主模型重试耗尽 → 抛出（让上层切 fallback）
                raise


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
                sent_bubbles = await humanizer.send_human_like(bot, gid, reply)
                if sent_bubbles:
                    sent += 1
                else:
                    # 生成成功但发送失败（可能 NapCat 抖动），保留到下次重试
                    kept.append(item)
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
