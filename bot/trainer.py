"""
训练模块 — 从违规数据中学习，持续提升自动审核能力
"""
import json
import logging
import os
import threading
from datetime import datetime

from . import db, automod

logger = logging.getLogger("qqbot")

STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trainer_status.json")


def get_training_status() -> dict:
    """获取训练状态"""
    if os.path.exists(STATUS_PATH):
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    violations = db.get_violation_count()
    samples = db.sample_count_by_label()
    return {
        "violation_count": violations,
        "normal_samples": samples.get("normal", 0),
        "ml_model_ready": os.path.exists(automod.MODEL_PATH),
        "auto_mod_threshold": max(0.25, 0.85 - violations * 0.003),
        "last_trained_at": None,
        "phase": _get_phase(violations),
    }


def _get_phase(count: int) -> str:
    if count < 10: return "冷启动（纯关键词匹配）"
    if count < 30: return "学习中（关键词+权重）"
    if count < 80: return "进阶（混合模型）"
    return "成熟（ML + 关键词双引擎）"


def train_if_ready() -> bool:
    """如果条件满足，触发训练"""
    count = db.get_violation_count()
    if count >= 20 and count % 10 == 0:
        t = threading.Thread(target=automod._build_ml_model, daemon=True)
        t.start()
        _save_status()
        return True
    return False


def collect_normal_sample(group_id: int, user_qq: int, raw_message: str):
    """收集正常消息作为负样本（ML 训练用）"""
    # 只收集长度适中的消息，减少噪声
    if 5 < len(raw_message) < 200:
        db.sample_add(group_id, user_qq, raw_message, "normal")


def _save_status():
    s = get_training_status()
    s["last_trained_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


# ==================== 手动训练数据应用 ====================

def apply_training_data(tids: list[int] = None) -> dict:
    """
    将手动训练数据应用到学习引擎

    - violation 样本: 自动分词提取关键词 → 调用 learner.learn_from_violation
    - normal 样本: 添加到 message_samples 表供 ML 训练
    - 标记 applied=1
    """
    from . import learner, cache

    if tids:
        items = []
        conn = db.get_connection()
        placeholders = ",".join("?" for _ in tids)
        rows = conn.execute(
            f"SELECT * FROM training_data WHERE id IN ({placeholders})",
            tids).fetchall()
        conn.close()
        items = [dict(r) for r in rows]
    else:
        items = db.training_data_get_unapplied()

    if not items:
        return {"ok": False, "message": "没有待应用的训练数据", "applied": 0}

    applied_ids = []
    violation_count = 0
    normal_count = 0

    for item in items:
        msg = item["message_text"].strip()
        if not msg:
            continue

        if item["label"] == "violation":
            # 分词提取关键词（过滤掉技术术语和垃圾词）
            try:
                import jieba
                from .learner import _is_junk_keyword
                words = [w for w in jieba.lcut(msg) if len(w) >= 2 and not _is_junk_keyword(w)]
            except Exception:
                words = [w for w in msg.split() if len(w) >= 2]

            # 使用用户指定的关键词（也要过滤）
            user_keywords = []
            if item.get("keywords"):
                from .learner import _is_junk_keyword
                user_keywords = [k.strip() for k in item["keywords"].split(",")
                               if k.strip() and not _is_junk_keyword(k.strip())]

            all_keywords = list(set(user_keywords + words))
            learner.learn_from_violation(msg, all_keywords, source="manual")

            # 同时添加违规记录（让 ML 模型计数器增加）
            db.add_violation(
                message_id=0, group_id=0, group_name="",
                violator_qq=0, violator_name="手动训练",
                raw_message=msg,
                violation_note=f"网页手动训练 [{item.get('category', '')}]",
                keywords=all_keywords,
                recorded_by=0, confidence=1.0, source="manual",
            )
            violation_count += 1
        elif item["label"] == "normal":
            # 作为负样本加入 message_samples
            db.sample_add(0, 0, msg, "normal")
            normal_count += 1

        applied_ids.append(item["id"])

    # 标记为已应用
    if applied_ids:
        db.training_data_mark_applied(applied_ids)

    # 清除缓存让下次评分使用新知识
    cache.learned_knowledge_cache.invalidate("knowledge")

    logger.info(
        f"手动训练数据应用完成: {violation_count} 条违规, "
        f"{normal_count} 条正常, 共 {len(applied_ids)} 条"
    )

    # 如果违规样本足够，触发 ML 模型重建
    total_violations = db.get_violation_count()
    if total_violations >= 20:
        t = threading.Thread(target=automod._build_ml_model, daemon=True)
        t.start()

    return {
        "ok": True,
        "message": f"已应用 {len(applied_ids)} 条训练数据",
        "applied": len(applied_ids),
        "violation_applied": violation_count,
        "normal_applied": normal_count,
    }
