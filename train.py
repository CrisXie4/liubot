"""
训练脚本 — 将外部数据导入训练数据表（人工审核后再应用）

用法:
    python train.py <数据文件路径>               # 默认: 存入训练数据表（网页审核）
    python train.py <数据文件路径> --apply       # 直接应用到学习引擎（仅限确定高质量的数据）

支持格式:
    JSONL (.jsonl) — 每行一条 JSON
    CSV (.csv) — 表头需包含 label 列
    JSON (.json) — 数组格式

label 值:
    违规 / CB / unsafe / violation → 违规样本
    不违规 / Non-CB / safe / normal → 正常样本

安全提醒:
    外部数据集通常不适合直接训练。建议先用默认模式存入训练数据表，
    在网页 http://127.0.0.1:8090/#training 中人工审核，再点"应用到学习引擎"。
    只有你手动 @bot 违规 标记的群消息才是精准的 QQ 群场景数据。
"""

import json
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import db


def train_to_db(items: list[dict], file_name: str = ""):
    """存入训练数据表（人工审核后再应用）"""
    count = 0
    for d in items:
        text = ""
        for col in ("text", "prompt", "comment_content", "description"):
            if d.get(col):
                text += str(d[col]) + " "
        text = text.strip()
        if not text or len(text) < 4:
            continue

        label = "violation" if str(d.get("label", d.get("prompt_label", ""))) in (
            "违规", "CB", "unsafe", "violation", "1", "true"
        ) else "normal"

        category = ""
        if d.get("subject"):
            category = str(d["subject"])
        elif d.get("violated_categories"):
            category = str(d["violated_categories"]).split(",")[0].strip()

        keywords = str(d.get("keywords", ""))
        db.training_data_add(text, label, category, keywords, "")
        count += 1
    return count


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("示例:")
        print("  python train.py data/training.jsonl            # 存入训练数据表（推荐）")
        print("  python train.py data/training.jsonl --apply    # 直接应用")
        print("  python train.py model/ChineseSafe/test.jsonl   # 先存表再审核")
        return

    path = sys.argv[1]
    apply = "--apply" in sys.argv

    if not os.path.exists(path):
        print(f"文件不存在: {path}")
        return

    ext = os.path.splitext(path)[1].lower()

    # 加载数据
    if ext == ".jsonl":
        items = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    elif ext == ".csv":
        items = []
        for enc in ["utf-8", "gbk", "gb18030"]:
            try:
                with open(path, "r", encoding=enc) as f:
                    items = list(csv.DictReader(f))
                break
            except (UnicodeDecodeError, Exception):
                continue
    elif ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else []
    else:
        print(f"不支持: {ext}")
        return

    if apply:
        # 直接应用模式
        from bot import learner, cache, automod
        import jieba
        v, n = 0, 0
        for d in items:
            text = (str(d.get("text", "") or d.get("prompt", "")) + " " + str(d.get("response", "") or "")).strip()
            if len(text) < 4:
                continue
            label = str(d.get("label", d.get("prompt_label", "")))
            if label in ("违规", "CB", "unsafe", "violation"):
                from bot.learner import _is_junk_keyword
                words = [w for w in jieba.lcut(text) if len(w) >= 2 and not _is_junk_keyword(w)]
                if words:
                    learner.learn_from_violation(text, words, source="manual")
                    v += 1
            elif label in ("不违规", "Non-CB", "safe", "normal"):
                db.sample_add(0, 0, text, "normal")
                n += 1
        cache.learned_knowledge_cache.invalidate("knowledge")
        automod._build_ml_model()
        kb = learner._load_knowledge()
        print(f"应用完成: 违规{v} 正常{n}")
        print(f"关键词: {len(kb.get('learned_keywords', {}))} | 模式: {len(kb.get('learned_patterns', []))}")
    else:
        # 默认: 存入训练数据表
        count = train_to_db(items, path)
        print(f"已导入 {count} 条训练数据到数据库")
        print(f"请打开 http://127.0.0.1:8090/#training 审核后再应用")


if __name__ == "__main__":
    main()
