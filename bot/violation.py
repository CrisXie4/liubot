"""
违规管理模块
- 记录违规消息
- 撤回违规消息
- 分析违规特征
"""
import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Optional

import jieba

from . import db, learner, blacklist, cache
from .config import config
from .onebot_client import OneBotClient

logger = logging.getLogger("qqbot")

# ---------- 违禁词 / 敏感词库 ----------
# 可自定义扩展，也可以从文件加载
DEFAULT_SENSITIVE_WORDS = {
    # 广告类
    "广告", "推广", "加群", "加我", "免费领取", "点击链接",
    "扫码", "兼职", "日赚", "刷单", "返利", "代理", "微商",
    "引流", "私聊我", "加微信", "加QQ", "看主页",
    # 辱骂/人身攻击
    "傻逼", "sb", "脑残", "废物", "垃圾", "cnm", "操你妈",
    "他妈", "草泥马", "nmsl", "司马", "畜生", "狗日的",
    # 色情
    "裸聊", "约炮", "色情", "黄色", "av", "番号",
    # 政治敏感
    "翻墙", "vpn推荐",
    # 赌博
    "赌博", "赌场", "彩票预测", "六合彩",
}

# 通用垃圾广告模式（正则）
AD_PATTERNS = [
    re.compile(r"加\s*(群|君\S?羊|羣|Q+群?)\s*\S*\d{5,}"),
    re.compile(r"[vV][xX]\s*[：:]\s*\S+"),
    re.compile(r"[qQ]{2,}\s*[：:]\s*\d{5,}"),
    re.compile(r"(http[s]?://|www\.)\S+"),
    re.compile(r"免费\S{0,3}(领取|送|拿|获取)"),
    re.compile(r"日\s*(入|赚|收)\s*\d{2,}"),
]


# 无害词白名单（日常对话绝不会是广告）
_SAFE_WORDS = {
    # 招呼/道别
    "你好", "谢谢", "再见", "哈哈", "好的", "收到", "明白", "了解",
    "早安", "晚安", "拜拜", "嗨", "嗯嗯", "哦哦", "来了", "走了",
    # 管理命令
    "取消", "拉黑", "解禁", "禁言", "帮助", "统计", "查询", "违规",
    "群友", "管理", "模式", "自动",
    # 日常
    "活着", "不行", "笑死", "原来", "个人", "辅助", "帮忙", "请问",
    "有人", "有没有", "能不能", "怎么", "什么", "哪里", "为什么",
    "可以", "不可以", "还行", "不错", "厉害", "牛逼", "高手",
    "今天", "明天", "昨天", "现在", "一会", "等等", "稍等",
    "知道", "不知道", "没事", "没关系", "无所谓",
    "真的", "假的", "确实", "可能", "应该", "大概", "也许",
    "已经", "还没", "马上", "刚刚", "刚才", "之前", "以后",
    "一个", "这个", "那个", "怎么", "这么", "那么",
    # CQ/格式
    "CQ", "at", "qq", "image", "file", "summary", "type", "sub", "url",
    "http", "https", "com", "www", "png", "jpg", "jpeg", "gif", "mp4",
    "动画", "表情", "下载", "fileid", "rkey", "appid", "multimedia",
    # 机器名
    "Xie", "小Q",
    # 群聊常见
    "群主", "群友", "大家", "各位", "伙伴",
    "聊天", "说话", "发言", "冒泡", "潜水",
    "上班", "下班", "吃饭", "睡觉", "打游戏", "学习",
    # 情绪
    "开心", "难过", "无聊", "困了", "累了", "醉了",
    # 回复类
    "嗯", "啊", "哦", "额", "呃", "唉", "哎",
}


def _strip_cq_codes(text: str) -> str:
    """移除 [CQ:...] 格式的QQ代码，只保留纯文本"""
    return re.sub(r"\[CQ:[^\]]+\]", "", text)


def extract_keywords(text: str, top_n: int = 10) -> list[str]:
    """从文本中提取特征关键词"""
    # 先清洗 CQ 代码
    clean = _strip_cq_codes(text)
    if not clean.strip():
        return []

    keywords: list[str] = []

    # 1. 匹配广告正则
    for pat in AD_PATTERNS:
        for match in pat.finditer(clean):
            kw = match.group()
            if kw not in _SAFE_WORDS and len(kw) >= 2:
                keywords.append(kw)

    # 2. 匹配违禁词库
    for word in DEFAULT_SENSITIVE_WORDS:
        if word in clean and word not in _SAFE_WORDS:
            keywords.append(word)

    # 3. jieba 分词（只保留有意义的词，使用缓存）
    words = cache.cached_jieba_lcut(clean)
    for w in words:
        w = w.strip()
        if len(w) < 2:
            continue
        if w in _SAFE_WORDS:
            continue
        if w.isdigit():
            continue
        if re.match(r"^https?://", w):
            continue
        if w in keywords:
            continue
        if re.match(r"^[一-鿿\w]{2,}$", w):
            keywords.append(w)

    # 去重，取 top_n
    seen = set()
    result = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result[:top_n]


def analyze_violation_message(raw_message: str) -> dict:
    """
    分析一条违规消息，返回特征字典
    """
    keywords = extract_keywords(raw_message)
    length = len(raw_message)

    # 判断违规类型
    violation_type = "其他"
    ad_signals = 0
    abuse_signals = 0

    ad_words = {"广告", "加群", "加微信", "扫码", "推广", "引流", "免费", "私聊"}
    abuse_words = {"傻逼", "sb", "cnm", "操", "草泥马", "脑残", "废物", "垃圾"}

    for kw in keywords:
        if kw in ad_words:
            ad_signals += 1
        if kw in abuse_words:
            abuse_signals += 1

    if ad_signals > abuse_signals:
        violation_type = "广告"
    elif abuse_signals > 0:
        violation_type = "辱骂/攻击"

    return {
        "keywords": keywords,
        "message_length": length,
        "violation_type": violation_type,
        "ad_signals": ad_signals,
        "abuse_signals": abuse_signals,
    }


def build_violation_report(group_id: Optional[int] = None) -> str:
    """生成违规分析报告（文本格式）"""
    stats = db.get_violation_stats(group_id)
    common_kw = db.get_common_keywords(group_id)

    lines = [
        "📊 **违规统计报告**",
        f"累计处理违规: {stats['total']} 条",
        "",
    ]

    if stats["top_violators"]:
        lines.append("🚫 **违规者排行:**")
        for i, u in enumerate(stats["top_violators"][:5], 1):
            name = u["violator_name"] or u["violator_qq"]
            lines.append(f"  {i}. {name} — {u['cnt']} 次")
        lines.append("")

    if common_kw:
        lines.append("🔑 **高频特征词:**")
        kw_str = "、".join(f"{k['keyword']}({k['count']})" for k in common_kw[:10])
        lines.append(f"  {kw_str}")
        lines.append("")

    if stats["hour_distribution"]:
        lines.append("⏰ **活跃时段:**")
        peak = max(stats["hour_distribution"], key=lambda x: x["cnt"])
        lines.append(f"  高峰时段: {peak['hour']}:00 左右")
        lines.append("")

    if stats["recent"]:
        lines.append("📝 **最近 5 条违规:**")
        for r in stats["recent"]:
            name = r["violator_name"] or r["violator_qq"]
            msg = r["raw_message"][:40].replace("\n", " ")
            lines.append(f"  [{r['recorded_at']}] {name}: {msg}...")
        lines.append("")

    return "\n".join(lines)


# =================== 违规处理流程 ===================


async def handle_violation(
    bot: OneBotClient,
    group_id: int,
    operator_qq: int,
    target_msg_id: int,
    note: str = "",
):
    """
    处理一次违规举报

    流程:
    1. 获取目标消息内容
    2. 获取违规者信息
    3. 分析消息特征
    4. 存入数据库
    5. 撤回消息
    6. 回复处理结果
    """
    # --- 1. 获取目标消息 ---
    msg_data = await bot.get_msg(target_msg_id)
    if not msg_data:
        await bot.send_group_msg(group_id, "❌ 无法获取该消息内容（可能已被撤回或超时）")
        return

    raw_message = msg_data.get("raw_message", msg_data.get("message", ""))
    message_segments = []
    if isinstance(raw_message, list):
        message_segments = raw_message
        texts = []
        for seg in raw_message:
            if seg.get("type") == "text":
                texts.append(seg["data"]["text"])
        raw_message = "".join(texts)

    violator_qq = msg_data.get("user_id", msg_data.get("sender", {}).get("user_id", 0))
    violator_name = msg_data.get("sender", {}).get("nickname", "")
    if not violator_name:
        violator_name = msg_data.get("sender", {}).get("card", "")

    # --- 统一内容提取（合并转发 + 图片 + 表情）---
    from . import content_reader, image_mod
    try:
        content = await content_reader.extract_full_content(bot, raw_message, message_segments)
    except Exception as e:
        logger.warning(f"content_reader 提取失败，降级: {e}")
        content = {"text": raw_message if isinstance(raw_message, str) else "",
                   "images": [], "stickers": [], "has_forward": False,
                   "forward": None, "errors": []}

    # 构造 effective_text（含合并转发子消息）
    effective_text = content["text"]
    if content["has_forward"] and content["forward"]:
        effective_text = (effective_text + "\n[合并转发]\n" + content["forward"]["text"]).strip()

    # 收集所有图片（本条 + 转发包内 + 表情）
    all_images = list(content["images"])
    for _s in content["stickers"]:
        if _s.get("url"):
            all_images.append({"url": _s["url"], "file_id": "", "caption_hint": _s.get("summary", "")})
    if content["has_forward"] and content["forward"]:
        all_images += content["forward"]["images"]
        for _s in content["forward"]["stickers"]:
            if _s.get("url"):
                all_images.append({"url": _s["url"], "file_id": "", "caption_hint": _s.get("summary", "")})

    # 走 Vision 三级降级（全审）
    vision_results = []
    if all_images:
        try:
            vision_results = await image_mod._audit_all_media(bot, all_images, caption=effective_text) or []
        except Exception as e:
            logger.warning(f"手动标记图片审核异常: {e}")
            vision_results = []
        for vr in vision_results:
            if not vr:
                continue
            ocr = vr.get("ocr_text", "")
            if ocr:
                effective_text = effective_text + "\n[图片内容] " + ocr

    # 用 effective_text 替代 raw_message 做后续分析
    raw_message = effective_text

    if not raw_message:
        await bot.send_group_msg(group_id, "❌ 消息内容为空")
        return

    # --- 2. 获取群信息 ---
    group_name = ""
    group_info = await bot.get_group_info(group_id)
    if group_info:
        group_name = group_info.get("group_name", "")

    # --- 3. 分析特征 ---
    analysis = analyze_violation_message(raw_message)

    # 把图片 Vision 违规类型并入 keywords（Task 4）
    for vr in vision_results:
        if vr and vr.get("violation_type") not in ("none", None, ""):
            analysis["keywords"] = list(set(
                analysis["keywords"] + [vr.get("violation_type", "")]
            ))[:10]

    # --- 4. 存入数据库 ---
    vid = db.add_violation(
        message_id=target_msg_id,
        group_id=group_id,
        group_name=group_name,
        violator_qq=int(violator_qq),
        violator_name=violator_name,
        raw_message=raw_message,
        violation_note=note or analysis["violation_type"],
        keywords=analysis["keywords"],
        recorded_by=int(operator_qq),
    )

    # --- 4.5 喂给学习引擎 ---
    learner.learn_from_violation(raw_message, analysis["keywords"], source="manual")

    # --- 5. 撤回消息 ---
    recall_ok = await bot.delete_msg(target_msg_id)
    recall_text = "✅ 已撤回" if recall_ok else "⚠️ 撤回失败（可能超2分钟或无权限）"

    # --- 5.5 违规禁言1小时 ---
    mute_ok = False
    try:
        mute_ok = await bot.set_group_ban(group_id, int(violator_qq), 3600)
    except Exception:
        pass
    mute_text = "，已禁言1小时" if mute_ok else ""

    # --- 6. 回复处理结果 ---
    kw_display = "、".join(analysis["keywords"][:8]) if analysis["keywords"] else "无"
    # 脱敏：只显示前5个字符的摘要，不公开完整违规内容
    snippet = raw_message.strip()[:5] if raw_message else ""
    reply = (
        f"🚫 违规已记录 [#{vid}]\n"
        f"违规者: {violator_name}({violator_qq})\n"
        f"类型: {analysis['violation_type']}\n"
        f"特征词: {kw_display}\n"
        f"{recall_text}{mute_text}\n"
        f"内容: {snippet}{'...' if len(raw_message) > 5 else ''}"
    )
    await bot.send_group_msg(group_id, reply)

    # --- 6.5 累计违规 ≥3 次 → 创建踢人审批（不自动踢）---
    records = db.query_violations(group_id=group_id, violator_qq=int(violator_qq))
    if len(records) >= 3:
        from . import guardian
        from .config import config as _cfg
        aid = guardian.create_action_request(
            group_id, int(violator_qq), violator_name,
            action="kick",
            reason=f"累计违规 {len(records)} 次",
            message=raw_message[:200],
        )
        # 通知主人审批
        try:
            await bot.send_private_msg(int(_cfg.OWNER_QQ),
                f"🚨 **待审批: 踢人（手动标记累计）**\n"
                f"群: {group_id}\n"
                f"用户: {violator_name}({violator_qq})\n"
                f"原因: 累计违规 {len(records)} 次\n"
                f"消息: {raw_message[:100]}\n\n"
                f"回复 \"同意 {aid}\" 执行踢人\n"
                f"回复 \"拒绝 {aid}\" 取消"
            )
        except Exception:
            pass
        # 群里也发一条提示（不自动踢，等主人确认）
        await bot.send_group_msg(group_id,
            f"⚠️ {violator_name}({violator_qq}) 已累计违规 {len(records)} 次，"
            f"已提交踢人审批，等待管理员确认")

    logger.info(
        f"违规记录 #{vid}: 群{group_id} {violator_qq} -> {raw_message[:50]}"
    )
