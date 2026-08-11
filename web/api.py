"""
Web API v4 — 安全加固版
新增: JWT鉴权、LLM审核面板、安全设置、限流
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, Request

from bot import (
    db, automod, trainer, blacklist, personality, scheduler, learner, memory,
    sticker, group_personality, influence, media_reader, welcome,
    llm_mod, security,
)
from bot.config import config
from bot.security import (
    create_jwt_token, verify_jwt_token, login_limiter,
    store_encrypted_secrets, load_encrypted_secrets,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger("qqbot")

# WebSocket 连接池
_ws_clients: list[WebSocket] = []


# ==================== Auth (JWT) ====================

@router.post("/auth/login")
async def auth_login(request: Request, data: dict):
    """
    JWT 登录。
    请求: {"password": "xxx"}
    返回: {"ok": true, "token": "xxx", "expires_in": 86400}
    """
    password = data.get("password", "")
    client_ip = request.client.host if request.client else "unknown"

    # 限流检查
    if not login_limiter.allow(client_ip):
        logger.warning(f"登录限流触发: {client_ip}")
        raise HTTPException(429, "登录过于频繁，请1分钟后再试")

    if not password:
        raise HTTPException(400, "请输入密码")

    if password != config.WEB_PASSWORD:
        logger.warning(f"登录失败: {client_ip}")
        raise HTTPException(403, "密码错误")

    token = create_jwt_token(
        {"sub": "admin", "role": "owner"},
        expires_hours=config.JWT_EXPIRES_HOURS,
    )
    logger.info(f"登录成功: {client_ip}")
    return {
        "ok": True,
        "token": token,
        "expires_in": config.JWT_EXPIRES_HOURS * 3600,
    }


@router.get("/auth/status")
async def auth_status(request: Request):
    """检查 token 是否有效"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = verify_jwt_token(auth[7:])
        if payload:
            return {
                "ok": True,
                "user": payload.get("sub", "admin"),
                "expires_at": payload.get("exp", 0),
            }
    return {"ok": False, "reason": "未登录或已过期"}


# 兼容旧接口
@router.post("/auth")
async def auth_legacy(password: str):
    """旧版密码登录（兼容），返回 JWT token"""
    if password == config.WEB_PASSWORD:
        token = create_jwt_token(
            {"sub": "admin", "role": "owner"},
            expires_hours=config.JWT_EXPIRES_HOURS,
        )
        return {"ok": True, "token": token}
    raise HTTPException(403, "密码错误")


# ==================== Dashboard ====================

@router.get("/dashboard")
async def dashboard():
    stats = db.get_violation_stats()
    am_stats = db.automod_stats()
    bl_count = len(db.blacklist_list())
    train_status = trainer.get_training_status()
    return {
        "violations_total": stats["total"],
        "violations_auto": stats.get("auto_count", 0),
        "violations_manual": stats["total"] - stats.get("auto_count", 0),
        "blacklist_count": bl_count,
        "auto_scanned": am_stats["total_scanned"],
        "auto_recalled": am_stats["auto_recalled"],
        "training_phase": train_status["phase"],
        "ml_ready": train_status["ml_model_ready"],
        "threshold": train_status["auto_mod_threshold"],
        "top_violators": stats["top_violators"][:5],
        "top_keywords": db.get_common_keywords(top_n=10),
        "hour_distribution": stats["hour_distribution"],
        "source_distribution": stats.get("source_distribution", []),
        "recent_violations": stats["recent"][:8],
    }


# ==================== Violations ====================

@router.get("/violations")
async def list_violations(
    group_id: Optional[int] = Query(None),
    violator_qq: Optional[int] = Query(None),
    source: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    records = db.query_violations(
        group_id=group_id, violator_qq=violator_qq,
        source=source, limit=page_size, offset=(page - 1) * page_size,
    )
    total = db.get_violation_count()
    return {"items": records, "total": total, "page": page, "page_size": page_size}


@router.get("/violations/keywords")
async def violation_keywords(top_n: int = 30):
    return {"keywords": db.get_common_keywords(top_n=top_n)}


@router.delete("/violations/{vid}")
async def delete_violation(vid: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM violations WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ==================== Blacklist ====================

@router.get("/blacklist")
async def list_blacklist():
    return {"items": blacklist.list_all()}


@router.post("/blacklist")
async def add_blacklist(user_qq: int, user_name: str = "", group_id: int = 0,
                        reason: str = "", action: str = "ban"):
    bid = blacklist.add_to_blacklist(user_qq, user_name, group_id, reason, action)
    await _broadcast({"type": "blacklist_added", "user_qq": user_qq, "reason": reason})
    return {"ok": True, "id": bid}


@router.delete("/blacklist/{user_qq}")
async def remove_blacklist(user_qq: int, group_id: int = 0):
    ok = blacklist.remove_from_blacklist(user_qq, group_id)
    return {"ok": ok}


# ==================== AutoMod ====================

@router.get("/automod/status")
async def automod_status():
    stats = db.automod_stats()
    status = trainer.get_training_status()
    return {**stats, **status, "auto_mod_enabled": config.AUTO_MOD_ENABLED}


@router.post("/automod/toggle")
async def automod_toggle():
    config.AUTO_MOD_ENABLED = not config.AUTO_MOD_ENABLED
    return {"enabled": config.AUTO_MOD_ENABLED}


@router.post("/automod/train")
async def automod_train():
    automod._build_ml_model()
    return {"ok": True, "status": trainer.get_training_status()}


@router.get("/automod/test")
async def automod_test(message: str):
    score = automod.score_message(message)
    ml = automod.ml_predict(message)
    return {"keyword_score": score, "ml_score": ml}


@router.get("/automod/log")
async def automod_logs(limit: int = 50):
    return {"items": db.automod_log_query(limit=limit)}


# ==================== Personality ====================

@router.get("/personality")
async def get_personality():
    return personality.load()


@router.post("/personality")
async def save_personality(data: dict):
    personality.save(data)
    return {"ok": True}


# ==================== Schedules ====================

@router.get("/schedules")
async def list_schedules():
    return {"items": scheduler.load_schedules()}


@router.post("/schedules")
async def add_schedule(data: dict):
    scheds = scheduler.load_schedules()
    scheds.append(data)
    scheduler.save_schedules(scheds)
    return {"ok": True}


@router.put("/schedules/{idx}")
async def update_schedule(idx: int, data: dict):
    scheds = scheduler.load_schedules()
    if 0 <= idx < len(scheds):
        scheds[idx] = data
        scheduler.save_schedules(scheds)
        return {"ok": True}
    raise HTTPException(404, "未找到")


@router.delete("/schedules/{idx}")
async def delete_schedule(idx: int):
    scheds = scheduler.load_schedules()
    if 0 <= idx < len(scheds):
        scheds.pop(idx)
        scheduler.save_schedules(scheds)
        return {"ok": True}
    raise HTTPException(404, "未找到")


# ==================== Learner ====================

@router.get("/learner/status")
async def learner_status():
    from bot import learner
    return learner.get_learning_status()


@router.get("/learner/test")
async def learner_test(message: str):
    from bot import learner
    return learner.score_with_learning(message)


@router.post("/learner/false_positive")
async def learner_false_positive(message: str):
    from bot import learner
    learner.record_false_positive(message)
    return {"ok": True}


# ==================== 模型设置 ====================

@router.get("/model-settings")
async def get_model_settings():
    """获取模型设置（API key 脱敏）"""
    return memory.get_model_settings_display()


@router.post("/model-settings")
async def save_model_settings(data: dict):
    """保存模型设置"""
    allowed = {"api_url", "api_key", "minimax_api_key", "model", "fallback_model", "vision_model", "temperature", "max_tokens",
               "context_size"}
    filtered = {k: v for k, v in data.items() if k in allowed}
    # 如果 api_key 是脱敏值（含 ****），保留原来的 key
    if "api_key" in filtered and "****" in str(filtered["api_key"]):
        old = memory.load_model_settings()
        filtered["api_key"] = old.get("api_key", "")
    memory.save_model_settings(filtered)
    return {"ok": True}


# ==================== AI 记忆 ====================

@router.get("/memory/stats")
async def memory_stats():
    """获取记忆系统统计"""
    return db.get_memory_stats()


@router.get("/memory/profiles")
async def memory_profiles(group_id: int = 0):
    """获取用户画像列表"""
    if group_id:
        return {"items": db.get_group_profiles(group_id)}
    # 全局所有画像
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM user_profiles ORDER BY message_count DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


@router.get("/memory/profiles/search")
async def memory_search_profiles(q: str):
    """搜索用户画像"""
    return {"items": db.search_user_profiles(q)}


@router.get("/memory/memories")
async def memory_memories(group_id: int = 0, limit: int = 50):
    """获取对话记忆列表"""
    if group_id:
        return {"items": db.get_recent_memories(group_id, limit)}
    # 全部
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM conversation_memories ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return {"items": [db._memory_row_to_dict(r) for r in rows]}


@router.get("/memory/memories/search")
async def memory_search_memories(q: str, group_id: int = 0):
    """搜索对话记忆"""
    return {"items": db.search_memories(q, group_id)}


@router.get("/memory/facts")
async def memory_facts(category: str = "", limit: int = 50):
    """获取知识碎片"""
    if category:
        return {"items": db.get_facts_by_category(category, limit)}
    return {"items": db.get_relevant_facts(limit=limit)}


@router.delete("/memory/profiles/{user_qq}")
async def memory_delete_profile(user_qq: int, group_id: int = 0):
    """删除用户画像"""
    conn = db.get_connection()
    if group_id:
        conn.execute("DELETE FROM user_profiles WHERE user_qq=? AND group_id=?", (user_qq, group_id))
    else:
        conn.execute("DELETE FROM user_profiles WHERE user_qq=?", (user_qq,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.delete("/memory/memories/{mid}")
async def memory_delete_memory(mid: int):
    """删除对话记忆"""
    conn = db.get_connection()
    conn.execute("DELETE FROM conversation_memories WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ==================== Bot 控制 ====================

@router.post("/bot/send")
async def bot_send_message(group_id: int, message: str):
    """手动发送群消息（无视 Bot 主开关，主人手动发的永远能发）"""
    import bot as _bot
    if _bot.bot_instance:
        mid = await _bot.bot_instance.send_group_msg(group_id, message)
        return {"ok": True, "message_id": mid}
    raise HTTPException(503, "Bot 未连接")


# ==================== 主开关 (Bot 整体 / AI 整体) ====================

@router.get("/bot/master-switches")
async def get_master_switches():
    from bot import runtime_switches as rs
    return rs.snapshot()


@router.post("/bot/master-switches")
async def set_master_switches(data: dict):
    from bot import runtime_switches as rs
    if "bot_enabled" in data:
        rs.set_bot_on(bool(data["bot_enabled"]))
    if "ai_enabled" in data:
        rs.set_ai_on(bool(data["ai_enabled"]))
    return rs.snapshot()


# ==================== 群列表 ====================

@router.get("/groups")
async def list_groups(force: bool = False):
    """获取 Bot 已加入的群列表（带缓存）"""
    from bot import groups
    items = await groups.fetch_groups(force=force)
    return {"items": items, "total": len(items)}


# ==================== 群模式管理 ====================

@router.get("/group-modes")
async def get_group_modes():
    """获取所有群的模式配置"""
    modes = config._group_modes.copy()
    return {
        "modes": {str(k): v for k, v in modes.items()},
        "admin_groups": [str(k) for k, v in modes.items() if v == "admin"],
        "friend_groups": [str(k) for k, v in modes.items() if v == "friend"],
        "auto_count": len([k for k, v in modes.items() if v == "auto"]),
    }


@router.post("/group-modes")
async def set_group_mode(data: dict):
    """设置一个群的模式: admin / friend / auto"""
    group_id = data.get("group_id")
    mode = data.get("mode", "auto")
    if not group_id:
        raise HTTPException(400, "缺少 group_id")
    if mode not in ("admin", "friend", "auto"):
        raise HTTPException(400, "mode 必须是 admin / friend / auto")
    config.set_group_mode(int(group_id), mode)
    return {"ok": True, "group_id": group_id, "mode": mode}


# ==================== 欢迎 & 签到 ====================

@router.get("/welcome/config")
async def get_welcome_config():
    """获取所有群的欢迎配置"""
    cfg = welcome.load_welcome_config()
    return cfg


@router.post("/welcome/config")
async def save_welcome_config(data: dict):
    """保存欢迎配置"""
    welcome.save_welcome_config(data)
    return {"ok": True}


@router.post("/welcome/group/{group_id}")
async def set_group_welcome_config(group_id: int, data: dict):
    """设置某群的欢迎"""
    welcome.set_group_welcome(
        group_id,
        enabled=data.get("enabled", True),
        message=data.get("message", ""),
    )
    return {"ok": True}


@router.get("/checkin/leaderboard/{group_id}")
async def checkin_leaderboard(group_id: int, limit: int = 15):
    """签到排行榜"""
    return {"items": welcome.get_group_checkin_leaderboard(group_id, limit=limit)}


@router.get("/points/leaderboard/{group_id}")
async def points_leaderboard(group_id: int, limit: int = 15):
    """积分排行榜"""
    return {"items": welcome.get_group_points_leaderboard(group_id, limit=limit)}


# ==================== 关键词管理 ====================

@router.get("/keywords")
async def list_keywords(search: str = "", limit: int = 100):
    """列出已学习的关键词（支持搜索）"""
    kb = learner._load_knowledge()
    items = []
    for kw, weight in kb.get("learned_keywords", {}).items():
        if not search or search.lower() in kw.lower():
            items.append({"keyword": kw, "weight": round(weight, 1)})
    items.sort(key=lambda x: -x["weight"])
    result = items[:limit]
    return {"items": result, "total": len(items), "shown": len(result)}


@router.post("/keywords")
async def add_keyword(data: dict):
    """手动添加关键词: {"keyword": "广告词", "weight": 3.0}"""
    kw = data.get("keyword", "").strip()
    weight = float(data.get("weight", 3.0))
    if not kw:
        raise HTTPException(400, "关键词不能为空")
    kb = learner._load_knowledge()
    kb["learned_keywords"][kw] = min(kb["learned_keywords"].get(kw, 0) + weight, 10.0)
    learner._save_knowledge(kb)
    return {"ok": True, "keyword": kw, "weight": kb["learned_keywords"][kw]}


@router.put("/keywords")
async def update_keyword(data: dict):
    """更新关键词权重: {"keyword": "广告词", "weight": 5.0}"""
    kw = data.get("keyword", "").strip()
    weight = float(data.get("weight", 0))
    if not kw:
        raise HTTPException(400, "关键词不能为空")
    kb = learner._load_knowledge()
    if kw in kb["learned_keywords"]:
        kb["learned_keywords"][kw] = min(max(weight, 0), 10.0)
        learner._save_knowledge(kb)
        return {"ok": True, "keyword": kw, "weight": kb["learned_keywords"][kw]}
    raise HTTPException(404, "关键词不存在")


@router.delete("/keywords/{keyword}")
async def delete_keyword(keyword: str):
    """删除关键词"""
    kb = learner._load_knowledge()
    if keyword in kb.get("learned_keywords", {}):
        del kb["learned_keywords"][keyword]
        learner._save_knowledge(kb)
        return {"ok": True}
    raise HTTPException(404, "关键词不存在")


@router.post("/keywords/batch")
async def batch_add_keywords(data: dict):
    """批量添加关键词: {"keywords": ["词1","词2"], "weight": 3.0}"""
    kws = data.get("keywords", [])
    weight = float(data.get("weight", 3.0))
    kb = learner._load_knowledge()
    count = 0
    for kw in kws:
        kw = kw.strip()
        if kw and len(kw) >= 2:
            kb["learned_keywords"][kw] = min(kb["learned_keywords"].get(kw, 0) + weight, 10.0)
            count += 1
    learner._save_knowledge(kb)
    return {"ok": True, "added": count}


@router.post("/keywords/cleanup")
async def cleanup_keywords(data: dict = None):
    """清理低权重/无效关键词: {"min_weight": 0.5}"""
    min_w = float((data or {}).get("min_weight", 0.5))
    kb = learner._load_knowledge()
    before = len(kb.get("learned_keywords", {}))
    kb["learned_keywords"] = {k: v for k, v in kb.get("learned_keywords", {}).items() if v >= min_w}
    after = len(kb["learned_keywords"])
    learner._save_knowledge(kb)
    return {"ok": True, "before": before, "after": after, "removed": before - after}


# ==================== 主人审批 ====================

@router.get("/guardian/pending")
async def guardian_pending():
    """获取待审批操作列表"""
    from bot import guardian
    return {"items": guardian.get_pending_actions()}


@router.post("/guardian/approve/{action_id}")
async def guardian_approve(action_id: str):
    """批准操作"""
    from bot import guardian
    detail = guardian.approve_action(action_id)
    if not detail:
        raise HTTPException(404, "审批不存在或已过期")
    # 执行操作
    import bot as _bot
    if _bot.bot_instance:
        if detail["action"] == "kick":
            try:
                await _bot.bot_instance._call("set_group_kick",
                    {"group_id": detail["group_id"], "user_id": detail["user_qq"]})
                await _bot.bot_instance.send_group_msg(detail["group_id"],
                    f"👢 {detail['user_name']}({detail['user_qq']}) 已被移出群聊\n原因: {detail['reason']}")
            except Exception as e:
                raise HTTPException(500, f"踢人失败: {e}")
        elif detail["action"] == "blacklist":
            from bot import blacklist
            blacklist.add_to_blacklist(detail["user_qq"], detail["user_name"],
                detail["group_id"], detail["reason"], "ban", 0)
            await _bot.bot_instance.send_group_msg(detail["group_id"],
                f"🚫 {detail['user_name']}({detail['user_qq']}) 已被拉黑\n原因: {detail['reason']}")
    return {"ok": True, "detail": detail}


@router.post("/guardian/reject/{action_id}")
async def guardian_reject(action_id: str):
    """拒绝操作"""
    from bot import guardian
    detail = guardian.reject_action(action_id)
    if not detail:
        raise HTTPException(404, "审批不存在或已过期")
    return {"ok": True, "detail": detail}


# ==================== 表情库 ====================

@router.get("/stickers")
async def list_stickers(
    group_id: int = 0, tag: str = "", sticker_type: str = "",
    limit: int = 50, offset: int = 0,
):
    """列出表情库"""
    items = sticker.list_stickers(
        group_id=group_id, tag=tag, sticker_type=sticker_type,
        limit=limit, offset=offset,
    )
    return {"items": items, "total": len(items)}


@router.get("/stickers/search")
async def search_stickers(q: str, limit: int = 20):
    """搜索表情"""
    return {"items": sticker.search_stickers(q, limit=limit)}


@router.get("/stickers/stats")
async def sticker_stats():
    """表情库统计"""
    return sticker.get_sticker_stats()


@router.get("/stickers/group/{group_id}")
async def group_stickers(group_id: int, limit: int = 30):
    """获取某群常用表情"""
    return {"items": sticker.get_group_stickers(group_id, limit=limit)}


@router.post("/stickers")
async def add_sticker(data: dict):
    """手动添加表情"""
    sid = sticker.add_sticker(
        sticker_type=data.get("sticker_type", "face"),
        cq_code=data.get("cq_code", ""),
        tags=data.get("tags", []),
        group_id=data.get("group_id", 0),
        added_by=data.get("added_by", 0),
        description=data.get("description", ""),
    )
    return {"ok": True, "id": sid}


@router.delete("/stickers/{sticker_id}")
async def delete_sticker(sticker_id: int):
    """删除表情"""
    ok = sticker.remove_sticker(sticker_id)
    return {"ok": ok}


# ==================== 群级人设 ====================

@router.get("/group-personality/{group_id}")
async def get_group_personality(group_id: int):
    """获取某群的性格配置"""
    return group_personality.get_group_personality(group_id)


@router.post("/group-personality/{group_id}")
async def save_group_personality(group_id: int, data: dict):
    """保存某群的性格配置"""
    group_personality.save_group_personality(group_id, data)
    return {"ok": True}


@router.delete("/group-personality/{group_id}")
async def delete_group_personality(group_id: int):
    """重置某群的性格配置"""
    group_personality.delete_group_personality(group_id)
    return {"ok": True}


@router.get("/group-personalities")
async def list_group_personalities():
    """列出所有有自定义配置的群"""
    return {"items": group_personality.list_all_groups()}


# ==================== 影响力 ====================

@router.get("/influence/{group_id}")
async def get_influence(group_id: int, limit: int = 20):
    """获取某群影响力排行"""
    return {"items": influence.get_top_influencers(group_id, limit=limit)}


@router.get("/influence/{group_id}/{user_qq}")
async def get_user_influence(group_id: int, user_qq: int):
    """获取某人在某群的影响力"""
    return {"score": influence.get_influence(group_id, user_qq)}


# ==================== 训练数据（网页手动喂给AI） ====================

@router.get("/training-data/stats")
async def training_data_stats():
    """获取训练数据统计"""
    return db.training_data_stats()


@router.get("/training-data")
async def list_training_data(
    label: str = "",
    category: str = "",
    applied: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """列出训练数据"""
    items = db.training_data_list(
        label=label, category=category, applied=applied,
        limit=page_size, offset=(page - 1) * page_size,
    )
    stats = db.training_data_stats()
    return {"items": items, "total": stats["total"], "page": page, "page_size": page_size}


@router.get("/training-data/search")
async def search_training_data(q: str, limit: int = 50):
    """搜索训练数据"""
    return {"items": db.training_data_search(q, limit=limit)}


@router.post("/training-data")
async def add_training_data(data: dict):
    """添加一条训练数据"""
    tid = db.training_data_add(
        message_text=data.get("message_text", ""),
        label=data.get("label", "violation"),
        category=data.get("category", ""),
        keywords=data.get("keywords", ""),
        notes=data.get("notes", ""),
    )
    return {"ok": True, "id": tid}


@router.put("/training-data/{tid}")
async def update_training_data(tid: int, data: dict):
    """更新一条训练数据"""
    ok = db.training_data_update(
        tid=tid,
        message_text=data.get("message_text", ""),
        label=data.get("label", ""),
        category=data.get("category", ""),
        keywords=data.get("keywords", ""),
        notes=data.get("notes", ""),
    )
    if not ok:
        raise HTTPException(404, "训练数据不存在")
    return {"ok": True}


@router.delete("/training-data/{tid}")
async def delete_training_data(tid: int):
    """删除一条训练数据"""
    ok = db.training_data_delete(tid)
    if not ok:
        raise HTTPException(404, "训练数据不存在")
    return {"ok": True}


@router.post("/training-data/batch")
async def batch_add_training_data(data: dict):
    """批量添加训练数据
    请求体: {"items": [{"message_text": "...", "label": "violation", ...}, ...]}
    也支持纯文本批量导入: {"text": "消息1\\n消息2\\n消息3", "label": "violation"}
    """
    if "text" in data:
        # 纯文本批量导入模式
        text = data["text"]
        label = data.get("label", "violation")
        category = data.get("category", "")
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        items = [{"message_text": l, "label": label, "category": category} for l in lines]
    elif "items" in data:
        items = data["items"]
    else:
        raise HTTPException(400, "需要 items 或 text 字段")

    if not items:
        raise HTTPException(400, "没有有效的训练数据")

    count = db.training_data_batch_add(items)
    return {"ok": True, "count": count}


@router.post("/training-data/apply")
async def apply_training_data(data: dict = None):
    """将训练数据应用到学习引擎
    可选参数: {"ids": [1,2,3]} 只应用指定ID，否则应用全部未应用的
    """
    if data and "ids" in data:
        result = trainer.apply_training_data(data["ids"])
    else:
        result = trainer.apply_training_data()

    # 应用后刷新 AutoMod ML 模型
    if result.get("applied", 0) > 0:
        automod._build_ml_model()

    return result


@router.delete("/training-data/clear")
async def clear_training_data(applied_only: bool = True):
    """清空训练数据（默认只清空已应用的）"""
    conn = db.get_connection()
    if applied_only:
        conn.execute("DELETE FROM training_data WHERE applied=1")
    else:
        conn.execute("DELETE FROM training_data")
    count = conn.total_changes
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": count}


# ==================== 媒体缓存管理 ====================

@router.get("/media-cache")
async def list_media_cache(limit: int = 50):
    """列出媒体分析缓存"""
    media_reader._load_cache()
    items = list(media_reader._media_cache.items())[:limit]
    return {"items": [{"key": k, **v} for k, v in items], "total": len(media_reader._media_cache)}


@router.delete("/media-cache")
async def clear_media_cache():
    """清空媒体分析缓存"""
    media_reader._media_cache.clear()
    media_reader._save_cache()
    return {"ok": True}


# ==================== Bot 全局设置 ====================

@router.get("/bot/settings")
async def get_bot_settings():
    """获取所有 Bot 运行时设置"""
    from bot import settings as bot_settings
    s = bot_settings.load()
    s["bot_qq"] = config.BOT_QQ
    s["owner_qq"] = config.OWNER_QQ
    s["web_host"] = config.WEB_HOST
    s["web_port"] = config.WEB_PORT
    s["auto_mod_enabled"] = config.AUTO_MOD_ENABLED
    s["llm_mod_enabled"] = config.LLM_MOD_ENABLED
    return s


@router.post("/bot/settings")
async def save_bot_settings(data: dict):
    """保存 Bot 运行时设置"""
    from bot import settings as bot_settings
    from bot import personality as _p

    allowed = {"bot_name", "auto_accept_invite", "chat_context_size",
               "owner_notifications", "auto_actions",
               "reply_policy", "quiet_hours"}
    updates = {}
    for k in allowed:
        if k in data:
            updates[k] = data[k]

    if updates:
        bot_settings.save(updates)

    # 同步到相关模块
    if "bot_name" in updates:
        config.BOT_NAME = updates["bot_name"]
        # 更新 AI 人设名称
        pcfg = _p.load()
        pcfg["name"] = updates["bot_name"]
        _p.save(pcfg)
        # 同步写回 .env，防止重启后名字丢失
        _update_env_file("BOT_NAME", updates["bot_name"])

    if "chat_context_size" in updates:
        config.CHAT_CONTEXT_SIZE = updates["chat_context_size"]
        ms = memory.load_model_settings()
        ms["context_size"] = updates["chat_context_size"]
        memory.save_model_settings(ms)

    return {"ok": True, "updated": list(updates.keys())}


# ==================== 群成员管理 ====================

@router.get("/groups/{group_id}/members")
async def get_group_members(group_id: int):
    """获取群成员列表"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    try:
        r = await _bot.bot_instance._call("get_group_member_list",
            {"group_id": group_id})
        members = r.get("data", []) if r.get("status") == "ok" else []
        # 补充违规次数
        for m in members:
            uid = m.get("user_id", 0)
            violations = db.query_violations(group_id=group_id, violator_qq=uid, limit=50)
            m["violation_count"] = len(violations)
            m["is_blacklisted"] = blacklist.is_blacklisted(uid, group_id)
        return {"items": members, "total": len(members)}
    except Exception as e:
        raise HTTPException(500, f"获取成员失败: {e}")


@router.post("/groups/{group_id}/mute")
async def mute_member(group_id: int, data: dict):
    """禁言群成员: {"user_id": 123, "duration": 600}（秒）"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    user_id = data.get("user_id")
    duration = int(data.get("duration", 600))
    if not user_id:
        raise HTTPException(400, "缺少 user_id")
    ok = await _bot.bot_instance.set_group_ban(group_id, user_id, duration)
    if ok:
        logger.info(f"Web UI 禁言: 群{group_id} {user_id} {duration}s")
        return {"ok": True, "user_id": user_id, "duration": duration}
    raise HTTPException(500, "禁言失败，请确认Bot是群管理员")


@router.post("/groups/{group_id}/unmute")
async def unmute_member(group_id: int, data: dict):
    """解除禁言: {"user_id": 123}"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    user_id = data.get("user_id")
    if not user_id:
        raise HTTPException(400, "缺少 user_id")
    ok = await _bot.bot_instance.set_group_ban(group_id, user_id, 0)
    if ok:
        return {"ok": True, "user_id": user_id}
    raise HTTPException(500, "解除禁言失败")


@router.post("/groups/{group_id}/kick")
async def kick_member(group_id: int, data: dict):
    """踢出群成员: {"user_id": 123}"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    user_id = data.get("user_id")
    reject = data.get("reject_join", False)
    if not user_id:
        raise HTTPException(400, "缺少 user_id")
    try:
        await _bot.bot_instance._call("set_group_kick",
            {"group_id": group_id, "user_id": user_id,
             "reject_add_request": reject})
        logger.info(f"Web UI 踢人: 群{group_id} {user_id}")
        return {"ok": True, "user_id": user_id}
    except Exception as e:
        raise HTTPException(500, f"踢人失败: {e}")


@router.post("/groups/{group_id}/blacklist-member")
async def blacklist_member(group_id: int, data: dict):
    """踢出并拉黑: {"user_id": 123, "user_name": "", "reason": ""}"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    user_id = data.get("user_id")
    user_name = data.get("user_name", "")
    reason = data.get("reason", "Web UI 操作")
    if not user_id:
        raise HTTPException(400, "缺少 user_id")
    blacklist.add_to_blacklist(user_id, user_name, group_id, reason, "ban", 0)
    try:
        await _bot.bot_instance._call("set_group_kick",
            {"group_id": group_id, "user_id": user_id, "reject_add_request": True})
    except Exception:
        pass
    try:
        await _bot.bot_instance.set_group_ban(group_id, user_id, 3600)
    except Exception:
        pass
    logger.info(f"Web UI 拉黑+踢: 群{group_id} {user_id} {reason}")
    return {"ok": True, "user_id": user_id}


# ==================== Bot 控制 ====================

@router.get("/bot/status")
async def bot_status():
    """获取 Bot 实时状态"""
    import bot as _bot
    if _bot.bot_instance:
        bot = _bot.bot_instance
        return {
            "connected": bot._ws is not None and not (bot._ws.closed if hasattr(bot._ws, 'closed') else True),
            "bot_qq": config.BOT_QQ,
            "owner_qq": config.OWNER_QQ,
            "bot_name": config.BOT_NAME,
            "auto_mod_enabled": config.AUTO_MOD_ENABLED,
            "llm_mod_enabled": config.LLM_MOD_ENABLED,
        }
    return {"connected": False, "bot_qq": config.BOT_QQ}


@router.get("/groups/{group_id}/info")
async def get_group_info(group_id: int):
    """获取群详细信息"""
    import bot as _bot
    if not _bot.bot_instance:
        raise HTTPException(503, "Bot 未连接")
    try:
        info = await _bot.bot_instance.get_group_info(group_id)
        if info:
            # 补充违规统计
            stats = db.get_violation_stats(group_id=group_id)
            bl_count = len(blacklist.list_all(group_id))
            mode = config.get_group_mode(group_id)
            info["violation_total"] = stats["total"]
            info["blacklist_count"] = bl_count
            info["mode"] = mode
        return info or {}
    except Exception as e:
        raise HTTPException(500, f"获取群信息失败: {e}")


# ==================== AI 大脑状态 ====================

@router.get("/ai-brain")
async def get_ai_brain():
    """获取 AI 的心情、学到的知识、性格（兼容旧接口）"""
    from bot import ai_brain
    brain = ai_brain.load_brain()
    mood_emoji = {"excited": "🤩", "happy": "😊", "neutral": "😐", "grumpy": "😒", "sad": "😢"}
    return {
        **brain,
        "mood_emoji": mood_emoji.get(brain["mood"], "😐"),
        "mood_bar": "█" * int(brain["mood_score"] * 20) + "░" * (20 - int(brain["mood_score"] * 20)),
    }


@router.get("/consciousness/time-facts")
async def get_time_facts():
    """当前注入给模型的"时间事实"——用来核对 Bot 会不会说出跟时间矛盾的话"""
    from bot import consciousness as cs
    return {
        "time_facts": cs.get_time_facts_prompt(),
        "time_of_day": cs.get_time_of_day_name(),
        "quiet_now": cs.is_quiet_hours(),
        "can_speak": cs.can_speak_proactively(cs.get_state())[0],
        "block_reason": cs.can_speak_proactively(cs.get_state())[1],
    }


@router.get("/consciousness")
async def get_consciousness():
    """获取完整意识状态：情绪、节律、日记、关系、行为决策"""
    from bot import consciousness as cs
    state = cs.get_state()

    # 情绪 emoji
    emoji_map = {
        "excited": "🤩", "happy": "😊", "neutral_happy": "🙂",
        "neutral": "😐", "content": "😌",
        "bored": "🥱", "lonely": "🥺", "tired": "😴",
        "grumpy": "😒", "irritated": "😤", "curious": "🤔",
        "sleeping": "💤",
    }
    mood = cs.get_dominant_mood(state)
    mood_score = cs.get_mood_score(state)
    behavior = cs.decide_behavior(state)
    circadian = cs.get_circadian_modifiers()

    # 情绪百分比 + 变化趋势
    emotions = {}
    for k, v in state["emotions"].items():
        # 判断趋势（简化：通过值离 0.5 多远来判断 "强度"）
        if v > 0.7:
            trend = "high"
        elif v > 0.55:
            trend = "above_avg"
        elif v > 0.45:
            trend = "avg"
        elif v > 0.3:
            trend = "below_avg"
        else:
            trend = "low"
        emotions[k] = {
            "value": v,
            "percent": int(v * 100),
            "bar": "▓" * int(v * 10) + "░" * (10 - int(v * 10)),
            "bar_width": int(v * 100),
            "trend": trend,
        }

    # 情绪中文名
    emotion_labels = {
        "joy": "快乐", "calm": "平静", "energy": "精力",
        "curiosity": "好奇心", "social": "社交欲",
        "irritation": "烦躁", "boredom": "无聊", "loneliness": "孤独",
        "satisfaction": "满足感",
    }

    # 关系
    relationships = []
    rels = state.get("relationships", {})
    for name, r in sorted(rels.items(), key=lambda x: x[1].get("closeness", 0), reverse=True):
        relationships.append({
            "name": name,
            "closeness": r.get("closeness", 0.5),
            "closeness_pct": int(r.get("closeness", 0.5) * 100),
            "impression": r.get("impression", ""),
            "interactions": r.get("interactions", 0),
            "last": r.get("last_interaction", ""),
        })

    # 今天
    today = state.get("today", {})
    today_date = today.get("date", "")
    if not today_date:
        from datetime import datetime
        today_date = datetime.now().strftime("%Y-%m-%d")

    return {
        "ok": True,
        "mood": mood,
        "mood_emoji": emoji_map.get(mood, "😐"),
        "mood_score": mood_score,
        "mood_score_pct": int(mood_score * 100),
        "sleeping": state.get("sleeping", False),
        "time_of_day": cs.get_time_of_day_name(),
        "circadian": str(circadian),
        "emotions": emotions,
        "emotion_labels": emotion_labels,
        "behavior": behavior,
        "today": {
            "date": today_date,
            "messages_sent": today.get("messages_sent", 0),
            "interactions": today.get("interactions", 0),
            "praised": today.get("praised", 0),
            "insulted": today.get("insulted", 0),
            "ignored": today.get("ignored_count", 0),
            "highlights": today.get("highlights", [])[-5:],
            "topics": today.get("interesting_topics", [])[-5:],
        },
        "traits": state.get("traits", {}),
        "relationships": relationships[:15],
        "diary": state.get("diary", [])[:20],
        "current_thought": state.get("current_thought", ""),
        "personality": {
            "extroversion": state.get("traits", {}).get("extroversion", 0.5),
            "agreeableness": state.get("traits", {}).get("agreeableness", 0.5),
            "openness": state.get("traits", {}).get("openness", 0.5),
            "playfulness": state.get("traits", {}).get("playfulness", 0.5),
            "stubbornness": state.get("traits", {}).get("stubbornness", 0.5),
        },
    }


@router.post("/ai-brain/mood")
async def set_ai_mood(data: dict):
    """手动调节 AI 心情: {"change": "happier"}"""
    from bot import ai_brain
    change = data.get("change", "neutral")
    impacts = {"happier": 0.15, "sadder": -0.15, "excited": 0.25, "grumpy": -0.2, "reset": -99}
    if change == "reset":
        brain = ai_brain.load_brain()
        brain["mood_score"] = 0.5
        brain["mood"] = "neutral"
        ai_brain.save_brain(brain)
    else:
        ai_brain.update_mood(change, impacts.get(change, 0))
    return await get_ai_brain()


# ==================== 联网搜索 ====================

@router.get("/web-search/test")
async def web_search_test(q: str = ""):
    """测试 SearXNG 联网搜索"""
    if not q:
        raise HTTPException(400, "请提供搜索关键词 q")
    from bot import web_search
    results = await web_search.search(q, max_results=5)
    return {
        "query": q,
        "count": len(results),
        "results": results,
    }


@router.post("/web-search/toggle")
async def web_search_toggle():
    """开关联网搜索"""
    from bot import memory
    ms = memory.load_model_settings()
    ms["web_search_enabled"] = not ms.get("web_search_enabled", True)
    memory.save_model_settings(ms)
    return {"enabled": ms["web_search_enabled"]}


# ==================== 数据导出 ====================

@router.get("/export/violations")
async def export_violations(
    group_id: int = 0,
    format: str = "json",
    limit: int = 10000,
):
    """导出违规记录"""
    records = db.query_violations(
        group_id=group_id if group_id else None,
        limit=limit
    )
    if format == "csv":
        import io, csv as csv_mod
        output = io.StringIO()
        writer = csv_mod.writer(output)
        writer.writerow(["ID", "时间", "群号", "违规者QQ", "违规者名称", "消息内容", "备注", "关键词", "置信度", "来源"])
        for r in records:
            writer.writerow([
                r.get("id"), r.get("recorded_at"), r.get("group_id"),
                r.get("violator_qq"), r.get("violator_name"),
                r.get("raw_message"), r.get("violation_note"),
                ", ".join(r.get("keywords", [])), r.get("confidence"),
                r.get("source"),
            ])
        from fastapi.responses import Response
        return Response(
            output.getvalue(),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename=violations.csv"},
        )
    return {"items": records, "total": len(records)}


@router.get("/export/training-data")
async def export_training_data(format: str = "json"):
    """导出训练数据"""
    items = db.training_data_list(limit=10000)
    if format == "csv":
        import io, csv as csv_mod
        output = io.StringIO()
        writer = csv_mod.writer(output)
        writer.writerow(["ID", "消息", "标签", "分类", "关键词", "备注", "已应用", "创建时间"])
        for r in items:
            writer.writerow([
                r.get("id"), r.get("message_text"), r.get("label"),
                r.get("category"), r.get("keywords"), r.get("notes"),
                "是" if r.get("applied") else "否", r.get("created_at"),
            ])
        from fastapi.responses import Response
        return Response(
            output.getvalue(),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename=training_data.csv"},
        )
    return {"items": items, "total": len(items)}


# ==================== LLM 审核 ====================

@router.get("/llm-mod/stats")
async def llm_mod_stats():
    """获取 LLM 审核统计"""
    return llm_mod.get_moderation_stats()


@router.get("/llm-mod/logs")
async def llm_mod_logs(limit: int = 50):
    """获取 LLM 审核日志"""
    return {"items": llm_mod.get_moderation_logs(limit=limit)}


@router.post("/llm-mod/test")
async def llm_mod_test(data: dict):
    """测试消息的 LLM 审核结果
    请求: {"message": "待测试消息", "group_id": 0}
    """
    msg = data.get("message", "")
    gid = data.get("group_id", 0)
    if not msg:
        raise HTTPException(400, "消息不能为空")
    result = await llm_mod.llm_moderate(msg, group_id=gid, force_llm=True)
    return result


@router.post("/llm-mod/toggle")
async def llm_mod_toggle():
    """开关 LLM 审核"""
    config.LLM_MOD_ENABLED = not config.LLM_MOD_ENABLED
    # 同步保存到模型设置
    ms = memory.load_model_settings()
    ms["moderation_enabled"] = config.LLM_MOD_ENABLED
    memory.save_model_settings(ms)
    return {"enabled": config.LLM_MOD_ENABLED}


@router.get("/llm-mod/config")
async def llm_mod_config():
    """获取 LLM 审核配置"""
    ms = memory.load_model_settings()
    return {
        "enabled": config.LLM_MOD_ENABLED,
        "model": ms.get("moderation_model", "MiniMax-M3"),
        "minimax_api_key": _mask(ms.get("minimax_api_key", "")),
        "minimax_api_url": ms.get("minimax_api_url", "https://api.minimaxi.com/anthropic"),
    }


@router.post("/llm-mod/config")
async def llm_mod_save_config(data: dict):
    """保存 LLM 审核配置"""
    ms = memory.load_model_settings()
    allowed = {"moderation_enabled", "moderation_model", "minimax_api_key", "minimax_api_url"}
    for k, v in data.items():
        if k in allowed:
            # 跳过脱敏值
            if "api_key" in k and "****" in str(v):
                continue
            ms[k] = v
    if "moderation_enabled" in data:
        config.LLM_MOD_ENABLED = bool(data["moderation_enabled"])
    memory.save_model_settings(ms)
    return {"ok": True}


# ==================== 安全设置 ====================

@router.get("/security/status")
async def security_status():
    """获取安全状态"""
    return {
        "encryption_active": bool(config.SECRET_KEY or os.path.exists(
            os.path.join(config.DATA_DIR, ".master_key")
        )),
        "jwt_enabled": True,
        "master_key_hash": security.get_master_key_hash() if security._MASTER_KEY else "未初始化",
        "web_password_set": config.WEB_PASSWORD != "admin",
        "allowed_ips": config.WEB_ALLOWED_IPS,
        "has_encrypted_secrets": os.path.exists(os.path.join(config.DATA_DIR, "secrets.enc")),
    }


@router.post("/security/change-password")
async def change_password(data: dict):
    """修改 Web UI 登录密码"""
    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")
    if not new_password or len(new_password) < 6:
        raise HTTPException(400, "新密码至少6位")
    if old_password != config.WEB_PASSWORD:
        raise HTTPException(403, "旧密码错误")

    config.WEB_PASSWORD = new_password
    # 更新 .env 文件
    _update_env_file("WEB_PASSWORD", new_password)
    logger.info("Web UI 密码已更改")
    return {"ok": True}


@router.post("/security/encrypt-secrets")
async def encrypt_secrets(data: dict):
    """
    加密存储 API keys。
    请求: {"api_key": "sk-xxx", "minimax_api_key": "sk-xxx", ...}
    """
    if not security._MASTER_KEY:
        raise HTTPException(500, "安全模块未初始化")
    ok = store_encrypted_secrets(data)
    if ok:
        return {"ok": True, "message": "API keys 已加密存储"}
    raise HTTPException(500, "加密存储失败")


@router.get("/security/encrypt-secrets")
async def get_encrypted_secrets():
    """获取解密后的 API keys（用于查看）"""
    secrets = load_encrypted_secrets()
    if secrets:
        masked = {k: _mask(v) for k, v in secrets.items()}
        return {"ok": True, "secrets": masked}
    return {"ok": True, "secrets": {}, "message": "没有加密存储的 keys"}


# ==================== WebSocket ====================

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query("")):
    # WebSocket 走不了 Authorization 头，token 从查询串取。
    # 之前这里完全不校验——任何人连上就能收推送。
    if not verify_jwt_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
    except Exception:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def _broadcast(data: dict):
    """向所有连接的 WebSocket 客户端推送事件"""
    for ws in _ws_clients[:]:
        try:
            await ws.send_json(data)
        except Exception:
            _ws_clients.remove(ws)


# ==================== 辅助 ====================

def _mask(value: str, show: int = 4) -> str:
    """脱敏显示字符串"""
    if not value:
        return ""
    if len(value) <= show * 2:
        return "*" * len(value)
    return value[:show] + "*" * (len(value) - show * 2) + value[-show:]


def _update_env_file(key: str, value: str):
    """更新 .env 文件中的某个配置项"""
    import os as _os
    env_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
    if not _os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} "):
                lines[i] = f"{key}={value}\n"
                updated = True
                break
        if not updated:
            lines.append(f"\n{key}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.error(f"更新 .env 失败: {e}")


# ==================== 环境配置 (.env) 管理 ====================

# .env 中不可通过 Web 修改的敏感字段
_SENSITIVE_ENV_KEYS = {"WEB_PASSWORD", "SECRET_KEY", "LLM_API_KEY",
                       "NAPCAT_HTTP_TOKEN", "NAPCAT_WS_TOKEN"}
# .env 中不可见的敏感字段
_HIDDEN_ENV_KEYS = {"LLM_API_KEY", "NAPCAT_HTTP_TOKEN", "NAPCAT_WS_TOKEN",
                    "SECRET_KEY"}

def _parse_env_file() -> dict[str, str]:
    """解析 .env 文件，返回 {KEY: value} 字典"""
    import os as _os
    env_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
    result = {}
    if not _os.path.exists(env_path):
        return result
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 去除引号
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                result[key] = value
    return result


@router.get("/env")
async def get_env_settings():
    """获取 .env 所有配置（敏感字段脱敏显示）"""
    env = _parse_env_file()
    result = {}
    for k, v in env.items():
        if k in _HIDDEN_ENV_KEYS:
            result[k] = _mask(v, show=3) if v else ""
        elif k == "WEB_PASSWORD":
            result[k] = "••••••••" if v else ""
        else:
            result[k] = v
    result["_editable"] = [k for k in env if k not in _SENSITIVE_ENV_KEYS]
    result["_sensitive"] = list(_SENSITIVE_ENV_KEYS & set(env.keys()))
    return result


@router.post("/env")
async def save_env_settings(data: dict):
    """批量更新 .env 配置（敏感字段自动跳过）"""
    import os as _os
    env_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
    if not _os.path.exists(env_path):
        raise HTTPException(404, ".env 文件不存在")

    skipped = []
    updated = {}

    for k, v in data.items():
        if k.startswith("_"):
            continue
        if k in _SENSITIVE_ENV_KEYS:
            skipped.append(k)
            continue
        v = str(v).strip()
        updated[k] = v
        _update_env_file(k, v)

    return {"ok": True, "updated": list(updated.keys()), "skipped": skipped}


@router.post("/env/reload")
async def reload_env_settings(data: dict):
    """
    重载单个敏感字段（需要当前密码验证）。
    请求: {"key": "WEB_PASSWORD", "current_password": "xxx", "value": "newpass"}
    """
    key = data.get("key", "")
    current_pw = data.get("current_password", "")
    new_value = data.get("value", "")

    if key not in _SENSITIVE_ENV_KEYS:
        raise HTTPException(400, "该字段可通过普通接口修改，无需密码验证")
    if not current_pw or current_pw != config.WEB_PASSWORD:
        raise HTTPException(403, "当前密码错误")

    _update_env_file(key, str(new_value))
    logger.warning(f"敏感配置已更新: {key} (来自 Web UI)")
    return {"ok": True, "updated": key}


# ==================== 加群验证历史 ====================

@router.get("/verify/history")
async def get_verify_history(limit: int = 50):
    """获取加群验证历史记录"""
    from bot import verify as _verify
    items = _verify.get_verification_history(limit=limit)

    # 统计
    approved = sum(1 for i in items if i["result"] == "approved")
    rejected = sum(1 for i in items if i["result"] == "rejected")
    timeout = sum(1 for i in items if i["result"] == "timeout")
    approved_web = sum(1 for i in items if i["result"] == "approved_web")
    rejected_web = sum(1 for i in items if i["result"] == "rejected_web")
    timeout_web = sum(1 for i in items if i["result"] == "timeout_web")

    return {
        "items": items,
        "total": len(items),
        "stats": {
            "approved": approved,
            "rejected": rejected,
            "timeout": timeout,
            "approved_web": approved_web,
            "rejected_web": rejected_web,
            "timeout_web": timeout_web,
        },
    }


# ==================== 网站认证 — 公开接口 ====================

@router.post("/verify/code/{token}")
async def reveal_web_code(token: str, data: dict, request: Request):
    """
    网站认证：新人点完 Turnstile 后调，校验通过返回词。
    公开接口（无需 JWT），但 token 一次性绑定 (group, user)。
    """
    from bot import verify as _verify
    cf_token = (data or {}).get("cf_token", "")
    if not cf_token:
        raise HTTPException(400, "缺少人机验证 token")
    remote_ip = request.client.host if request.client else ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        remote_ip = forwarded.split(",")[0].strip()

    result = await _verify.reveal_web_code(token, cf_token, remote_ip)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "验证失败"))
    return {"ok": True, "code": result["code"]}


@router.get("/verify/config")
async def verify_public_config():
    """网站认证公开页需要的配置（sitekey）。公开接口。"""
    from bot.config import config
    return {"sitekey": config.TURNSTILE_SITE_KEY}


# ==================== 网站认证 — 比例配置（需 JWT）====================

@router.get("/verify/ratio")
async def get_verify_ratio():
    """获取网站认证模式概率（0-100）。复用 bot.verify._get_web_ratio()（DRY）。"""
    from bot import verify as _verify
    return {"ratio": _verify._get_web_ratio()}


@router.post("/verify/ratio")
async def set_verify_ratio(data: dict):
    """保存网站认证模式概率（0-100）"""
    ratio = data.get("ratio")
    if ratio is None or not isinstance(ratio, int):
        try:
            ratio = int(ratio)
        except Exception:
            raise HTTPException(400, "ratio 必须是 0-100 的整数")
    if not (0 <= ratio <= 100):
        raise HTTPException(400, "ratio 必须是 0-100")

    import os as _os, json as _json
    from bot.config import config
    path = _os.path.join(config.DATA_DIR, "bot_settings.json")
    settings = {}
    try:
        if _os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                settings = _json.load(f)
    except Exception:
        settings = {}
    settings["web_verify_ratio"] = ratio
    try:
        _os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(500, f"保存失败: {e}")
    logger.info(f"网站认证比例 → {ratio}%")
    return {"ok": True, "ratio": ratio}


# ==================== 日志查看 ====================

@router.get("/logs")
async def get_bot_logs(lines: int = 200, offset: int = 0, level: str = ""):
    """
    获取 Bot 运行日志。
    - lines: 返回行数 (默认 200，最大 2000)
    - offset: 跳过前 N 行（倒序前）
    - level: 按日志级别过滤 (DEBUG/INFO/WARNING/ERROR)
    """
    import os as _os
    log_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "bot.log")
    if not _os.path.exists(log_path):
        return {"ok": True, "lines": [], "total": 0, "message": "日志文件不存在"}

    lines = max(1, min(lines, 2000))
    offset = max(0, offset)

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)

        # 按级别过滤
        if level:
            level_upper = level.upper()
            all_lines = [l for l in all_lines if f"| {level_upper} " in l or f"|{level_upper} " in l]

        filtered_total = len(all_lines)

        # 倒序取最新
        start = max(0, len(all_lines) - lines - offset)
        end = len(all_lines) - offset if offset > 0 else len(all_lines)
        result = all_lines[start:end]

        return {
            "ok": True,
            "lines": [l.rstrip("\n").rstrip("\r") for l in result],
            "total": total,
            "filtered_total": filtered_total,
            "returned": len(result),
            "file_exists": True,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
