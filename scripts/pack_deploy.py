#!/usr/bin/env python3
"""
打包 qqbot 部署文件（位于 scripts/，在项目根目录运行: python scripts/pack_deploy.py）。

纯代码包：不包含 .env（密钥）、不包含 data/（运行时数据）、不包含 model/（训练数据集）。
服务器上的 .env 和 data/ 由服务器自己保留，解压覆盖不会动它们。

包含:
  - 所有代码 (bot/, web/, main.py, train.py)
  - requirements.txt / README.md / .env.example（模板，无密钥）

排除:
  - .git/ __pycache__/ .pytest_cache/ .superpowers/ archives/
  - .env（密钥）、data/（运行时数据+配置）、model/（训练数据集）
  - bot.log / *.zip / get-pip.py
  - mcp_server.py（本地调试用，不需要部署）
  - tests/ scripts/ docs/（部署不需要）

输出: archives/qqbot-deploy.zip
"""
import os
import sys
import zipfile

# 脚本在 scripts/ 下，项目根目录是上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVES = os.path.join(ROOT, "archives")
OUT = os.path.join(ARCHIVES, "qqbot-deploy.zip")

# ─── 顶层文件 ───
TOP_INCLUDE = {
    "main.py", "requirements.txt", "README.md", ".env.example", ".gitignore",
    "train.py",
}
TOP_EXCLUDE_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".superpowers", ".mimosa",
    "tests", "data", "web", "bot", "model",  # 这些单独处理
    "archives", "scripts", "docs", "NapCatQQ-main",
}
TOP_EXCLUDE_FILES = {
    "bot.log", "qqbot.zip", "qqbot-src.zip", "get-pip.py",
    "mcp_server.py", "pack_deploy.py", "qqbot-deploy.zip",
    ".htaccess",  # 服务器配置可能不同
}

# ─── 代码目录（全打包 .py/.html/.css/.js/.json） ───
CODE_DIRS = {"bot", "web"}
CODE_EXCLUDE_DIRS = {"__pycache__"}
CODE_EXCLUDE_EXTS = {".pyc", ".pyo"}


def should_include_code_file(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if ext in CODE_EXCLUDE_EXTS:
        return False
    if name == "__pycache__":
        return False
    return True


def add_dir_to_zip(zf: zipfile.ZipFile, dir_path: str, arc_prefix: str,
                   exclude_dirs: set[str], exclude_files: set[str] = frozenset(),
                   file_filter=None):
    """递归把目录加进 zip。"""
    for root, dirs, files in os.walk(dir_path):
        # 原地修改 dirs 跳过排除项
        dirs[:] = [d for d in dirs
                   if d not in exclude_dirs and not d.startswith(".")]
        for f in files:
            if f in exclude_files:
                continue
            if file_filter and not file_filter(f):
                continue
            abs_path = os.path.join(root, f)
            rel = os.path.relpath(abs_path, dir_path)
            arc = os.path.join(arc_prefix, rel).replace(os.sep, "/")
            zf.write(abs_path, arc)


def main():
    os.makedirs(ARCHIVES, exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)

    file_count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. 顶层文件
        for name in sorted(os.listdir(ROOT)):
            full = os.path.join(ROOT, name)
            if not os.path.isfile(full):
                continue
            if name in TOP_INCLUDE and name not in TOP_EXCLUDE_FILES:
                zf.write(full, name)
                file_count += 1
                print(f"  + {name}")

        # 2. 代码目录
        for d in CODE_DIRS:
            dir_path = os.path.join(ROOT, d)
            if not os.path.isdir(dir_path):
                continue
            before = file_count
            add_dir_to_zip(
                zf, dir_path, d,
                exclude_dirs=CODE_EXCLUDE_DIRS,
                file_filter=should_include_code_file,
            )
            # 数下加了几个文件
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = [x for x in dirs
                           if x not in CODE_EXCLUDE_DIRS and not x.startswith(".")]
                for f in files:
                    if should_include_code_file(f):
                        file_count += 1
            print(f"  + {d}/ ({file_count - before} 文件)")

        # 3. data/ 只放占位文件（配置和运行时数据由服务器自己保留，不打包）
        zf.writestr("data/.gitkeep", "")
        file_count += 1
        print(f"  + data/.gitkeep (占位)")

        # 4. README 部署说明
        deploy_readme = """# qqbot 部署说明（纯代码包）

本包只有代码，不含 .env 和 data/——服务器上的配置与运行时数据解压时不会被覆盖。

## 升级现有部署

1. 进入服务器 qqbot 目录
2. 解压覆盖代码:

       unzip -o qqbot-deploy.zip

3. 安装依赖（本次新增 psycopg）:

       pip install -r requirements.txt

4. 检查服务器 .env（参照 .env.example），本次需要新增:

       RELAY_DB_URL=postgresql://...   # 中转站数据库，AI 只读查询用；不配则相关工具自动隐藏

5. 重启 Bot

## 包含内容

  - bot/         Bot 主程序（含中转站数据库访问: bot/relay_db.py）
  - web/         Web 后台 + 加群验证页
  - main.py      入口
  - train.py     训练数据导入脚本
  - requirements.txt / .env.example / README.md

## 不在包内（服务器自己保留）

  - .env                     密钥与连接串（含 RELAY_DB_URL）
  - data/                    bot.db、模型设置、人设、意识状态等全部配置与运行时数据
  - model/                   训练数据集
"""
        zf.writestr("DEPLOY.md", deploy_readme)
        file_count += 1
        print(f"  + DEPLOY.md")

    size_mb = os.path.getsize(OUT) / 1024 / 1024
    print()
    print(f"✅ 打包完成: {OUT}")
    print(f"   文件数: {file_count}")
    print(f"   大小:   {size_mb:.2f} MB")
    return OUT


if __name__ == "__main__":
    main()
