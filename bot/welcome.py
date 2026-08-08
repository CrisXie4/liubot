"""
群欢迎 & 签到 & 积分模块
- 新人入群欢迎
- 成员退群通知
- 每日签到 + 连续签到
- 简单积分系统
"""
import json
import logging
import os
import random
from datetime import datetime, date

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

logger = logging.getLogger("qqbot")

WELCOME_PATH = os.path.join(DATA_DIR, "welcome_config.json")
CHECKIN_PATH = os.path.join(DATA_DIR, "checkin.json")
POINTS_PATH = os.path.join(DATA_DIR, "points.json")


# ==================== 欢迎配置 ====================

def load_welcome_config() -> dict:
    if os.path.exists(WELCOME_PATH):
        try:
            with open(WELCOME_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "enabled": True,
        "default_message": "欢迎 {nickname} 加入本群！🎉\n一起来聊天吧~",
        "group_overrides": {},  # {group_id: {"enabled": true, "message": "..."}}
    }


def save_welcome_config(cfg: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WELCOME_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_welcome_message(group_id: int, nickname: str, user_qq: int) -> str | None:
    """获取欢迎消息，返回 None 表示不发送"""
    cfg = load_welcome_config()
    override = cfg.get("group_overrides", {}).get(str(group_id), {})

    enabled = override.get("enabled", cfg.get("enabled", True))
    if not enabled:
        return None

    template = override.get("message", cfg.get("default_message", "欢迎 {nickname} 加入本群！🎉"))
    msg = template.replace("{nickname}", nickname or str(user_qq)) \
                  .replace("{qq}", str(user_qq)) \
                  .replace("{group_id}", str(group_id)) \
                  .replace("{at}", f"[CQ:at,qq={user_qq}]")
    return msg


def set_group_welcome(group_id: int, enabled: bool, message: str = ""):
    """设置某群的欢迎配置"""
    cfg = load_welcome_config()
    key = str(group_id)
    if key not in cfg["group_overrides"]:
        cfg["group_overrides"][key] = {}
    cfg["group_overrides"][key]["enabled"] = enabled
    if message:
        cfg["group_overrides"][key]["message"] = message
    save_welcome_config(cfg)


# ==================== 退群通知 ====================

def load_leave_config() -> dict:
    cfg = load_welcome_config()
    return cfg.get("leave_notice", {"enabled": True, "message": "{nickname}({qq}) 离开了群聊 👋"})


def get_leave_message(group_id: int, nickname: str, user_qq: int) -> str | None:
    """获取退群通知消息"""
    lc = load_leave_config()
    if not lc.get("enabled", True):
        return None
    template = lc.get("message", "{nickname}({qq}) 离开了群聊 👋")
    return template.replace("{nickname}", nickname or str(user_qq)) \
                   .replace("{qq}", str(user_qq)) \
                   .replace("{group_id}", str(group_id))


# ==================== 签到系统 ====================

def load_checkin() -> dict:
    if os.path.exists(CHECKIN_PATH):
        try:
            with open(CHECKIN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_checkin(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHECKIN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def do_checkin(user_qq: int, group_id: int, user_name: str) -> dict:
    """执行签到，返回签到结果"""
    data = load_checkin()
    today = date.today().isoformat()
    key = f"{group_id}:{user_qq}"

    if key not in data:
        data[key] = {"streak": 0, "total": 0, "last_date": "", "name": user_name}

    record = data[key]
    record["name"] = user_name

    last_date = record.get("last_date", "")
    if last_date == today:
        return {"ok": False, "message": "今天已经签到过了哦~", "streak": record["streak"], "total": record["total"]}

    # 检查连续签到
    yesterday = date.today()
    from datetime import timedelta
    yesterday = (yesterday - timedelta(days=1)).isoformat()

    if last_date == yesterday:
        record["streak"] += 1
    else:
        record["streak"] = 1

    record["total"] += 1
    record["last_date"] = today

    # 计算奖励积分
    base_points = 5
    streak_bonus = min(record["streak"] - 1, 10) * 2  # 连续签到每天+2，最多+20
    points_earned = base_points + streak_bonus

    # 更新积分
    add_points(user_qq, group_id, user_name, points_earned, "签到")

    save_checkin(data)

    # 随机鼓励语
    encouragements = [
        "坚持就是胜利！💪", "每天都来，真棒！🌟", "群友模范！🏆",
        "又是元气满满的一天！☀️", "签到成功，好运连连！🍀",
        "你的坚持让群更温暖~ 🔥", "打卡达人！📅", "今天也是爱群的一天！❤️",
    ]

    return {
        "ok": True,
        "message": f"✅ 签到成功！+{points_earned}分 "
                   f"（连续 {record['streak']} 天 · 累计 {record['total']} 天）\n"
                   f"{random.choice(encouragements)}",
        "streak": record["streak"],
        "total": record["total"],
        "points": points_earned,
    }


def get_checkin_status(user_qq: int, group_id: int) -> dict:
    """获取签到状态"""
    data = load_checkin()
    key = f"{group_id}:{user_qq}"
    today = date.today().isoformat()

    if key not in data:
        return {"checked_today": False, "streak": 0, "total": 0}

    record = data[key]
    return {
        "checked_today": record.get("last_date") == today,
        "streak": record.get("streak", 0),
        "total": record.get("total", 0),
    }


def get_group_checkin_leaderboard(group_id: int, limit: int = 10) -> list[dict]:
    """群签到排行榜"""
    data = load_checkin()
    prefix = f"{group_id}:"
    group_records = []
    for key, record in data.items():
        if key.startswith(prefix):
            qq = key.split(":")[1]
            group_records.append({
                "user_qq": int(qq),
                "user_name": record.get("name", str(qq)),
                "streak": record.get("streak", 0),
                "total": record.get("total", 0),
                "last_date": record.get("last_date", ""),
            })
    group_records.sort(key=lambda x: (-x["streak"], -x["total"]))
    return group_records[:limit]


# ==================== 积分系统 ====================

def load_points() -> dict:
    if os.path.exists(POINTS_PATH):
        try:
            with open(POINTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_points(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(POINTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_points(user_qq: int, group_id: int, user_name: str, points: int, reason: str = ""):
    """添加积分"""
    data = load_points()
    key = f"{group_id}:{user_qq}"
    if key not in data:
        data[key] = {"points": 0, "name": user_name}
    data[key]["points"] += points
    data[key]["name"] = user_name
    save_points(data)


def get_points(user_qq: int, group_id: int) -> int:
    """获取某人的积分"""
    data = load_points()
    key = f"{group_id}:{user_qq}"
    return data.get(key, {}).get("points", 0)


def get_group_points_leaderboard(group_id: int, limit: int = 15) -> list[dict]:
    """群积分排行榜"""
    data = load_points()
    prefix = f"{group_id}:"
    records = []
    for key, record in data.items():
        if key.startswith(prefix):
            qq = key.split(":")[1]
            records.append({
                "user_qq": int(qq),
                "user_name": record.get("name", str(qq)),
                "points": record.get("points", 0),
            })
    records.sort(key=lambda x: -x["points"])
    return records[:limit]
