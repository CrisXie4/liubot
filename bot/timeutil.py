"""
权威上海时间源
==============
从国内/国际 HTTP 源拉 Date 头，算系统时钟与权威时间的偏移，
保证部署到错误 TZ/漂移的机器上 bot 仍然知道真实上海时间。

启动时 + 每 30min 同步一次。每次从 10 个源里随机抽 3 个并发测，
取多数一致值。全失败则保留上次偏移；从未成功过则退回系统时钟。
"""
import asyncio
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 10 个 HTTP 时间源（返回 GMT Date 头，+8 = 上海时间）
_TIME_SOURCES = [
    "https://www.baidu.com",
    "https://www.qq.com",
    "https://www.taobao.com",
    "https://www.microsoft.com",
    "https://www.apple.com",
    "https://www.bing.com",
    "https://github.com",
    "https://www.cloudflare.com",
    "https://www.alibaba.com",
    "https://www.sohu.com",
]

_SYNC_INTERVAL = 1800          # 30 分钟
_SAMPLE_SIZE = 3               # 每次随机抽 3 个
_SOURCE_TIMEOUT = 8            # 单源超时 8s
_MAX_TOLERANCE = timedelta(seconds=2)  # 源间最大允许偏差；超差则放弃本次

# 全局状态
_offset: Optional[timedelta] = None     # authoritative - system；None=未同步过
_last_sync_ts: float = 0.0
_last_sources: list[str] = []
_sync_count: int = 0
_fail_count: int = 0

logger = logging.getLogger("qqbot")


def now_shanghai() -> datetime:
    """返回权威上海时间（tz-aware）。缓存未就绪时退回系统时钟。"""
    now = datetime.now(SHANGHAI)
    if _offset is not None:
        return now + _offset
    return now


def today_shanghai() -> date:
    """返回权威上海日期"""
    return now_shanghai().date()


def get_sync_status() -> dict:
    return {
        "synced": _offset is not None,
        "offset_seconds": _offset.total_seconds() if _offset else 0.0,
        "last_sync_ts": _last_sync_ts,
        "last_sources": _last_sources,
        "sync_count": _sync_count,
        "fail_count": _fail_count,
    }


async def _fetch_source_time(url: str, session) -> Optional[datetime]:
    """从单个源拉 HTTP Date 头，返回 tz-aware UTC datetime；失败返回 None"""
    try:
        async with session.head(
            url, timeout=_SOURCE_TIMEOUT, allow_redirects=True,
            ssl=False,
        ) as resp:
            date_hdr = resp.headers.get("Date") or resp.headers.get("date")
            if not date_hdr:
                return None
            return parsedate_to_datetime(date_hdr)  # tz-aware UTC
    except Exception as e:
        logger.debug(f"timeutil 源 {url} 失败: {e}")
        return None


async def sync_once() -> tuple[Optional[timedelta], str]:
    """同步一次。返回 (偏移量, 状态描述)。偏移量 None = 失败。"""
    import aiohttp

    global _offset, _last_sync_ts, _last_sources, _sync_count, _fail_count

    sampled = random.sample(_TIME_SOURCES, min(_SAMPLE_SIZE, len(_TIME_SOURCES)))
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_source_time(u, session) for u in sampled]
        )

    # 过滤失败的
    pairs = [(u, t) for u, t in zip(sampled, results) if t is not None]
    if not pairs:
        _fail_count += 1
        return None, "all_sources_failed"

    # 系统当前 UTC 时间
    sys_utc = datetime.now(timezone.utc)
    # 每个源算出的偏移 = 源时间 - 系统时间
    offsets = [(u, t - sys_utc) for u, t in pairs]

    # 取多数一致：把偏移量化到秒，找出现次数最多的
    by_sec = {}
    for u, off in offsets:
        sec = round(off.total_seconds())
        by_sec.setdefault(sec, []).append((u, off))

    # 选票数最多且与第二差距满足多数的
    best_sec = max(by_sec, key=lambda s: len(by_sec[s]))
    winners = by_sec[best_sec]

    # 若多数组只有 1 个源、且源间偏差 > 容差，判为不一致
    if len(winners) == 1 and len(offsets) > 1:
        secs = [round(o.total_seconds()) for _, o in offsets]
        if max(secs) - min(secs) > _MAX_TOLERANCE.total_seconds():
            _fail_count += 1
            return None, "sources_disagree"

    consensus_offset = winners[0][1]
    _offset = consensus_offset
    _last_sync_ts = time.time()
    _last_sources = [u for u, _ in winners]
    _sync_count += 1
    logger.info(
        f"timeutil 同步成功: 偏移={consensus_offset.total_seconds():+.1f}s "
        f"来源={_last_sources}"
    )
    return consensus_offset, "ok"


async def start_background_sync():
    """启动后台同步：启动后立即一次 + 每 30min 一次。永不退出。"""
    # 立即同步一次（启动后尽快拿到准确时间）
    try:
        off, status = await sync_once()
        if off is None:
            logger.warning(f"timeutil 启动同步失败: {status}，退回系统时钟")
    except Exception as e:
        logger.warning(f"timeutil 启动同步异常: {e}")

    while True:
        await asyncio.sleep(_SYNC_INTERVAL)
        try:
            await sync_once()
        except Exception as e:
            logger.debug(f"timeutil 定期同步异常: {e}")
