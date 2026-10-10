"""
全局 LLM 调用令牌桶限速器
========================

背景:
  API 网关限制 15 次/分钟（含失败重试）。无控制地并发调用会撞墙，
  撞墙后的重试又消耗额度，造成 429 雪崩——vision 永远失败、图片读不了、
  主动行为生成不了。

策略:
  - 令牌桶: 容量 13（留 2 个余量给网关红线），每 60 秒补满
  - 超额调用排队等待，不抛 429
  - 4 级优先级: P0(用户在等) > P1(vision) > P2(后台思考) > P3(可跳过)
  - P3 在令牌紧张时直接跳过（返回 False），不进入队列

使用:
  if await acquire_token("P1"):
      resp = await llm_create(...)
  else:
      # P3 被跳过了
      return None
"""
import asyncio
import logging
import time

logger = logging.getLogger("qqbot")

# ═══════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════

BUCKET_CAPACITY = 13          # 桶容量（网关 15/min 红线，留 2 余量）
BUCKET_REFILL_SECS = 60       # 补满周期
P3_MIN_RESERVE = 3            # 桶里少于这个数时 P3 直接跳过

# 各优先级信息（用于日志/统计，不再有超时——全部等到拿到令牌为止）
PRIORITY_TIMEOUT = {
    "P0": None,   # 用户在等回复，无限等
    "P1": None,   # vision 图片理解，无限等
    "P2": None,   # 后台思考，无限等
    "P3": None,   # 可跳过任务，也无限等（429 就等到不 429）
}

# 轮询间隔（等待时多久重试一次取令牌）
_POLL_INTERVAL = 0.5

# ═══════════════════════════════════════════════════════════
#  令牌桶状态
# ═══════════════════════════════════════════════════════════

_tokens: float = BUCKET_CAPACITY
_last_refill: float = time.monotonic()
_lock = asyncio.Lock()

# 统计（供 Web UI / 日志观察）
_stats = {
    "total_calls": 0,
    "acquired": 0,
    "skipped_p3": 0,
    "timed_out": 0,
}


def _refill_locked():
    """在持有 _lock 时调用：补满令牌。"""
    global _tokens, _last_refill
    now = time.monotonic()
    elapsed = now - _last_refill
    if elapsed >= BUCKET_REFILL_SECS:
        _tokens = BUCKET_CAPACITY
        _last_refill = now


def _try_take_locked(priority: str) -> bool:
    """在持有 _lock 时调用：尝试取一个令牌。返回是否成功。"""
    global _tokens
    _refill_locked()
    if _tokens >= 1:
        _tokens -= 1
        return True
    return False


async def acquire_token(priority: str = "P2") -> bool:
    """获取一个调用令牌。一直等到拿到为止（不超时、不跳过）。

    遇到限流就排队等，等到有令牌再返回。永远返回 True。
    priority 参数仅用于日志和统计。
    """
    _stats["total_calls"] += 1

    # 等待循环：一直等到拿到令牌
    while True:
        async with _lock:
            if _try_take_locked(priority):
                _stats["acquired"] += 1
                return True
        # 算一下下一个令牌大概什么时候补
        async with _lock:
            wait_for = (_last_refill + BUCKET_REFILL_SECS) - time.monotonic()
        await asyncio.sleep(min(_POLL_INTERVAL, max(wait_for, 0.1)))


def get_stats() -> dict:
    """返回当前限流统计（供观察）。"""
    return {
        **_stats,
        "current_tokens": _tokens,
        "capacity": BUCKET_CAPACITY,
    }


def reset_stats():
    """重置统计计数器（不影响令牌桶本身）。"""
    for k in _stats:
        _stats[k] = 0


def _set_tokens_for_test(n: float):
    """仅用于测试：强制设置当前令牌数。生产代码不要调用。"""
    global _tokens, _last_refill
    _tokens = n
    _last_refill = time.monotonic()
