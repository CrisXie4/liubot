"""
二次审核 & 主人通知模块

- MiniMax M3 辅助审核：当关键词系统判定违规时，用 M3 做二次确认
- 主人通知：管理模式下踢人/拉黑前必须通知主人，主人同意后才执行
"""
import json
import logging
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
            model=ms.get("model", "glm-5.2-free"),
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
