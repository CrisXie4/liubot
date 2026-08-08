"""
渐进学习引擎 — 从违规消息中持续学习

学习维度:
  1. 关键词权重 — 从违规记录中统计高频词，动态调整权重
  2. 正则模式 — 自动提取广告/违规的消息模式
  3. 置信度校准 — 根据准确率反馈调整阈值
  4. 上下文特征 — URL、QQ号、重复字符等辅助信号
"""
import json
import logging
import math
import os
import re
from collections import Counter
from datetime import datetime

from . import db, cache

logger = logging.getLogger("qqbot")

KNOWLEDGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "learned_knowledge.json"
)


# ==================== 知识库持久化 ====================

def _load_knowledge() -> dict:
    """读取学习知识库（带缓存，减少文件 IO）"""
    cached = cache.learned_knowledge_cache.get("knowledge")
    if cached is not None:
        return cached

    if os.path.exists(KNOWLEDGE_PATH):
        try:
            with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
                knowledge = json.load(f)
            cache.learned_knowledge_cache.set("knowledge", knowledge)
            return knowledge
        except Exception:
            pass
    knowledge = {
        "learned_keywords": {},       # {词: 权重}
        "learned_patterns": [],       # [{"pattern": "正则", "weight": 1.5, "source": "..."}]
        "false_positives": [],        # 误判记录
        "version": 0,                 # 知识库版本
        "updated_at": "",
    }
    cache.learned_knowledge_cache.set("knowledge", knowledge)
    return knowledge


def _save_knowledge(knowledge: dict):
    os.makedirs(os.path.dirname(KNOWLEDGE_PATH), exist_ok=True)
    knowledge["version"] += 1
    knowledge["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(KNOWLEDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)
    # 更新缓存（写入后立即可用）而非使缓存失效
    cache.learned_knowledge_cache.set("knowledge", knowledge)


# ==================== 上下文特征提取 ====================

# 裸域名后缀（支持 . / 。/ ．/ · / 点 等多种分隔符，防止绕过）
_BARE_DOMAIN_RE = re.compile(
    r"[a-zA-Z0-9][-a-zA-Z0-9]*[.。．·]\s*(com|cn|net|xyz|top|cc|cfd|vip|icu|site|ink|pw|fun|online|shop|store|cloud|dev|app|link|io|org|co|me|info|biz)\b",
    re.IGNORECASE,
)
_BARE_DOMAIN_CN_RE = re.compile(
    r"[a-zA-Z0-9][-a-zA-Z0-9]*\s*点\s*(com|cn|net|xyz|top|cc|cfd|vip|icu|site|ink|pw|fun|online|shop|store|cloud|dev|app|link|io|org|co|me|info|biz)\b",
    re.IGNORECASE,
)


def extract_context_features(raw_message: str) -> dict:
    """提取消息的上下文辅助特征"""
    # 裸域名计数（. / 。/ 点 等各种分隔符写法）
    # 过滤掉已知安全域名
    _safe_domains = {
        "github.com", "gitlab.com", "gitee.com", "stackoverflow.com",
        "npmjs.com", "pypi.org", "python.org", "rust-lang.org",
        "qq.com", "tencent.com", "bilibili.com", "zhihu.com",
        "baidu.com", "google.com", "bing.com", "6geapi.com",
    }
    def _is_safe_bare_domain(match_text: str) -> bool:
        """检查裸域名匹配是否属于安全域名"""
        # 标准化分隔符
        normalized = re.sub(r"[.。．·]\s*", ".", match_text.lower())
        normalized = re.sub(r"\s*点\s*", ".", normalized)
        return any(normalized.endswith("." + d) or normalized == d for d in _safe_domains)

    bare_matches = _BARE_DOMAIN_RE.findall(raw_message) + _BARE_DOMAIN_CN_RE.findall(raw_message)
    bare_domains = sum(0 if _is_safe_bare_domain(m) else 1 for m in bare_matches)
    return {
        "length": len(raw_message),
        "url_count": len(re.findall(r"https?://|www\.", raw_message)) + bare_domains,
        "bare_domain_count": bare_domains,
        "qq_count": len(re.findall(r"[1-9]\d{4,10}", raw_message)),
        "special_ratio": len(re.findall(r"[^\w\s一-鿿]", raw_message)) / max(len(raw_message), 1),
        "digit_ratio": len(re.findall(r"\d", raw_message)) / max(len(raw_message), 1),
        "has_invite": bool(re.search(r"(加|进|来).{0,3}(群|Q+群|君羊)", raw_message)),
        "has_money": bool(re.search(r"(日|月|年).{0,3}(赚|入|收)\s*\d+|[¥￥]\s*\d+", raw_message)),
        "has_free": bool(re.search(r"免费|白嫖|不要钱|0元", raw_message)),
        "has_urgency": bool(re.search(r"快|赶紧|马上|限时|最后|错过|机会", raw_message)),
        "has_ad_combos": bool(re.search(r"(低价|便宜|实惠|特价|折扣).{0,5}(找|联系|加|咨询|私|购买|下单|中转|机场|节点|流量|专线)", raw_message)),
        "repeat_char_ratio": _repeat_ratio(raw_message),
    }


def _repeat_ratio(text: str) -> float:
    """检测重复字符比例（垃圾消息特征）"""
    if len(text) < 4:
        return 0
    repeats = 0
    for i in range(len(text) - 2):
        if text[i] == text[i + 1] == text[i + 2]:
            repeats += 1
    return repeats / len(text)


# ==================== 模式提取 ====================

def extract_patterns(raw_message: str) -> list[dict]:
    """从单条违规消息中提取通用模式"""
    patterns = []

    # QQ群号模式（需要上下文：加群/QQ等关键词在旁边）
    qq_groups = re.findall(r"[1-9]\d{4,11}", raw_message)
    if qq_groups and re.search(r"(加|来|进|群|Q+|qq|引流|私聊|联系)", raw_message, re.IGNORECASE):
        patterns.append({
            "pattern": r"[1-9]\d{4,11}",
            "type": "qq_number",
            "weight": 0.6,
            "description": "疑似QQ号",
        })

    # URL 模式
    if re.search(r"https?://|www\.", raw_message):
        patterns.append({
            "pattern": r"https?://\S+|www\.\S+",
            "type": "url",
            "weight": 0.8,
            "description": "包含链接",
        })

    # 加群话术
    invite_matches = re.findall(r"(加.{0,4}(群|君羊|Q+群|討論組))", raw_message)
    if invite_matches:
        patterns.append({
            "pattern": r"加\s*(群|君羊|Q+?群|討論組)\s*\d*",
            "type": "invite",
            "weight": 1.5,
            "description": "加群话术",
        })

    # 微信引流
    if re.search(r"[vV]\s*[xX]\s*[:：]?\s*\w+", raw_message):
        patterns.append({
            "pattern": r"[vV]\s*[xX]\s*[:：]?\s*\w+",
            "type": "wechat",
            "weight": 1.3,
            "description": "微信号引流",
        })

    # 红包/金钱诱惑
    if re.search(r"(红包|现金|奖励|福利|返利|佣金|提现|到账)", raw_message):
        patterns.append({
            "pattern": r"(红包|现金|奖励|福利|返利|佣金|提现|到账)",
            "type": "money_lure",
            "weight": 1.2,
            "description": "金钱诱惑",
        })

    # 关键词密度异常（用jieba分词后看高频词）
    try:
        words = [w for w in cache.cached_jieba_lcut(raw_message) if len(w) >= 2]
        word_freq = Counter(words)
        for word, cnt in word_freq.most_common(5):
            if cnt >= 3 and len(word) >= 2:
                patterns.append({
                    "pattern": re.escape(word),
                    "type": "repeat_word",
                    "weight": min(cnt * 0.4, 2.0),
                    "description": f"重复词: {word}",
                })
    except Exception:
        pass

    return patterns


# ==================== 过滤：不应学为违禁词的词汇 ====================

# 永远不应被标记为违规的词汇（技术术语、模型名、常见英文）
_NEVER_VIOLATION = {
    # AI/LLM 模型名
    "gpt", "GPT", "claude", "Claude", "codex", "Codex", "gemini", "Gemini",
    "openai", "OpenAI", "OpenAl", "deepseek", "DeepSeek", "chatgpt", "ChatGPT",
    "anthropic", "Anthropic", "copilot", "Copilot", "cursor", "Cursor",
    # 版本/等级名
    "pro", "Pro", "plus", "Plus", "flash", "Flash", "turbo", "Turbo",
    "mini", "Mini", "max", "Max", "lite", "Lite", "ultra", "Ultra",
    # 常见技术词汇
    "api", "API", "sdk", "SDK", "url", "URL", "http", "HTTP", "https", "HTTPS",
    "json", "JSON", "ssh", "SSH", "gpu", "GPU", "cpu", "CPU", "ram", "RAM",
    "token", "tokens", "model", "models", "agent", "agents",
    "docker", "Docker", "nginx", "Nginx", "redis", "Redis", "mysql", "MySQL",
    "python", "Python", "node", "Node", "java", "Java", "rust", "Rust", "go", "Go",
    "npm", "pip", "git", "Git", "github", "Github", "GitHub",
    "vscode", "VSCode", "linux", "Linux", "windows", "Windows", "mac", "Mac",
    "android", "Android", "ios", "iOS",
    # 常见服务/产品名
    "vercel", "Vercel", "netlify", "Netlify", "cloudflare", "Cloudflare",
    "aws", "AWS", "azure", "Azure", "gcp", "GCP",
    "notion", "Notion", "slack", "Slack", "discord", "Discord",
    "telegram", "Telegram", "whatsapp", "WhatsApp",
    # 游戏/平台
    "steam", "Steam", "epic", "Epic", "bilibili", "Bilibili", "youtube", "Youtube",
    "tiktok", "Tiktok", "抖音", "快手",
    # 常见但不违规的短词
    "ai", "AI", "llm", "LLM", "ml", "ML", "ui", "UI", "ux", "UX",
    "web", "Web", "app", "App", "pc", "PC",
    "im", "IM", "qq", "QQ", "wechat", "WeChat", "微信",
    "op", "OP", "os", "OS", "db", "DB", "io", "IO",
    "ok", "OK", "okay", "no", "yes", "id", "ID", "ip", "IP",
    "vpn", "VPN", "dns", "DNS", "cdn", "CDN",
    "jpg", "png", "gif", "mp4", "pdf", "css", "html", "js", "ts",
    "svg", "csv", "xml", "yaml", "toml", "md",
    # 数字/单位/符号
    "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8",
    "o1", "o3", "o4", "f5",
    "2b", "3b", "4k", "8k", "hd",
}


def _is_junk_keyword(kw: str) -> bool:
    """检查关键词是否不应被学习（纯数字、过短英文、技术术语等）"""
    if not kw or len(kw) < 2:
        return True
    # 纯数字/小数/带单位数字
    if kw.replace(".", "").replace("-", "").replace("_", "").replace(",", "").strip("RrKkMmBb").isdigit():
        return True
    # 纯ASCII 且 ≤2 字符
    if kw.isascii() and len(kw) <= 2:
        return True
    # 纯ASCII 且长度 3-4 且全小写/全大写（很可能是随机缩写）
    if kw.isascii() and len(kw) <= 4 and kw.lower() == kw and not any(c.isdigit() for c in kw):
        # 但是要允许已知的违规词如 "sb" "cnm" 等
        pass  # let the NEVER_VIOLATION check below handle it
    # 已知安全词
    if kw in _NEVER_VIOLATION:
        return True
    # 纯中文高频日常词（不应成为违禁词）
    if all('一' <= c <= '鿿' for c in kw):
        if kw in _SAFE_WORDS:
            return True
        # 单字中文
        if len(kw) == 1:
            return True
        # 极常见的中文日常用词前缀
        common_prefixes = ('不知', '为什', '怎么', '什么', '在哪', '可以', '有没有',
                          '是不是', '能不能', '会不会', '应不应该', '现在', '今天',
                          '明天', '昨天', '之前', '以后', '一会', '等一下', '马上',
                          '因为', '所以', '但是', '不过', '虽然', '如果', '假如',
                          '一个', '这个', '那个', '哪个', '有些', '一些', '所有',
                          '还是', '已经', '还没', '刚才', '刚刚', '可能', '应该',
                          '大概', '也许', '觉得', '认为', '知道', '不知道',
                          '好的', '没事', '没关系', '无所谓', '没问题',
                          '真的', '假的', '确实', '肯定', '一定')
        if kw.startswith(common_prefixes):
            return True
    return False


# ==================== 学习：从违规中更新知识 ====================

def learn_from_violation(raw_message: str, keywords: list[str], source: str = "manual"):
    """
    从一条违规记录中学习并更新知识库

    - manual: 手动标记，权重加成 1.5x
    - auto: 自动识别，权重加成 1.0x
    """
    knowledge = _load_knowledge()

    # 1. 更新关键词权重（过滤垃圾词、技术术语、数字、短英文）
    boost = 1.5 if source == "manual" else 0.5
    garbage = {"http", "https", "CQ", "image", "file", "type", "sub", "url", "png",
               "jpg", "jpeg", "gif", "mp4", "download", "appid", "fileid", "rkey",
               "file_size", "multimedia", "nt.qq.com"}
    skipped = 0
    for kw in keywords:
        # 跳过 URL 和图片元数据
        if kw.startswith("http") or kw in garbage:
            skipped += 1
            continue
        # 跳过垃圾词
        if _is_junk_keyword(kw):
            skipped += 1
            continue
        if kw in _SAFE_WORDS:
            skipped += 1
            continue
        old = knowledge["learned_keywords"].get(kw, 0)
        # 降低学习速度和上限
        knowledge["learned_keywords"][kw] = min(old + boost, 8.0)
    if skipped:
        logger.debug(f"过滤了 {skipped} 个无效关键词")

    # 2. 提取并存储通用模式
    patterns = extract_patterns(raw_message)
    for p in patterns:
        # 检查是否已存在相似模式
        exists = False
        for existing in knowledge["learned_patterns"]:
            if existing["pattern"] == p["pattern"]:
                existing["weight"] = min(existing["weight"] + boost * 0.5, 5.0)
                exists = True
                break
        if not exists:
            p["weight"] *= boost
            knowledge["learned_patterns"].append(p)

    # 3. 限制模式数量（保留权重最高的 200 条）
    knowledge["learned_patterns"] = sorted(
        knowledge["learned_patterns"], key=lambda x: x["weight"], reverse=True
    )[:200]

    # 4. 限制关键词数量（保留权重最高的 500 个）
    knowledge["learned_keywords"] = dict(
        sorted(knowledge["learned_keywords"].items(), key=lambda x: x[1], reverse=True)[:500]
    )

    _save_knowledge(knowledge)

    logger.info(
        f"学习完成: 关键词 {len(knowledge['learned_keywords'])} 个, "
        f"模式 {len(knowledge['learned_patterns'])} 条, "
        f"来源: {source}"
    )

    return knowledge


def record_false_positive(raw_message: str):
    """记录误判（自动撤回但实际正常的消息），用于校准"""
    knowledge = _load_knowledge()
    knowledge["false_positives"].append({
        "message": raw_message[:200],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    # 只保留最近 100 条
    knowledge["false_positives"] = knowledge["false_positives"][-100:]
    _save_knowledge(knowledge)


# ==================== 综合评分引擎 ====================

# 安全词（绝不会是广告的日常词）
_SAFE_WORDS = {
    "你好", "谢谢", "再见", "哈哈", "好的", "收到", "明白", "了解",
    "早安", "晚安", "拜拜", "嗨", "嗯嗯", "哦哦", "来了", "走了",
    "取消", "拉黑", "解禁", "禁言", "帮助", "统计", "查询", "违规",
    "群友", "管理", "模式", "自动",
    "活着", "不行", "笑死", "原来", "个人", "辅助", "帮忙", "请问",
    "有人", "有没有", "能不能", "怎么", "什么", "哪里", "为什么",
    "可以", "不可以", "还行", "不错", "厉害", "牛逼", "高手",
    "今天", "明天", "昨天", "现在", "一会", "等等", "稍等",
    "知道", "不知道", "没事", "没关系", "无所谓",
    "真的", "假的", "确实", "可能", "应该", "大概", "也许",
    "已经", "还没", "马上", "刚刚", "刚才", "之前", "以后",
    "一个", "这个", "那个", "怎么", "这么", "那么",
    "CQ", "at", "qq", "image", "file", "summary", "type", "sub", "url",
    "http", "https", "com", "www", "png", "jpg", "jpeg", "gif", "mp4",
    "动画", "表情", "下载", "fileid", "rkey", "appid", "multimedia",
    "Xie", "小Q",
    "群主", "群友", "大家", "各位", "伙伴",
    "聊天", "说话", "发言", "冒泡", "潜水",
    "上班", "下班", "吃饭", "睡觉", "打游戏", "学习",
    "开心", "难过", "无聊", "困了", "累了", "醉了",
    "嗯", "啊", "哦", "额", "呃", "唉", "哎",
    # v3 新增安全词
    "微信", "支付", "方式", "联系", "手机", "电话", "号码",
    "号码", "账号", "账户", "密码", "登录", "注册", "绑定",
    "支付宝", "转账", "红包", "收款", "付款", "退款",
    "公司", "老板", "同事", "朋友", "家人", "同学", "老师",
    "学校", "医院", "银行", "超市", "快递", "外卖", "打车",
    "视频", "语音", "直播", "录播", "截图", "录屏", "拍照",
    "手机号", "微信号", "邮箱", "地址", "姓名", "性别", "年龄",
    "身高", "体重", "生日", "星座", "血型", "生肖",
    "提醒", "通知", "公告", "置顶", "精华", "收藏", "转发",
    "加入", "退出", "设置", "修改", "更改", "更新", "升级",
    "下载", "安装", "卸载", "打开", "关闭", "重启", "刷新",
    "保存", "删除", "撤回", "编辑", "发布", "提交", "确认",
    "开始", "结束", "暂停", "继续", "完成", "成功", "失败",
    "输入", "输出", "导入", "导出", "上传", "加载", "处理",
    "已读", "未读", "在线", "离线", "活跃", "沉默",
    "关注", "点赞", "评论", "回复", "私信", "留言",
    "推荐", "热门", "最新", "最热", "排行", "榜单",
    "中文", "英文", "日语", "韩语", "法语", "德语",
    "功能", "支持", "兼容", "插件", "扩展", "皮肤", "字体",
    "版本", "更新日志", "更新内容", "新功能", "修复", "优化",
    "免费", "付费", "会员", "VIP", "积分", "等级", "经验",
    "任务", "签到", "打卡", "抽奖", "活动", "优惠", "折扣",
    "价格", "原价", "现价", "差价", "优惠券", "满减", "包邮",
    "喜欢", "讨厌", "不错", "不行", "完美", "垃圾",
    "简单", "困难", "复杂", "麻烦", "方便", "快捷",
    "好看", "难看", "好听", "难听", "好吃", "难吃",
    "好玩", "无聊", "有趣", "没意思", "有意思",
    "是真的", "不是真的", "真的吗", "确定", "确实", "肯定",
    "需要", "想要", "得到", "拿到", "找到", "看到", "听到",
    "来说", "来讲", "来看", "来听", "来做", "来玩", "来试",
    "一样", "不一样", "类似", "不同", "相同", "差别",
    "包括", "不包括", "包含", "不包含", "除了", "以外",
    "因为", "所以", "但是", "不过", "虽然", "然而", "而且",
    "如果", "假如", "假设", "万一", "即使", "哪怕",
    "所有", "全部", "部分", "有些", "少数", "多数", "大部分",
    "之前说过", "之前提过", "之前聊过", "之前问过",
    "表情包", "贴纸", "动图", "图片", "照片", "照骗",
    "链接", "访问", "点击", "打开链接", "查看", "浏览",
    "搬运", "转载", "分享", "原创", "抄袭", "借鉴",
    "申精", "申请", "审核", "审批", "通过", "驳回", "拒绝",
    "指南", "攻略", "教程", "说明", "介绍", "详解", "科普",
    "测评", "评测", "对比", "横评", "开箱", "体验",
    "小米", "华为", "苹果", "三星", "OPPO", "vivo", "一加",
    "电脑", "笔记本", "台式机", "平板", "iPad", "iPhone",
    "键盘", "鼠标", "显示器", "耳机", "音箱", "麦克风",
    "显卡", "CPU", "内存", "硬盘", "主板", "电源", "机箱",
    "游戏", "王者", "LOL", "原神", "崩铁", "吃鸡", "DOTA",
    "二次元", "动漫", "番剧", "声优", "角色", "剧情",
    "猫咪", "狗狗", "宠物", "动物", "植物", "昆虫",
    "天气", "温度", "下雨", "晴天", "阴天", "刮风", "下雪",
    "吃饭", "早餐", "午餐", "晚餐", "宵夜", "零食", "饮料",
    "运动", "健身", "跑步", "游泳", "骑行", "登山", "瑜伽",
    "旅行", "旅游", "景点", "酒店", "机票", "火车票", "攻略",
    # v3 补充：容易被误判的常见词
    "QQ", "qq", "Q群", "qq群", "群号", "群聊",
    "私聊", "私信", "加好友", "加我好友", "好友",
    "二维码", "扫码", "扫一扫",
    "我的", "你的", "他的", "她的", "我们的", "你们的",
    "这是", "那是", "就是", "不是", "还是",
    "微信", "QQ号", "手机号", "账号",
    "发我", "发你", "发他", "给我", "给你", "给他",
    "发一下", "发给我", "发给你", "发给他",
    "分享", "转发", "推荐", "介绍",
}


def _strip_cq(text: str) -> str:
    import re
    # 移除所有 [CQ:...] 格式的QQ代码（包括嵌套）
    while True:
        new_text = re.sub(r"\[CQ:[^\]]*\]", "", text)
        if new_text == text:
            break
        text = new_text
    return text


def score_with_learning(raw_message: str) -> dict:
    """
    综合评分: 学习到的关键词 + 模式 + 上下文特征
    返回详细评分
    """
    knowledge = _load_knowledge()
    # 清洗 CQ 代码
    clean = _strip_cq(raw_message)
    if not clean.strip():
        return {"confidence": 0, "threshold": 1, "is_violation": False, "raw_score": 0,
                "details": {"keyword_hits": [], "pattern_hits": [], "context_signals": []},
                "learned_keywords": len(knowledge.get("learned_keywords", {})),
                "learned_patterns": len(knowledge.get("learned_patterns", [])),
                "false_positives": len(knowledge.get("false_positives", [])),
                "violation_count": 0}

    features = extract_context_features(clean)
    text_lower = clean.lower()
    score = 0.0
    details = {
        "keyword_hits": [],
        "pattern_hits": [],
        "context_signals": [],
    }

    # --- 1. 学习到的关键词 + 基础词库匹配 ---
    learned_kw = dict(knowledge["learned_keywords"])
    base_hits = set()  # 来自基础词库的命中（权重更高）
    try:
        from .automod import SENSITIVE_WORDS as _BASE
        for kw, w in _BASE.items():
            if kw not in learned_kw and kw not in _SAFE_WORDS:
                learned_kw[kw] = w
                base_hits.add(kw)
    except ImportError:
        pass
    for kw, weight in learned_kw.items():
        if kw in _SAFE_WORDS:
            continue
        if kw in text_lower:
            # 基础词库的命中权重不打折
            mult = 1.0 if kw in base_hits else 0.7
            score += weight * mult
            details["keyword_hits"].append({"keyword": kw, "weight": round(weight, 1)})

    # --- 2. 学习到的正则模式匹配（在清洗后的文本上匹配）---
    for p in knowledge["learned_patterns"]:
        try:
            if re.search(p["pattern"], clean):
                score += p.get("weight", 1.0) * 1.2
                details["pattern_hits"].append(p)
        except re.error:
            continue

    # --- 3. 上下文特征加分（排除已知安全域名）---
    # 安全域名白名单 — 这些域名的链接不应被标记为违规
    _SAFE_DOMAINS = {
        "github.com", "gitlab.com", "gitee.com", "stackoverflow.com",
        "npmjs.com", "pypi.org", "python.org", "rust-lang.org",
        "qq.com", "tencent.com", "multimedia.nt.qq.com.cn",
        "bilibili.com", "zhihu.com", "douban.com", "juejin.cn",
        "baidu.com", "google.com", "bing.com",
        "6geapi.com",  # 用户自建 API 服务
    }
    url_count = features["url_count"]
    if url_count > 0:
        # 检查 URL 是否全部是安全域名
        import re as _re2
        urls = _re2.findall(r"https?://([\w.-]+)", clean)
        safe_urls = sum(1 for u in urls if any(u.endswith(d) or u == d for d in _SAFE_DOMAINS))
        suspicious_urls = url_count - safe_urls
        if suspicious_urls > 0:
            score += suspicious_urls * 0.5
            details["context_signals"].append(f"链接 x{suspicious_urls}")
        elif url_count > 0:
            # 全是安全域名，不加分
            pass

    if features["qq_count"] > 0:
        score += features["qq_count"] * 0.3
        details["context_signals"].append(f"QQ号 x{features['qq_count']}")

    if features["has_invite"]:
        score += 1.2
        details["context_signals"].append("加群话术")

    if features["has_money"]:
        score += 0.8
        details["context_signals"].append("金钱诱惑")

    if features["has_urgency"]:
        score += 0.4
        details["context_signals"].append("紧迫感话术")

    if features.get("bare_domain_count", 0) > 0:
        score += features["bare_domain_count"] * 1.0
        details["context_signals"].append(f"可疑域名 x{features['bare_domain_count']}")

    if features.get("has_ad_combos", False):
        score += 1.2
        details["context_signals"].append("广告话术组合(低价+联系/中转/节点等)")

    if features["repeat_char_ratio"] > 0.1:
        score += features["repeat_char_ratio"] * 5
        details["context_signals"].append("字符重复")

    if features["special_ratio"] > 0.3:
        score += features["special_ratio"] * 2
        details["context_signals"].append("特殊字符过多")

    # --- 3.5 技术上下文检测: 如果是技术讨论，大幅降权 ---
    tech_signals = 0
    tech_patterns = [
        r"(?i)\b(api|sdk|cli|http|json|rest|graphql|oauth|jwt|webhook)\b",
        r"(?i)\b(git|github|gitlab|gitee|docker|k8s|kubernetes|nginx|linux)\b",
        r"(?i)\b(python|javascript|typescript|golang|rust|java|react|vue|node)\b",
        r"(?i)\b(openai|anthropic|claude|gpt|deepseek|gemini|copilot|cursor)\b",
        r"(?i)\b(localhost|127\.0\.0\.1|0\.0\.0\.0|:\d{4,5})\b",
        r"(?i)\b(config|env|token|secret|credential|endpoint|proxy)\b",
        r"(?i)\b(npm|pip|cargo|brew|apt|yum|docker-compose|kubectl)\b",
        r"(?i)\b(报错|错误|error|exception|timeout|500|404|403|debug|traceback)\b",
        r"(?i)\b(教程|文档|doc|guide|tutorial|示例|example|demo|怎么|如何|请教)\b",
        r"(?i)\b(代码|源码|code|repo|repository|开源|open.?source)\b",
    ]
    for pat in tech_patterns:
        if re.search(pat, clean):
            tech_signals += 1
    # 有≥2个技术信号 → 大幅降权（技术讨论中的"api""节点""订阅"等是正常术语）
    tech_penalty = 1.0
    if tech_signals >= 3:
        tech_penalty = 0.3   # 强技术讨论，降到30%
        details["context_signals"].append(f"技术讨论(信号{tech_signals})")
    elif tech_signals >= 2:
        tech_penalty = 0.5   # 中等技术讨论，降到50%
        details["context_signals"].append(f"疑似技术讨论(信号{tech_signals})")
    score *= tech_penalty

    # --- 4. 归一化 ---
    msg_len = max(len(clean), 1)
    if msg_len < 5:
        length_penalty = 0.3
    else:
        length_penalty = min(1.0, 5.0 / math.sqrt(msg_len))
    confidence = min(score * length_penalty / 3.5, 1.0)

    # --- 5. 动态阈值 ---
    violation_count = db.get_violation_count()
    learned_count = len(learned_kw)
    fp_count = len(knowledge.get("false_positives", []))

    base_threshold = max(0.55, 0.68 - learned_count * 0.001)
    if len(details["keyword_hits"]) >= 3:
        base_threshold -= 0.05
    fp_penalty = min(fp_count * 0.005, 0.15)
    threshold = min(base_threshold + fp_penalty, 0.88)

    # 硬性要求
    hits = details["keyword_hits"]
    pats = details["pattern_hits"]
    # QQ号模式需要伴随广告上下文词才算真正可疑
    effective_pats = [
        p for p in pats
        if p.get("type") != "qq_number" or len(hits) >= 1 or features.get("has_invite", False)
    ]
    has_dangerous_hit = any(h["keyword"] in base_hits and h["weight"] >= 2.5 for h in hits)
    has_strong_context = (
        features.get("bare_domain_count", 0) > 0
        or features.get("has_ad_combos", False)
        or features.get("has_invite", False)       # 加群话术
        or features.get("has_free", False)          # 免费/白嫖
        or features.get("has_money", False)         # 金钱诱惑
    )
    if len(hits) < 3 and len(effective_pats) < 1 and not has_dangerous_hit and not has_strong_context:
        confidence = 0

    is_violation = confidence >= threshold

    return {
        "confidence": round(confidence, 4),
        "threshold": round(threshold, 4),
        "raw_score": round(score, 2),
        "is_violation": is_violation,
        "details": details,
        "learned_keywords": learned_count,
        "learned_patterns": len(knowledge.get("learned_patterns", [])),
        "false_positives": fp_count,
        "violation_count": violation_count,
    }


# ==================== 学习状态查询 ====================

def get_learning_status() -> dict:
    """获取学习系统状态"""
    knowledge = _load_knowledge()
    learned_kw = knowledge.get("learned_keywords", {})
    violation_count = db.get_violation_count()

    # 按权重排行的 top 关键词
    top_keywords = sorted(learned_kw.items(), key=lambda x: x[1], reverse=True)[:20]

    # 按权重排行的 top 模式
    top_patterns = sorted(
        knowledge.get("learned_patterns", []), key=lambda x: x["weight"], reverse=True
    )[:20]

    # 学习阶段
    if violation_count < 10:
        phase = "冷启动"
        phase_desc = "依赖基础词库，每手动标记一条都会学习"
    elif violation_count < 30:
        phase = "积累期"
        phase_desc = f"已学习 {len(learned_kw)} 个关键词，开始形成判断力"
    elif violation_count < 80:
        phase = "成长期"
        phase_desc = "关键词+模式+上下文综合判断"
    else:
        phase = "成熟期"
        phase_desc = f"高精度自动审核，{len(learned_kw)} 个关键词护航"

    return {
        "phase": phase,
        "phase_desc": phase_desc,
        "learned_keywords_count": len(learned_kw),
        "learned_patterns_count": len(knowledge.get("learned_patterns", [])),
        "false_positives_count": len(knowledge.get("false_positives", [])),
        "violation_count": violation_count,
        "knowledge_version": knowledge.get("version", 0),
        "updated_at": knowledge.get("updated_at", ""),
        "top_keywords": [{"keyword": k, "weight": round(w, 1)} for k, w in top_keywords],
        "top_patterns": [
            {"pattern": p["pattern"], "type": p["type"], "weight": round(p["weight"], 1)}
            for p in top_patterns
        ],
    }
