"""
二次审核 & 主人通知模块

- MiniMax M3 辅助审核：当关键词系统判定违规时，用 M3 做二次确认
- 主人通知：管理模式下踢人/拉黑前必须通知主人，主人同意后才执行
"""
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Optional

from .config import config

logger = logging.getLogger("qqbot")

# 待审批的操作
_pending_actions: dict[str, dict] = {}  # {action_id: {group_id, user_qq, user_name, reason, message, timestamp}}
_pending_lock = threading.Lock()

async def _call_minimax(text: str) -> Optional[dict]:
    """调用 LLM 进行二次审核（统一走 llm_call）"""
    from . import llm_call, memory
    if llm_call.get_client() is None:
        return None

    try:
        ms = memory.load_model_settings()
        response = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=150,
            temperature=0.1,
            messages=[{"role": "user", "content": (
                "你是一个QQ群内容审核助手。请判断以下消息是否违规。"
                "只有明确的广告、色情、赌博、诈骗、辱骂才算违规。"
                "正常的技术讨论、日常聊天不算违规。\n\n"
                "消息: " + text[:500] + "\n\n"
                "请只输出JSON:\n"
                '{"is_violation": true/false, "reason": "简短理由(15字以内)", "confidence": 0.0-1.0}'
            )}],
        )

        result_text = response.content[0].text.strip()
        # 清理 markdown
        result_text = re.sub(r"^```\w*\n?", "", result_text)
        result_text = re.sub(r"\n?```$", "", result_text)
        return json.loads(result_text)
    except json.JSONDecodeError:
        # Try to extract from broken JSON
        is_v = '"is_violation": true' in result_text.lower() if 'result_text' in dir() else False
        return {"is_violation": is_v, "reason": "解析失败", "confidence": 0.5}
    except Exception as e:
        logger.warning(f"MiniMax 二次审核失败: {e}")
        return None

async def secondary_check(raw_message: str, keyword_score: dict) -> dict:
    """
    二次审核：用 MiniMax M3 确认关键词系统的判定

    返回:
    {
        "agreed": True/False,        # M3 是否同意是违规
        "should_action": True/False,  # 是否应该采取动作
        "reason": "M3 判定理由",
        "needs_owner": True/False,   # 是否需要主人审批
    }
    """
    m3 = await _call_minimax(raw_message)

    if m3 is None:
        # M3 不可用 → 信任关键词系统，但标记需要主人审批
        return {
            "agreed": True,
            "should_action": True,
            "reason": "MiniMax 不可用，信任关键词系统",
            "needs_owner": True,
        }

    m3_violation = m3.get("is_violation", False)
    kw_violation = keyword_score.get("is_violation", False)
    kw_confidence = keyword_score.get("confidence", 0)
    m3_confidence = m3.get("confidence", 0.5)

    agreed = m3_violation and kw_violation

    # 决策矩阵
    if agreed:
        # 两方都认为是违规 → 确认
        return {
            "agreed": True,
            "should_action": True,
            "reason": f"M3确认: {m3.get('reason', '违规')} (M3:{m3_confidence:.0%} KW:{kw_confidence:.0%})",
            "needs_owner": True,  # 管理动作始终需要主人审批
        }
    elif kw_violation and not m3_violation:
        # 关键词说是但 M3 说不是 → 只记录，不动作
        return {
            "agreed": False,
            "should_action": False,
            "reason": f"M3 不认同: {m3.get('reason', '正常')} (M3:{m3_confidence:.0%} KW:{kw_confidence:.0%})",
            "needs_owner": False,
        }
    else:
        # 都不认为是违规
        return {
            "agreed": False,
            "should_action": False,
            "reason": "双方均不判定违规",
            "needs_owner": False,
        }

# ==================== 主人通知系统 ====================

def create_action_request(
    group_id: int, user_qq: int, user_name: str,
    action: str, reason: str, message: str
) -> str:
    """创建待审批操作，返回 action_id"""
    import uuid
    aid = str(uuid.uuid4())[:8]
    with _pending_lock:
        _pending_actions[aid] = {
            "group_id": group_id,
            "user_qq": user_qq,
            "user_name": user_name,
            "action": action,
            "reason": reason,
            "message": message[:200],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    return aid

def get_pending_actions() -> list[dict]:
    """获取所有待审批操作"""
    with _pending_lock:
        return [{"id": k, **v} for k, v in _pending_actions.items()]

def approve_action(action_id: str) -> Optional[dict]:
    """批准操作，返回操作详情"""
    with _pending_lock:
        return _pending_actions.pop(action_id, None)

def reject_action(action_id: str) -> Optional[dict]:
    """拒绝操作"""
    with _pending_lock:
        return _pending_actions.pop(action_id, None)


# ==================== 分级裁判闸门（通用） ====================

SEVERITY_AUTONOMOUS = "autonomous"   # 直接执行
SEVERITY_LIGHT = "light"             # 快规则当场执行
SEVERITY_HEAVY = "heavy"             # 异步裁判
SEVERITY_EXEMPT = "exempt"           # 自禁等豁免

# 动作 → 默认级别
ACTION_SEVERITY = {
    "set_mood": SEVERITY_AUTONOMOUS,
    "set_essence": SEVERITY_AUTONOMOUS,
    "upload_group_file": SEVERITY_AUTONOMOUS,
    "create_group_folder": SEVERITY_AUTONOMOUS,
    "set_self_card": SEVERITY_AUTONOMOUS,
    "set_self_nickname": SEVERITY_AUTONOMOUS,
    "at_all": SEVERITY_LIGHT,          # @全体受限速（轻量）
    "recall": SEVERITY_LIGHT,
    "mute": SEVERITY_LIGHT,            # 禁言 ≤10min 是轻量；>10min 是重度
    "set_member_card": SEVERITY_LIGHT,
    "set_member_title": SEVERITY_LIGHT,
    "kick": SEVERITY_HEAVY,
    "ban": SEVERITY_HEAVY,
    "whole_ban": SEVERITY_HEAVY,
    "self_ban": SEVERITY_EXEMPT,       # 自禁豁免
}

_CONFIG_PATH = os.path.join(config.DATA_DIR, "ai_guard_config.json")
_config_cache = None
_config_ts = 0.0


def load_config() -> dict:
    """加载 ai_guard 配置，带文件 mtime 缓存（5s）。文件缺失时返回 {}，
    由各消费者的 .get(key, default) 提供安全默认值（保护名单默认开启）。"""
    global _config_cache, _config_ts
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        mtime = 0
    if _config_cache is not None and abs(mtime - _config_ts) < 5:
        return _config_cache
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    except Exception:
        _config_cache = {}
    _config_ts = mtime
    return _config_cache or {}


async def is_protected(target_qq: int, group_id: int,
                       member_info: dict = None,
                       master_instructed: bool = False) -> bool:
    """判断目标是否受保护。

    master_instructed=True 且配置允许覆盖时，
    保护名单里除主人自身外都可被覆盖。

    主人 QQ 统一从 config.OWNER_QQ 读（与 master_instructed 判定同源），
    不在 ai_guard_config.json 里重复维护。
    """
    cfg = load_config().get("protected", {})
    # 主人 QQ：合并 config.OWNER_QQ（主）+ ai_guard_config.master_qq（兼容旧配置）
    master_set = set()
    try:
        from .config import config as _cfg
        if getattr(_cfg, "OWNER_QQ", ""):
            master_set.add(int(_cfg.OWNER_QQ))
    except Exception:
        pass
    master_set.update(int(x) for x in cfg.get("master_qq", []))
    whitelist = set(int(x) for x in cfg.get("whitelist", []))

    # 主人永远受保护（即使 master_instructed 也不能动主人自己）
    if target_qq in master_set:
        return True
    if target_qq in whitelist:
        return True

    # 主人指令可覆盖其余保护
    if master_instructed and load_config().get("master_override_protects", True):
        return False

    # 群主/群管理员
    if member_info and isinstance(member_info, dict):
        role = member_info.get("role", "")
        if cfg.get("protect_group_owner", True) and role == "owner":
            return True
        if cfg.get("protect_group_admin", True) and role == "admin":
            return True
    return False


async def judge(action: str, target_qq: int, target_name: str,
                reason: str, context_snippets: list[str],
                master_instructed: bool) -> tuple[bool, str]:
    """重度动作异步裁判。返回 (allow, verdict_reason)。"""
    cfg = load_config().get("judge", {})
    if not cfg.get("enabled", True):
        return True, "judge_disabled"

    from . import llm_call, memory
    if llm_call.get_client() is None:
        return False, "llm_unavailable"

    ms = memory.load_model_settings()
    prompt = (
        "你是 QQ 群机器人的安全裁判。机器人的 AI 想对一名群成员执行一个重度操作。"
        "判断这个操作是否合理。\n\n"
        f"操作: {action}\n"
        f"目标: {target_name}({target_qq})\n"
        f"AI 给的理由: {reason[:300]}\n"
        f"是否主人明确指令: {'是' if master_instructed else '否'}\n\n"
        "最近群聊上下文（仅参考，不据此判定违规）：\n"
        + "\n".join(f"- {s[:100]}" for s in context_snippets[-10:]) +
        "\n\n合理性判定标准：\n"
        "- 主人明确指令 → 默认允许（除非目标本身是主人）\n"
        "- 非主人指令时：需要明确的违规/攻击证据，不能因'看他不爽'就动\n"
        "- 无理取闹/报复/无证据 → 拒绝\n\n"
        "只输出 JSON:\n"
        '{"allow": true/false, "verdict": "简短理由(20字以内)"}'
    )
    try:
        response = await llm_call.llm_create(
            model=cfg.get("model") or ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=cfg.get("max_tokens", 100),
            temperature=cfg.get("temperature", 0.1),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        result = json.loads(text)
        return bool(result.get("allow", False)), result.get("verdict", "")
    except Exception as e:
        logger.warning(f"guardian 裁判失败（保守拒绝）: {e}")
        return False, f"judge_error: {type(e).__name__}"


async def request(group_id: int, action: str,
                  target_qq: int = None, target_name: str = "",
                  reason: str = "",
                  caller_qq: int = None,
                  master_instructed: bool = False,
                  member_info: dict = None,
                  mute_seconds: int = 0,
                  context_snippets: list[str] = None,
                  bot_is_admin: bool = True) -> dict:
    """统一闸门入口。返回:
       {"allow": bool, "verdict": str, "audit_id": int,
        "severity": str, "reason": str, "placeholder_sent": bool}
    """
    from . import db

    # 禁言时长决定级别：>10min 升级为重度
    severity = ACTION_SEVERITY.get(action, SEVERITY_LIGHT)
    if action == "mute" and mute_seconds > load_config().get(
            "thresholds", {}).get("mute_light_max_seconds", 600):
        severity = SEVERITY_HEAVY

    # 自禁豁免
    cfg = load_config()
    if (cfg.get("self_ban_exempt", True)
            and target_qq and caller_qq
            and target_qq == caller_qq
            and action in ("mute", "ban", "kick", "self_ban")):
        severity = SEVERITY_EXEMPT

    # 保护名单硬护栏（豁免级别也查——除非是自禁）
    if target_qq and severity != SEVERITY_EXEMPT:
        if await is_protected(target_qq, group_id, member_info, master_instructed):
            aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                     "deny_protected", master_instructed)
            return {"allow": False, "verdict": "deny_protected",
                    "audit_id": aid, "severity": severity,
                    "reason": f"目标 {target_name} 受保护",
                    "placeholder_sent": False}

    # 自主/豁免 → 直接放行
    if severity in (SEVERITY_AUTONOMOUS, SEVERITY_EXEMPT):
        v = "autonomous" if severity == SEVERITY_AUTONOMOUS else "exempt"
        aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                 v, master_instructed)
        return {"allow": True, "verdict": v, "audit_id": aid,
                "severity": severity, "reason": reason,
                "placeholder_sent": False}

    # 轻量 → 当场放行（不调裁判 LLM），仅记审计
    if severity == SEVERITY_LIGHT:
        aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                 "light", master_instructed)
        return {"allow": True, "verdict": "light", "audit_id": aid,
                "severity": severity, "reason": reason,
                "placeholder_sent": False}

    # 重度 → 主人指令跳裁判立即执行；否则异步裁判
    # （调用方应在调本函数前已发占位文案——仅非主人触发时）
    if master_instructed:
        # 主人明确指令：保护名单已在上面检查过（主人自身永远拒），
        # 其余一律放行，不再等 ~100s 裁判
        aid = db.record_ai_audit(group_id, action, target_qq, reason,
                                 "master_instructed", master_instructed)
        return {"allow": True, "verdict": "master_instructed",
                "audit_id": aid, "severity": severity,
                "reason": reason, "placeholder_sent": False}

    allow, verdict = await judge(
        action, target_qq or 0, target_name, reason,
        context_snippets or [], master_instructed)
    label = "allow" if allow else "deny_judge"
    aid = db.record_ai_audit(group_id, action, target_qq, reason,
                             label, master_instructed)
    return {"allow": allow, "verdict": label, "audit_id": aid,
            "severity": severity,
            "reason": verdict if not allow else reason,
            "placeholder_sent": False}


# ==================== 限速器（@all / 精华） ====================

# 内存限速状态：{(group_id, action): [(ts,...)]}
_rate_state: dict[tuple, list[float]] = {}


def _check_rate(group_id: int, action: str, window_hours: int, max_count: int) -> bool:
    """检查 (group_id, action) 在过去 window_hours 小时内是否超过 max_count。返回 True=允许。"""
    import time as _time
    key = (int(group_id), action)
    now = _time.time()
    cutoff = now - window_hours * 3600
    recent = [t for t in _rate_state.get(key, []) if t > cutoff]
    if len(recent) >= max_count:
        return False
    recent.append(now)
    _rate_state[key] = recent
    return True


def check_at_all_rate(group_id: int) -> bool:
    """@全体 限速：每群每 6h 至多 1 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "at_all",
        cfg.get("at_all_hours_per_group", 6),
        cfg.get("at_all_max_per_window", 1),
    )


def check_essence_rate(group_id: int) -> bool:
    """自主设精华限速：每群每天 N 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "essence", 24,
        cfg.get("essence_daily_per_group", 5),
    )


def check_proactive_rate(group_id: int) -> bool:
    """主动消息（volition SPEAK + 新闻分享）软限速：每群每小时最多 8 条"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "proactive", 1,
        cfg.get("proactive_hourly_per_group", 8),
    )


def check_auto_emoji_rate(group_id: int) -> bool:
    """自动表情表态限速：每群每小时最多 8 次"""
    cfg = load_config().get("thresholds", {})
    return _check_rate(
        group_id, "auto_emoji", 1,
        cfg.get("auto_emoji_hourly_per_group", 8),
    )


def pick_placeholder() -> str:
    """随机选一条占位文案（重度动作触发时群里先发）"""
    msgs = load_config().get("judge", {}).get(
        "placeholder_messages", ["让我想想…"])
    import random as _r
    return _r.choice(msgs) if msgs else "让我想想…"
