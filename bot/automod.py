"""
自动审核引擎
- 关键词权重评分
- TF-IDF + 朴素贝叶斯分类器（训练后启用）
- 置信度阈值自动调整
"""
import json
import logging
import math
import os
import re
import pickle
from collections import Counter
from typing import Optional

import jieba

from . import db, learner, cache

logger = logging.getLogger("qqbot")

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "automod_model.pkl")

# ─── 违禁词库 ───
SENSITIVE_WORDS = {
    "广告": 1.5, "推广": 1.5, "加群": 1.4, "加我": 1.2, "免费领取": 1.5,
    "点击链接": 1.3, "扫码": 1.4, "兼职": 1.3, "日赚": 1.6, "刷单": 1.4,
    "返利": 1.3, "代理": 1.2, "微商": 1.3, "引流": 1.4, "私聊我": 1.4,
    "加微信": 1.5, "加QQ": 1.4, "看主页": 1.2, "网址": 0.8, "http": 1.0,
    # 昵称广告特征词
    "低价": 1.4, "中转": 1.2, "找我": 0.8, "联系我": 1.2,
    # 注意: "api" 权重降低到 0.4，因为技术讨论中合法出现频率极高
    # 真正可疑的是 "官key"/"出key"/"售key" 等组合词
    "api": 0.4,
    ".xyz": 1.2, ".top": 1.2, ".cc": 1.2, ".cfd": 1.2, "机场": 1.0,
    "节点": 0.8, "流量": 0.7, "套餐": 0.9, "秒开": 0.8, "专线": 0.8,
    "加速": 0.7, "梯子": 0.9, "vpn": 0.9, "clash": 0.8, "订阅": 0.8,
    # API key 贩卖黑话
    "官key": 1.6, "出key": 1.5, "售key": 1.5,
    "白名单": 0.7, "额度": 0.6, "倍率": 0.9, "刀": 0.5,
    # 辱骂
    "傻逼": 1.0, "sb": 1.0, "脑残": 1.0, "废物": 1.0, "垃圾": 0.8,
    "cnm": 1.2, "操你妈": 1.3, "他妈": 0.8, "草泥马": 1.2, "nmsl": 1.5,
    "司马": 1.2, "畜生": 1.0, "狗日的": 1.2,
    # 色情/赌博
    "裸聊": 2.5, "约炮": 2.5, "色情": 2.0, "av番号": 2.5,
    "赌博": 2.0, "赌场": 2.0, "六合彩": 2.0,
}

AD_PATTERNS = [
    (re.compile(r"加\s*(群|君\S?羊|羣|Q+群?)\s*\S*\d{5,}"), 1.8),
    (re.compile(r"加\s*(我|你)\s*(微信|QQ|qq|vx|VX|wx|WX)\s*\S+"), 2.0),
    (re.compile(r"[vV][xX]\s*[：:]\s*\S+"), 1.5),
    (re.compile(r"[qQ]{2,}\s*[：:]\s*\d{5,}"), 1.6),
    # URL 匹配但排除已知安全域名
    (re.compile(r"(http[s]?://|www\.)(?!([\w.-]+\.)?(6geapi|github|gitlab|gitee|qq|tencent|bilibili|zhihu|baidu|google|bing|python|npmjs|pypi)\.\S+)\S+"), 1.0),
    (re.compile(r"免费\S{0,3}(领取|送|拿|获取)"), 1.5),
    (re.compile(r"日\s*(入|赚|收)\s*\d{2,}"), 1.6),
    # 🔑 裸域名检测（无 http/www 前缀的域名，昵称广告最常见的形式）
    # 匹配模式: xxx.xxx 形式，同时覆盖中文句号/全角点/空格等绕过手段
    (re.compile(r"[a-zA-Z0-9][-a-zA-Z0-9]*[.。．·]\s*(com|cn|net|xyz|top|cc|cfd|vip|icu|site|ink|pw|fun|online|shop|store|cloud|dev|app|link|io|org|co|me|info|biz)\b"), 1.6),
    # "xxx点xxx" 形式（全中文绕过，如 "sheepapi点xyz"）
    (re.compile(r"[a-zA-Z0-9][-a-zA-Z0-9]*\s*点\s*(com|cn|net|xyz|top|cc|cfd|vip|icu|site|ink|pw|fun|online|shop|store|cloud|dev|app|link|io|org|co|me|info|biz)\b", re.IGNORECASE), 1.6),
]


# ─── 评分引擎 ───

def _get_keyword_weights() -> dict[str, float]:
    """从违规记录中构建加权关键词词典（带 TTL 缓存，减少 DB 查询）"""
    cached = cache.keyword_weights_cache.get("keyword_weights")
    if cached is not None:
        return cached

    weights = dict(SENSITIVE_WORDS)
    records = db.query_violations(limit=500)
    freq: dict[str, int] = Counter()
    for r in records:
        for kw in r.get("keywords", []):
            freq[kw] += 1
    # 频率加权：log(1+freq) 防止高频词主导
    for kw, cnt in freq.items():
        w = math.log(1 + cnt) * 1.2
        weights[kw] = max(weights.get(kw, 0), w)

    cache.keyword_weights_cache.set("keyword_weights", weights)
    return weights


def score_message(raw_message: str) -> dict:
    """
    对一条消息打分
    返回: {confidence, keywords_matched, is_violation, threshold}
    """
    # 先清洗 CQ 码，避免图片/表情元数据干扰
    clean = raw_message
    while True:
        new_text = re.sub(r"\[CQ:[^\]]*\]", "", clean)
        if new_text == clean:
            break
        clean = new_text
    if not clean.strip():
        return {"confidence": 0, "keywords_matched": [], "is_violation": False,
                "threshold": 1, "total_score": 0, "violation_count": 0}

    weights = _get_keyword_weights()
    text_lower = clean.lower()
    matched: dict[str, float] = {}
    total_score = 0.0

    # 关键词匹配
    for kw, w in weights.items():
        if kw in text_lower:
            matched[kw] = w
            total_score += w

    # 正则匹配
    for pat, w in AD_PATTERNS:
        if pat.search(clean):
            key = pat.pattern[:30]
            matched[f"re:{key}"] = w
            total_score += w

    # 裸域名安全过滤：如果消息里的域名全在安全白名单中，不计分
    _SAFE_BARE = {  # {域名主体: {允许的TLD}}
        "6geapi": {"com"}, "github": {"com", "io"}, "gitlab": {"com"},
        "gitee": {"com"}, "qq": {"com"}, "tencent": {"com"},
        "bilibili": {"com"}, "zhihu": {"com"}, "baidu": {"com"},
        "google": {"com"}, "bing": {"com"}, "python": {"org"},
        "npmjs": {"com"}, "pypi": {"org"}, "stackoverflow": {"com"},
    }
    bare_raw = re.findall(r"[a-zA-Z0-9][-a-zA-Z0-9]*[.。．·]\s*([a-zA-Z]{2,})\b", clean)
    bare_cn = re.findall(r"[a-zA-Z0-9][-a-zA-Z0-9]*\s*点\s*([a-zA-Z]{2,})\b", clean)
    bare_names = re.findall(r"([a-zA-Z0-9][-a-zA-Z0-9]*)[.。．·]", clean)
    bare_names += re.findall(r"([a-zA-Z0-9][-a-zA-Z0-9]*)\s*点", clean)
    all_safe = False
    if bare_names:
        tlds = bare_raw + bare_cn
        all_safe = all(
            name.lower() in _SAFE_BARE and (not tlds or any(t in _SAFE_BARE[name.lower()] for t in tlds))
            for name in bare_names
        )
    if all_safe and bare_names:
        # 所有匹配到的裸域名都是安全域名，扣回分数
        for k in list(matched.keys()):
            if k.startswith("re:") and ("com|cn|net" in k or "点" in k):
                total_score -= matched[k]
                del matched[k]

    # 技术上下文检测: 技术讨论自动降权
    tech_signals = 0
    _tech_res = [
        r"(?i)\b(api|sdk|cli|http|json|rest|graphql|oauth|jwt|webhook)\b",
        r"(?i)\b(git|github|gitlab|gitee|docker|k8s|kubernetes|nginx|linux)\b",
        r"(?i)\b(python|javascript|typescript|golang|rust|java|react|vue|node)\b",
        r"(?i)\b(openai|anthropic|claude|gpt|deepseek|gemini|copilot|cursor)\b",
        r"(?i)\b(localhost|127\.0\.0\.1|:\d{4,5})\b",
        r"(?i)\b(config|env|token|secret|credential|endpoint|proxy)\b",
        r"(?i)\b(npm|pip|cargo|brew|apt|yum|docker-compose|kubectl)\b",
        r"(?i)\b(报错|错误|error|exception|timeout|500|404|debug|traceback)\b",
        r"(?i)\b(教程|文档|doc|guide|tutorial|示例|example|demo|怎么|如何|请教)\b",
        r"(?i)\b(代码|源码|code|repo|repository|开源|open.?source)\b",
    ]
    for pat in _tech_res:
        if re.search(pat, clean):
            tech_signals += 1
    if tech_signals >= 3:
        total_score *= 0.3
    elif tech_signals >= 2:
        total_score *= 0.5

    # 归一化：短消息(<10字)微罚，正常消息正常评分
    msg_len = max(len(clean), 1)
    if msg_len < 10:
        norm_factor = 0.7
    else:
        norm_factor = min(1.0, 3.0 / math.sqrt(msg_len / 10))
    confidence = min(total_score * norm_factor / 4.5, 1.0)

    # 阈值：基础 0.65，只随反馈微调
    sample_count = db.get_violation_count()
    threshold = max(0.55, 0.70 - sample_count * 0.0005)

    # 硬性要求：≥3个关键词，或≥1个正则模式，或≥1个高危词(权重>=2.0)
    # 昵称/短文本放宽要求，避免漏检
    keyword_count = len([k for k in matched if not k.startswith("re:")])
    pattern_count = len([k for k in matched if k.startswith("re:")])
    base_high_hits = [k for k in matched if not k.startswith("re:") and matched.get(k, 0) >= 2.0]
    if keyword_count < 3 and pattern_count < 1 and not base_high_hits:
        confidence = 0

    return {
        "confidence": round(confidence, 4),
        "keywords_matched": list(matched.keys()),
        "is_violation": confidence >= threshold,
        "threshold": round(threshold, 4),
        "total_score": round(total_score, 4),
        "violation_count": sample_count,
    }


def should_auto_recall(raw_message: str) -> tuple[bool, dict]:
    """判断是否应该自动撤回，返回 (是否撤回, 评分详情)"""
    # 学习引擎评分
    result = learner.score_with_learning(raw_message)
    # 旧引擎评分（作为补充）
    old_result = score_message(raw_message)

    # 合并关键词
    learner_hits = [h["keyword"] for h in result.get("details", {}).get("keyword_hits", [])]
    pattern_hits = [p.get("description", "") for p in result.get("details", {}).get("pattern_hits", [])]
    result["keywords_matched"] = list(set(learner_hits + pattern_hits + old_result.get("keywords_matched", [])))

    # 综合置信度：取两个引擎的最高值
    combined_confidence = max(result["confidence"], old_result["confidence"])
    result["confidence"] = round(combined_confidence, 4)
    result["is_violation"] = combined_confidence >= min(result["threshold"], old_result["threshold"])

    # 自动撤回：≥0.95 且命中≥4个关键词才撤（避免误判）
    if combined_confidence >= 0.95 and (
        len(result.get("details", {}).get("keyword_hits", [])) >= 4
        or len(result.get("details", {}).get("pattern_hits", [])) >= 2
    ):
        return True, result
    return False, result


# ─── ML 分类器 ───

_ml_model = None
_ml_vectorizer = None


def _build_ml_model():
    """构建 TF-IDF + 朴素贝叶斯分类器"""
    global _ml_model, _ml_vectorizer
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.naive_bayes import MultinomialNB
    except ImportError:
        logger.warning("scikit-learn 未安装，ML 分类器不可用")
        return

    violations = db.query_violations(limit=1000)
    normals = db.sample_get_all()

    if len(violations) < 20:
        logger.info(f"训练样本不足 ({len(violations)} 条违规)，跳过 ML 模型")
        return

    texts = []
    labels = []

    def preprocess(t):
        return " ".join(cache.cached_jieba_lcut(t))

    for v in violations:
        texts.append(preprocess(v["raw_message"]))
        labels.append(1)  # violation

    for s in normals[:len(violations) * 2]:
        texts.append(preprocess(s["raw_message"]))
        labels.append(0)  # normal

    _ml_vectorizer = TfidfVectorizer(max_features=2000, ngram_range=(1, 2))
    X = _ml_vectorizer.fit_transform(texts)
    _ml_model = MultinomialNB(alpha=0.5)
    _ml_model.fit(X, labels)

    # 保存模型
    with open(MODEL_PATH, "wb") as f:
        pickle.dump((_ml_vectorizer, _ml_model), f)

    logger.info(f"ML 模型训练完成: {len(violations)} 正样本, {len(normals)} 负样本")


def ml_predict(raw_message: str) -> Optional[dict]:
    """使用 ML 模型预测"""
    global _ml_model, _ml_vectorizer
    if _ml_model is None:
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH, "rb") as f:
                    _ml_vectorizer, _ml_model = pickle.load(f)
            except Exception:
                return None
        else:
            return None
    if _ml_model is None:
        return None

    text = " ".join(cache.cached_jieba_lcut(raw_message))
    vec = _ml_vectorizer.transform([text])
    proba = _ml_model.predict_proba(vec)[0]
    pred = _ml_model.predict(vec)[0]
    return {
        "is_violation": bool(pred),
        "confidence": round(float(max(proba)), 4),
        "violation_prob": round(float(proba[1]), 4),
    }
