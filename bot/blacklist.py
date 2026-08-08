"""
黑名单管理模块
"""
import logging
from typing import Optional

from . import db

logger = logging.getLogger("qqbot")


def add_to_blacklist(user_qq: int, user_name: str = "", group_id: int = 0,
                     reason: str = "", action: str = "ban", by_qq: int = 0) -> int:
    """加入黑名单，返回记录 ID"""
    return db.blacklist_add(user_qq, user_name, group_id, reason, action, by_qq)


def remove_from_blacklist(user_qq: int, group_id: int = 0) -> bool:
    return db.blacklist_remove(user_qq, group_id)


def is_blacklisted(user_qq: int, group_id: int = 0) -> Optional[dict]:
    return db.blacklist_check(user_qq, group_id)


def list_all(group_id: int = 0) -> list[dict]:
    return db.blacklist_list(group_id)
