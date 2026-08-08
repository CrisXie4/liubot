"""
群列表获取模块
- 从 Bot 获取已加入的群列表
- TTL 缓存（5分钟）
"""
import logging
import time
from typing import Optional

from . import cache
from .config import config

logger = logging.getLogger("qqbot")

# 缓存 key
GROUPS_CACHE_KEY = "bot_groups"
GROUPS_CACHE_TTL = 300  # 5分钟


async def fetch_groups(force: bool = False) -> list[dict]:
    """
    获取 Bot 已加入的群列表。
    优先走缓存（5分钟TTL），force=True 强制刷新。
    """
    # 读取缓存
    if not force:
        cached = cache.group_mode_cache.get(GROUPS_CACHE_KEY)
        if cached is not None:
            return cached

    import bot as _bot
    if not _bot.bot_instance:
        logger.warning("Bot 未连接，无法获取群列表")
        return _load_fallback()

    try:
        r = await _bot.bot_instance._call("get_group_list", {})
        if r.get("status") == "ok":
            groups = r.get("data", [])
            # 只保留需要的字段
            result = []
            for g in groups:
                result.append({
                    "group_id": g.get("group_id", 0),
                    "group_name": g.get("group_name", ""),
                    "member_count": g.get("member_count", 0),
                    "max_member_count": g.get("max_member_count", 0),
                })
            # 写入缓存
            cache.group_mode_cache.set(GROUPS_CACHE_KEY, result, GROUPS_CACHE_TTL)
            return result
        else:
            logger.warning(f"获取群列表失败: {r}")
            return _load_fallback()
    except Exception as e:
        logger.error(f"获取群列表异常: {e}")
        return _load_fallback()


def _load_fallback() -> list[dict]:
    """从本地数据兜底：收集所有出现过的 group_id"""
    from . import db as _db
    try:
        conn = _db.get_connection()
        # 从违规记录中收集
        rows = conn.execute(
            "SELECT DISTINCT group_id, group_name FROM violations WHERE group_id > 0 ORDER BY group_id"
        ).fetchall()
        # 从用户画像中收集
        rows2 = conn.execute(
            "SELECT DISTINCT group_id FROM user_profiles WHERE group_id > 0 ORDER BY group_id"
        ).fetchall()
        conn.close()

        seen = set()
        result = []
        for r in rows:
            gid = r["group_id"]
            if gid not in seen:
                seen.add(gid)
                result.append({"group_id": gid, "group_name": r["group_name"] or f"群{gid}", "member_count": 0, "max_member_count": 0})
        for r in rows2:
            gid = r["group_id"]
            if gid not in seen:
                seen.add(gid)
                result.append({"group_id": gid, "group_name": f"群{gid}", "member_count": 0, "max_member_count": 0})
        return result
    except Exception:
        return []
