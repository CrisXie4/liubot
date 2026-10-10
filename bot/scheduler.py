"""
定时任务 / 主动消息模块
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from . import timeutil

logger = logging.getLogger("qqbot")

SCHEDULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "schedule.json")

DEFAULT_SCHEDULES = []


def load_schedules() -> list[dict]:
    if os.path.exists(SCHEDULE_PATH):
        try:
            with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return list(DEFAULT_SCHEDULES)


def save_schedules(schedules: list[dict]):
    os.makedirs(os.path.dirname(SCHEDULE_PATH), exist_ok=True)
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)


class Scheduler:
    """简易定时任务调度器"""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._send_func = None  # 由外部注入

    def set_sender(self, func):
        self._send_func = func

    async def start(self):
        """加载并启动所有定时任务"""
        schedules = load_schedules()
        for s in schedules:
            if s.get("enabled", True):
                task = asyncio.create_task(self._run_schedule(s))
                self._tasks.append(task)
        # 定期扫描群名片违规（每30分钟）
        task = asyncio.create_task(self._run_nickname_scanner())
        self._tasks.append(task)
        logger.info(f"调度器已启动，{len(self._tasks)} 个定时任务")

    async def _run_nickname_scanner(self):
        """每30分钟扫描一次所有群的成员名片"""
        import asyncio as _asyncio
        while True:
            await _asyncio.sleep(1800)  # 30 分钟
            try:
                import bot as _bot
                if not _bot.bot_instance:
                    continue
                from . import groups, handler
                glist = await groups.fetch_groups()
                for g in glist:
                    gid = g["group_id"]
                    result = await handler.check_member_names(_bot.bot_instance, gid)
                    # check_member_names 返回 {"warnings": [...], "actions": [...]}
                    warns = result.get("warnings") if isinstance(result, dict) else result
                    if warns:
                        preview = ' | '.join(str(w).splitlines()[0] for w in warns[:3])
                        logger.info(f"名片扫描: 群{gid} 发现 {len(warns)} 个违规名片: {preview[:300]}")
            except Exception as e:
                logger.warning(f"名片扫描异常: {e}")

    async def _run_schedule(self, sched: dict):
        """运行单个定时任务"""
        group_id = sched.get("group_id", 0)
        message = sched.get("message", "")
        cron = sched.get("cron", "")
        # cron 格式: "HH:MM" 每天 / "HH:MM:SS" / "interval:秒数"
        while True:
            try:
                if cron.startswith("interval:"):
                    secs = int(cron.split(":")[1])
                    await asyncio.sleep(secs)
                else:
                    # 每天定点
                    now = timeutil.now_shanghai()
                    parts = cron.split(":")
                    target_h, target_m = int(parts[0]), int(parts[1])
                    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
                    if target <= now:
                        from datetime import timedelta
                        target += timedelta(days=1)
                    wait = (target - now).total_seconds()
                    await asyncio.sleep(wait)

                if self._send_func and group_id and message:
                    await self._send_func(group_id, message)
                    logger.info(f"定时消息已发送: 群{group_id}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时任务异常: {e}")
                await asyncio.sleep(60)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


# 全局单例
scheduler = Scheduler()
