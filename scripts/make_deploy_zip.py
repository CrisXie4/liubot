"""打部署 ZIP：全仓库代码，排除运行时数据与本地环境。

排除清单（有新增记得同步这里）：
- .env          服务器上已有自己的 .env（含密钥），绝不能覆盖
- data/         运行时数据（记忆/上下文/数据库），不在部署包里
- .git/         仓库元数据（49MB+）
- NapCatQQ-main NapCat 完整源码快照（122MB，服务器上独立部署，不属于本 bot）
- .mimosa/.superpowers/.pytest_cache/__pycache__  本地工具/缓存状态
- bot.log *.zip *.pyc  运行日志与产物

用法: python scripts/make_deploy_zip.py
产出: 仓库根 qqbot-deploy-YYYYMMDD.zip
"""
import os
import zipfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, f"qqbot-deploy-{date.today():%Y%m%d}.zip")

EXCLUDE_DIRS = {
    ".git", "data", "__pycache__", ".pytest_cache",
    ".mimosa", ".superpowers",
    "NapCatQQ-main", "node_modules", ".venv", "venv",
}
EXCLUDE_FILES = {".env", "bot.log"}          # 按文件名精确匹配（含子目录）
EXCLUDE_EXTS = {".zip", ".pyc", ".log"}      # 按扩展名


def main():
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in sorted(filenames):
                if (fn in EXCLUDE_FILES
                        or os.path.splitext(fn)[1].lower() in EXCLUDE_EXTS):
                    continue
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, ROOT))
                count += 1
    print(f"{OUT}")
    print(f"{count} files, {os.path.getsize(OUT) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
