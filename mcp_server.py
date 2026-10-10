#!/usr/bin/env python3
"""
qqbot MCP Server — 通过 Web API 连接到部署的 qqbot 实例。

让 AI 助手能读服务器日志、查运行时状态、诊断"忘事/看不到消息"等问题。

环境变量:
  QQBOT_URL       - Web API 地址 (默认 https://qqbot.crisxie.top)
  QQBOT_PASSWORD  - Web 登录密码 (必填)

启动:
  python mcp_server.py

工具:
  get_logs          读运行日志
  tail_logs         按关键字/级别过滤日志（定位问题用）
  get_bot_status    查 Bot 运行状态
  get_dashboard     总览统计
  get_groups        群列表
  get_memory_stats  长期记忆统计
  get_verify_history 加群验证历史
  search_violations 违规记录
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from typing import Optional

import requests

try:
    from mcp.server import MCPServer
except ImportError:
    sys.stderr.write(
        "[qqbot-mcp] 缺少依赖 'mcp'。请运行: pip install mcp requests\n"
    )
    sys.exit(1)

try:
    import paramiko
except ImportError:
    paramiko = None  # 部署功能可选；缺了只禁用 deploy_and_test

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None  # 部署加密需要；缺了禁用加密部署

# ─── 配置 ───
BASE_URL = os.getenv("QQBOT_URL", "https://qqbot.crisxie.top").rstrip("/")
PASSWORD = os.getenv("QQBOT_PASSWORD", "")
TIMEOUT = 20

# 部署目标（SSH）
SSH_HOST = os.getenv("QQBOT_SSH_HOST", "")
SSH_PORT = int(os.getenv("QQBOT_SSH_PORT", "22"))
SSH_USER = os.getenv("QQBOT_SSH_USER", "root")
SSH_PASSWORD = os.getenv("QQBOT_SSH_PASSWORD", "")
REMOTE_PROJECT_DIR = "/www/wwwroot/qqbot"
# 宝塔 Python 项目虚拟环境（必须 source activate 后才能 import uvicorn 等依赖）
# 注意: 宝塔的 venv 目录名有拼写错误 "pyporject_evn"（非笔误，服务器上就是这样）
# 旧路径 /www/server/python_project/vhost/env/qqbot.env 是 0 字节空文件，source 它等于什么都没做
REMOTE_VENV = "/www/server/pyporject_evn/qqbot"
REMOTE_ENV_SOURCE = f"{REMOTE_VENV}/bin/activate"
# 用 venv 的 python 直接启动，即使 source 失败也能找到 uvicorn
STARTUP_CMD = f"{REMOTE_VENV}/bin/python3 main.py"
# pgrep 匹配模式：宝塔面板启动的进程命令行是 "python3 main.py"，需兼容两种
PROCESS_PATTERN = "python3 main.py"

# ─── 宝塔面板（Bot 进程的唯一管理者）───
# 部署重启走面板 API，避免"面板自启 + 部署脚本手动启"双实例互相冲突。
# 认证（面板源码 class/common.py 实测）: request_token = md5(request_time + api.json.token)
# 调用方式：经 SSH 在服务器本地 curl https://127.0.0.1:<port>，
# 密钥只在服务器上读、不出服务器；面板端口也不对公网新增暴露。
# 前提：面板 API 已开启且 limit_addr 含 127.0.0.1（已在服务器 api.json 配好）。
BT_PROJECT_NAME = "qqbot"
BT_PANEL_PORT_FILE = "/www/server/panel/data/port.pl"
BT_PANEL_API_CONF = "/www/server/panel/config/api.json"
# 面板启动的进程会被注入环境变量 BT_PYTHON_SERVICE_SID —— 以此区分手动 nohup 的遗留进程
BT_SERVICE_LOG = "/www/server/python_project/service/qqbot/main.log"

# 本地仓库根（mcp_server.py 所在目录）
LOCAL_ROOT = os.path.dirname(os.path.abspath(__file__))
DEPLOY_HISTORY_PATH = os.path.join(LOCAL_ROOT, "data", "deploy_history.json")

# ─── 部署白名单（只允许 AI 改这些路径，其余一律拒绝）───
# 安全策略：白名单优于黑名单。新增的敏感文件（如 .env、密钥、DB）默认就被挡住。
# 想放行更多路径，必须显式改这里 —— 这是有意的摩擦。
_DEPLOY_ALLOW_DIRS = ("bot/", "web/", "model/")            # 允许的代码目录（递归）
_DEPLOY_ALLOW_TOP_FILES = ("main.py", "requirements.txt")  # 允许的顶层文件
_DEPLOY_DENY_SUFFIXES = (".pyc", ".pyo")                    # 目录内仍排除的编译产物

# stdout/stderr 分流: 日志走 stderr，stdout 只走 MCP 协议
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] qqbot-mcp: %(message)s",
)
log = logging.getLogger("qqbot-mcp")

# ─── Token 缓存 ───
_token: str = ""
_token_expires: float = 0


def _login() -> str:
    """登录获取 JWT。失败抛异常。"""
    global _token, _token_expires
    if not PASSWORD:
        raise RuntimeError(
            "QQBOT_PASSWORD 环境变量未设置 — 无法登录到 qqbot Web API"
        )
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"password": PASSWORD},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"登录失败: {data}")
    _token = data["token"]
    # 提前 5 分钟过期，避免边界 401
    _token_expires = time.time() + data.get("expires_in", 86400) - 300
    log.info(f"登录成功 {BASE_URL}，token 有效期至 {time.ctime(_token_expires)}")
    return _token


def _token_valid() -> bool:
    return bool(_token) and time.time() < _token_expires


def api(method: str, path: str, **kwargs) -> object:
    """调用 API，自动处理鉴权 + 401 重试一次。

    返回: JSON 解析后的对象 (dict/list)
    异常: 网络错误/HTTP 错误会抛出，由调用方捕获
    """
    if not _token_valid():
        _login()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_token}"
    kwargs["headers"] = headers
    kwargs.setdefault("timeout", TIMEOUT)

    r = requests.request(method, f"{BASE_URL}{path}", **kwargs)

    # 401 → 重新登录重试一次
    if r.status_code == 401:
        log.info("Token 失效，重新登录")
        _login()
        headers["Authorization"] = f"Bearer {_token}"
        r = requests.request(method, f"{BASE_URL}{path}", **kwargs)

    r.raise_for_status()
    return r.json()


def _format(data) -> str:
    """把 dict/list 格式化为人类可读 JSON 字符串。"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════
#  部署辅助（SSH/SFTP）
# ═══════════════════════════════════════════════════════════

def _ssh_check() -> None:
    """部署前校验：paramiko 装了 + SSH 配置全。失败抛异常。"""
    if paramiko is None:
        raise RuntimeError(
            "缺少依赖 'paramiko'。请运行: pip install paramiko"
        )
    if Fernet is None:
        raise RuntimeError(
            "缺少依赖 'cryptography'。请运行: pip install cryptography"
        )
    missing = [k for k, v in [
        ("QQBOT_SSH_HOST", SSH_HOST),
        ("QQBOT_SSH_PASSWORD", SSH_PASSWORD),
    ] if not v]
    if missing:
        raise RuntimeError(
            f"SSH 配置不完整，缺少环境变量: {', '.join(missing)}"
        )


def _ssh_connect() -> "paramiko.SSHClient":
    """建立 SSH 连接，返回 client。失败抛异常。"""
    _ssh_check()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        SSH_HOST, port=SSH_PORT, username=SSH_USER,
        password=SSH_PASSWORD, timeout=15,
    )
    return client


def _detect_changed_files() -> list[str]:
    """用 git diff 检测本地已修改但未部署的文件（相对路径、POSIX 风格）。"""
    out = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=LOCAL_ROOT, stderr=subprocess.DEVNULL,
    ).decode("utf-8", "replace").strip()
    staged = subprocess.check_output(
        ["git", "diff", "--name-only", "--cached"],
        cwd=LOCAL_ROOT, stderr=subprocess.DEVNULL,
    ).decode("utf-8", "replace").strip()
    raw = set()
    for blob in (out, staged):
        for line in blob.splitlines():
            line = line.strip().replace("\\", "/")
            if line:
                raw.add(line)
    return sorted(f for f in raw if _should_deploy_file(f))


def _validate_rel_path(rel_path: str) -> Optional[str]:
    """白名单 + 路径穿越校验。返回清洗后的 POSIX 路径；非法返回 None。

    白名单策略：只允许显式列出的目录/顶层文件，其余一律拒绝。
    这比黑名单更安全——新增的敏感文件（如 .env、密钥）默认就被挡住。
    """
    if not rel_path:
        return None
    p = rel_path.replace("\\", "/").strip().lstrip("/")
    # 防 zip-slip / 绝对路径 / 路径穿越
    if p.startswith("/") or ".." in p.split("/"):
        return None
    # 目录内仍排除编译产物
    if any(p.endswith(s) for s in _DEPLOY_DENY_SUFFIXES):
        return None
    # 顶层文件：必须在显式白名单内
    if "/" not in p:
        return p if p in _DEPLOY_ALLOW_TOP_FILES else None
    # 子目录：必须落在允许的目录前缀下
    for d in _DEPLOY_ALLOW_DIRS:
        if p == d.rstrip("/") or p.startswith(d):
            return p
    return None


def _should_deploy_file(rel_path: str) -> bool:
    """白名单校验：只有显式允许的路径才能部署。"""
    return _validate_rel_path(rel_path) is not None


# ─── 部署加密（Fernet / AES-128-CBC + HMAC-SHA256）───
_deploy_key: bytes = b""


def _get_deploy_key() -> bytes:
    """获取/生成部署加密密钥。优先读 QQBOT_DECRYPT_KEY 环境变量。

    密钥只用于本地加密 + 经 SSH 加密通道传给服务器解密，
    因此服务器端无需预先配置密钥。
    """
    global _deploy_key
    if _deploy_key:
        return _deploy_key
    env_key = os.getenv("QQBOT_DECRYPT_KEY", "").strip()
    if env_key:
        _deploy_key = env_key.encode()
        return _deploy_key
    # 未配置 → 本次生成临时密钥并提示持久化
    _deploy_key = Fernet.generate_key()
    sys.stderr.write(
        "[qqbot-mcp] ⚠️ QQBOT_DECRYPT_KEY 未设置 — 本次使用临时密钥。\n"
        "  如需固定密钥，请把下面这行加入环境变量 QQBOT_DECRYPT_KEY：\n"
        f"  {_deploy_key.decode()}\n"
    )
    return _deploy_key


def _encrypt_file(plain_path: str, enc_path: str) -> None:
    """用 Fernet 加密文件内容，输出到 enc_path。"""
    key = _get_deploy_key()
    with open(plain_path, "rb") as f:
        token = Fernet(key).encrypt(f.read())
    with open(enc_path, "wb") as f:
        f.write(token)


def _pack_files(files: list[str], zip_path: str) -> tuple[int, int]:
    """把本地文件按相对路径打进 zip。每个路径都要过白名单校验。返回 (文件数, 总字节)。"""
    file_count = 0
    total_bytes = 0
    rejected: list[str] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in files:
            safe = _validate_rel_path(rel)
            if safe is None:
                rejected.append(rel)
                continue
            abs_path = os.path.join(LOCAL_ROOT, safe.replace("/", os.sep))
            if not os.path.isfile(abs_path):
                log.warning(f"跳过不存在的文件: {safe}")
                continue
            # arc 名用校验过的白名单路径，杜绝 zip-slip
            zf.write(abs_path, safe)
            file_count += 1
            total_bytes += os.path.getsize(abs_path)
    if rejected:
        log.warning(
            f"白名单拒绝 {len(rejected)} 个文件（只允许 {_DEPLOY_ALLOW_DIRS} + "
            f"{_DEPLOY_ALLOW_TOP_FILES}）: {rejected[:10]}"
        )
    return file_count, total_bytes


def _ssh_exec(client: "paramiko.SSHClient", cmd: str, timeout: int = 30) -> tuple[str, str, int]:
    """执行远程命令，返回 (stdout, stderr, exit_code)。"""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return (
        stdout.read().decode("utf-8", "replace").strip(),
        stderr.read().decode("utf-8", "replace").strip(),
        exit_code,
    )


def _bt_panel_api(client: "paramiko.SSHClient", path: str, data: dict, timeout: int = 90) -> dict:
    """调宝塔面板 API（经 SSH 在服务器本地 curl 127.0.0.1，密钥不出服务器）。

    request_time 取服务器自身时钟，避免本机与服务器时钟漂移导致校验失败。
    返回面板 JSON（dict）。失败抛异常，由调用方决定降级路径。
    """
    port, _, _ = _ssh_exec(client, f"cat {BT_PANEL_PORT_FILE}")
    port = port.strip()
    if not port.isdigit():
        raise RuntimeError(f"读面板端口失败: {port!r}")
    conf_raw, _, _ = _ssh_exec(client, f"cat {BT_PANEL_API_CONF}")
    api_token = ""
    try:
        api_token = (json.loads(conf_raw) or {}).get("token", "")
    except json.JSONDecodeError:
        pass
    if not api_token:
        raise RuntimeError("读面板 API token 失败（api.json 缺 token 或面板未开启 API）")
    srv_time, _, _ = _ssh_exec(client, "date +%s")
    request_time = srv_time.strip()
    request_token = hashlib.md5((request_time + api_token).encode()).hexdigest()
    form = "&".join(f"{k}={v}" for k, v in data.items())
    # 面板可能没开 SSL（换机/重装后常见），https 握手失败时退回 http（仅 127.0.0.1 本地回环）
    last_err = ""
    for scheme in ("https", "http"):
        cmd = (
            f"curl -sk -m {timeout} -X POST '{scheme}://127.0.0.1:{port}{path}' "
            f"-d '{form}' -d 'request_time={request_time}' -d 'request_token={request_token}'"
        )
        out, err, code = _ssh_exec(client, cmd, timeout=timeout + 10)
        if code != 0:
            last_err = f"{scheme} curl exit={code} stderr={err[:200]}"
            continue
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            last_err = f"{scheme} 面板返回非 JSON: {out[:300]}"
    raise RuntimeError(last_err or "面板 API 调用失败")


def _pgrep_bot(client: "paramiko.SSHClient") -> Optional[str]:
    """返回 Bot 主进程 PID（pgrep 第一个匹配），没有则 None。"""
    out, _, _ = _ssh_exec(client, f"pgrep -f '{PROCESS_PATTERN}'")
    return out.splitlines()[0].strip() if out.strip() else None


def _pid_is_panel_managed(client: "paramiko.SSHClient", pid: Optional[str]) -> bool:
    """面板启动的进程带 BT_PYTHON_SERVICE_SID 环境变量——以此区分手动 nohup 的遗留进程。"""
    if not pid:
        return False
    out, _, _ = _ssh_exec(
        client,
        f"grep -aq BT_PYTHON_SERVICE_SID /proc/{pid}/environ 2>/dev/null && echo yes || echo no",
    )
    return out.strip().endswith("yes")


def _panel_restart(
    client: "paramiko.SSHClient", deploy_id: str, old_pid: Optional[str]
) -> tuple[Optional[str], Optional[str], str, list[str]]:
    """通过宝塔面板 API 重启 Bot，让面板接管进程管理。

    流程（优先级从高到低，保证 Bot 不下线）:
      1. 调面板 restart_project —— 常规路径
      2. 若面板没能替换旧进程（典型：旧进程是早期部署脚本手动 nohup 的遗留进程，
         面板的 stop 管不住它），杀掉旧进程后调面板 start_project 让面板认领 —— 一次性迁移
      3. 面板路线彻底不可用时，退回手动 setsid nohup（老方案），保底可用

    返回 (new_pid, restart_error, restart_via, notes)
    """
    notes: list[str] = []

    # 1. 面板 restart
    try:
        resp = _bt_panel_api(
            client, "/project/python/restart_project/python",
            {"project_name": BT_PROJECT_NAME},
        )
        notes.append(f"面板 restart 响应: {resp.get('msg', resp)}")
        if resp.get("status") is not True:
            notes.append(f"⚠️ 面板返回 status!=true: {resp}")
    except Exception as e:
        notes.append(f"⚠️ 面板 restart 调用失败: {e}")

    time.sleep(6)
    new_pid = _pgrep_bot(client)
    if new_pid and new_pid != old_pid:
        return new_pid, None, "panel_api", notes

    # 2. 旧进程没被面板替换 → 杀掉（兼容遗留手动进程），让面板 start 认领
    if new_pid:
        legacy = not _pid_is_panel_managed(client, new_pid)
        notes.append(
            f"面板未替换旧进程 PID={new_pid}"
            + ("（手动启动的遗留进程，迁移到面板管理）" if legacy else "")
        )
        _ssh_exec(client, f"kill {new_pid}", timeout=5)
        time.sleep(1.5)
        if _pgrep_bot(client):
            _ssh_exec(client, f"kill -9 {new_pid}", timeout=5)
            time.sleep(1)
    try:
        resp = _bt_panel_api(
            client, "/project/python/start_project/python",
            {"project_name": BT_PROJECT_NAME},
        )
        notes.append(f"面板 start 响应: {resp.get('msg', resp)}")
    except Exception as e:
        notes.append(f"⚠️ 面板 start 调用失败: {e}")
    time.sleep(5)
    new_pid = _pgrep_bot(client)
    if new_pid:
        return new_pid, None, "panel_api+migration", notes

    # 3. 面板路线失败 → 手动兜底（Bot 绝不能没人拉起来）
    notes.append("⚠️ 面板重启未生效，退回手动 nohup 启动")
    startup = (
        f"cd {REMOTE_PROJECT_DIR} && "
        f"setsid bash -c '"
        f"source {REMOTE_ENV_SOURCE} 2>/dev/null; "
        f"nohup {STARTUP_CMD} > /tmp/qqbot_stdout_{deploy_id}.log 2>&1 < /dev/null &"
        f"'"
    )
    _ssh_exec(client, startup, timeout=15)
    time.sleep(3)
    new_pid = _pgrep_bot(client)
    if new_pid:
        return new_pid, None, "manual_fallback", notes

    restart_error = "重启失败（面板与手动启动均未拉起进程）"
    for diag_cmd in (
        f"tail -40 {BT_SERVICE_LOG} 2>/dev/null",
        f"tail -40 /tmp/qqbot_stdout_{deploy_id}.log 2>/dev/null",
    ):
        so, _, _ = _ssh_exec(client, diag_cmd, timeout=10)
        if so.strip():
            restart_error += f" | 日志末尾:\n{so.strip()}"
            break
    return None, restart_error, "failed", notes


def _load_history() -> list[dict]:
    """读部署历史。"""
    try:
        with open(DEPLOY_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history(records: list[dict]) -> None:
    """写部署历史（最多保留 100 条）。"""
    os.makedirs(os.path.dirname(DEPLOY_HISTORY_PATH), exist_ok=True)
    with open(DEPLOY_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(records[-100:], f, ensure_ascii=False, indent=2)


# 启动日志里这些关键字命中 = 失败
_ERROR_PATTERNS = (
    "Traceback", "ImportError", "ModuleNotFoundError",
    "Address already in use", "FATAL", "CRITICAL",
    "SyntaxError", "IndentationError",
)


def _scan_log_for_errors(log_lines: list[str]) -> list[str]:
    """扫描日志行，返回疑似错误的片段。"""
    errors = []
    for line in log_lines:
        if any(p in line for p in _ERROR_PATTERNS):
            errors.append(line)
            if len(errors) >= 10:
                break
    return errors


# ═══════════════════════════════════════════════════════════
#  MCP Server 定义
# ═══════════════════════════════════════════════════════════

server = MCPServer(
    name="qqbot",
    version="1.0.0",
    instructions=(
        "通过 qqbot.crisxie.top 的 Web API 连接到部署的 QQ 机器人实例。"
        "用于读取服务器日志、查询运行时状态、诊断问题。"
        "调任何工具前先确认 QQBOT_PASSWORD 已配置。"
    ),
)


@server.tool()
def get_logs(lines: int = 200, level: str = "", offset: int = 0) -> str:
    """获取 qqbot 运行日志（最新的在最后）。

    Args:
        lines: 返回行数 (1-2000，默认 200)
        level: 按级别过滤 (DEBUG/INFO/WARNING/ERROR)，空=不过滤
        offset: 跳过最新 N 行后再取（用于翻更老的）

    Returns:
        日志文本（每行一条），或错误信息。
    """
    try:
        data = api(
            "GET", "/api/logs",
            params={"lines": lines, "level": level, "offset": offset},
        )
        if not isinstance(data, dict):
            return f"(意外的返回类型: {type(data).__name__})"
        result_lines = data.get("lines", [])
        meta = (
            f"(共 {data.get('total', '?')} 行，过滤后 {data.get('filtered_total', '?')} 行，"
            f"返回 {data.get('returned', len(result_lines))} 行)\n"
        )
        if not result_lines:
            return meta + "(无日志)"
        return meta + "\n".join(result_lines)
    except Exception as e:
        return f"❌ 获取日志失败: {e}"


@server.tool()
def tail_logs(
    keyword: str = "", level: str = "WARNING", lines: int = 30,
) -> str:
    """按关键字或级别过滤最近日志（定位问题用，最新在最后）。

    Args:
        keyword: 关键字（大小写不敏感，空=不过滤）
        level: 级别 (默认 WARNING，即 WARNING+ERROR+CRITICAL)
        lines: 最多返回多少行 (默认 30)

    Returns:
        匹配的日志文本。
    """
    try:
        data = api("GET", "/api/logs", params={"lines": 1000, "level": level})
        all_lines = data.get("lines", []) if isinstance(data, dict) else []
        if keyword:
            kw_lower = keyword.lower()
            all_lines = [l for l in all_lines if kw_lower in l.lower()]
        if not all_lines:
            return f"(level={level} keyword={keyword!r} 无匹配)"
        return "\n".join(all_lines[-lines:])
    except Exception as e:
        return f"❌ 过滤日志失败: {e}"


@server.tool()
def get_bot_status() -> str:
    """获取 Bot 运行状态（在线/离线、运行时长、群数、消息统计等）。

    Returns:
        JSON 格式的状态信息。
    """
    try:
        data = api("GET", "/api/bot/status")
        return _format(data)
    except Exception as e:
        return f"❌ 获取状态失败: {e}"


@server.tool()
def get_dashboard() -> str:
    """获取仪表盘总览（违规统计、审核统计、消息量等关键指标）。

    Returns:
        JSON 格式的总览数据。
    """
    try:
        data = api("GET", "/api/dashboard")
        return _format(data)
    except Exception as e:
        return f"❌ 获取仪表盘失败: {e}"


@server.tool()
def get_groups() -> str:
    """获取机器人加入的所有群列表。

    Returns:
        JSON 格式的群列表。
    """
    try:
        data = api("GET", "/api/groups")
        return _format(data)
    except Exception as e:
        return f"❌ 获取群列表失败: {e}"


@server.tool()
def get_memory_stats() -> str:
    """获取长期记忆统计（用户画像数、对话摘要数、知识碎片数）。

    用于诊断"忘事"问题——如果记忆条数很少，说明记忆系统没在攒数据。
    """
    try:
        data = api("GET", "/api/memory/stats")
        return _format(data)
    except Exception as e:
        return f"❌ 获取记忆统计失败: {e}"


@server.tool()
def get_verify_history(limit: int = 20) -> str:
    """获取加群验证历史记录（看新人通过/被踢情况）。

    Args:
        limit: 返回最近多少条 (默认 20)

    Returns:
        JSON 格式的验证记录列表（最新在前）。
    """
    try:
        data = api("GET", "/api/verify/history")
        items = data if isinstance(data, list) else data.get("items", [])
        return _format(items[:limit] if isinstance(items, list) else items)
    except Exception as e:
        return f"❌ 获取验证历史失败: {e}"


@server.tool()
def search_violations(group_id: int = 0, limit: int = 20) -> str:
    """查询违规记录（诊断审核误判/漏判）。

    Args:
        group_id: 群号 (0=所有群)
        limit: 返回条数 (默认 20)

    Returns:
        JSON 格式的违规记录列表。
    """
    try:
        params = {"limit": limit}
        if group_id:
            params["group_id"] = group_id
        data = api("GET", "/api/violations", params=params)
        return _format(data)
    except Exception as e:
        return f"❌ 查询违规失败: {e}"


# ═══════════════════════════════════════════════════════════
#  部署工具（SSH/SFTP）
# ═══════════════════════════════════════════════════════════

@server.tool()
def deploy_and_test(
    files: Optional[list[str]] = None,
    restart: bool = True,
    wait_secs: int = 60,
    note: str = "",
) -> str:
    """把本地变更文件部署到生产服务器、重启 Bot、扫描启动日志判断成败。

    重启通过宝塔面板 API（/project/python/restart_project）执行，面板是 Bot 进程的
    唯一管理者，避免与面板自启冲突；面板不可用时自动退回手动启动兜底。

    Args:
        files: 要部署的相对路径列表（POSIX 风格，如 ["bot/chat.py", "web/api.py"]）。
               None 或空 = 自动用 git diff 检测已修改文件。
               安全约束：路径必须在白名单内（bot/ web/ model/ + main.py/requirements.txt），
               含路径穿越(../)或绝对路径一律拒绝；传输经 Fernet/AES 加密。
        restart: 上传后是否重启 Bot 进程（默认 True）。
        wait_secs: 重启后等多少秒再拉日志（默认 60，给 Bot 启动 + WS 连接时间）。
        note: 可选备注，写进历史记录。

    Returns:
        部署结果：状态（ok/failed）、文件清单、PID 变化、重启方式、错误片段（若有）、日志预览。
    """
    deploy_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    ts_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # 1. 确定要传的文件
    try:
        if not files:
            files = _detect_changed_files()
        else:
            files = [f.replace("\\", "/") for f in files if _should_deploy_file(f.replace("\\", "/"))]
    except Exception as e:
        return f"❌ 检测变更文件失败: {e}\n（如果不是 git 仓库，请显式传 files=[...]）"

    if not files:
        return (
            "ℹ️ 没有可部署的文件。\n"
            "可能原因：\n"
            "  - 改动的文件不在白名单内（只允许 bot/ web/ model/ + main.py/requirements.txt）\n"
            "  - 不是 git 仓库且没传 files 参数\n"
            "请显式传 files=['bot/xxx.py', ...]（路径必须在白名单目录下）。"
        )

    log.info(f"部署 {deploy_id}: {len(files)} 文件, restart={restart}")

    # 2. 打包
    local_zip = os.path.join(LOCAL_ROOT, f"deploy_{deploy_id}.zip")
    try:
        file_count, total_bytes = _pack_files(files, local_zip)
    except Exception as e:
        return f"❌ 打包失败: {e}"

    if file_count == 0:
        try:
            os.remove(local_zip)
        except OSError:
            pass
        return f"❌ 打包后 0 文件（原始 {len(files)} 个路径都不存在？）"

    # 3. SSH 连接 + 加密上传 + 服务器解密 + 解压 + 备份 + 重启
    remote_enc = f"/tmp/qqbot_deploy_{deploy_id}.enc"
    remote_zip = f"/tmp/qqbot_deploy_{deploy_id}.zip"
    backup_dir = f"{REMOTE_PROJECT_DIR}/.backups/{deploy_id}"
    new_pid = None
    old_pid = None
    upload_error = None
    unzip_error = None
    restart_error = None
    restart_via = None

    try:
        client = _ssh_connect()
    except Exception as e:
        try:
            os.remove(local_zip)
        except OSError:
            pass
        return f"❌ SSH 连接失败: {e}"

    try:
        # 3a. 加密 zip（Fernet/AES）→ 本地密文 .enc
        _encrypt_file(local_zip, local_zip + ".enc")

        # 3b. 上传密文（传输内容已加密；即使 /tmp 落地泄露，拿到的也是密文）
        sftp = client.open_sftp()
        try:
            sftp.put(local_zip + ".enc", remote_enc)
            log.info(
                f"上传密文完成: {remote_enc} "
                f"({os.path.getsize(local_zip + '.enc')} 字节密文 / {total_bytes} 字节明文)"
            )
        finally:
            sftp.close()

        # 3c. 服务器端解密（密钥经 SSH stdin 传入，不进命令行/进程列表/历史）
        decrypt_cmd = (
            "python3 -c '"
            "import sys;"
            "from cryptography.fernet import Fernet;"
            "k=sys.stdin.buffer.readline().strip();"
            "d=Fernet(k).decrypt(open(\"" + remote_enc + "\",\"rb\").read());"
            "open(\"" + remote_zip + "\",\"wb\").write(d);"
            "print(\"DECRYPT_OK\")'"
        )
        din, dout, derr = client.exec_command(decrypt_cmd, timeout=30)
        din.write(_get_deploy_key() + b"\n")
        din.flush()
        din.channel.shutdown_write()
        d_exit = dout.channel.recv_exit_status()
        d_out = dout.read().decode("utf-8", "replace").strip()
        d_err = derr.read().decode("utf-8", "replace").strip()
        if d_exit != 0 or "DECRYPT_OK" not in d_out:
            raise RuntimeError(
                f"服务器解密失败: exit={d_exit} out={d_out[:200]} err={d_err[:200]}"
            )
        log.info(f"服务器解密完成 → {remote_zip}")

        # 3d. 立即删除服务器密文（减少落地痕迹）
        _ssh_exec(client, f"rm -f {remote_enc}", timeout=5)

        # 3e. 解压 + 备份（一条命令，原子性）
        # 关键：先建备份目录，对每个待覆盖文件做 cp，再 unzip 覆盖
        unzip_cmd = (
            f"cd {REMOTE_PROJECT_DIR} && "
            f"mkdir -p {backup_dir} && "
            f"for f in $(unzip -Z1 {remote_zip}); do "
            f"  [ -f \"$f\" ] && mkdir -p \"{backup_dir}/$(dirname \"$f\")\" && cp -a \"$f\" \"{backup_dir}/$f\"; "
            f"done && "
            f"unzip -o {remote_zip} && "
            f"rm -f {remote_zip} && "
            f"echo UNZIP_OK"
        )
        out, err, code = _ssh_exec(client, unzip_cmd, timeout=60)
        if code != 0 or "UNZIP_OK" not in out:
            unzip_error = f"exit={code} stdout={out[:300]} stderr={err[:300]}"
            raise RuntimeError(f"解压失败: {unzip_error}")
        log.info(f"解压完成: {out.splitlines()[-1] if out else ''}")

        # 重启 —— 走宝塔面板 API（面板是进程唯一管理者，杜绝"面板自启 + 脚本手动启"双实例冲突）
        if restart:
            old_pid = _pgrep_bot(client)
            if old_pid:
                log.info(f"旧 PID: {old_pid}")
            new_pid, restart_error, restart_via, restart_notes = _panel_restart(client, deploy_id, old_pid)
            for n in restart_notes:
                log.info(f"重启: {n}")
            if new_pid:
                log.info(f"新 PID: {new_pid} (via {restart_via})")

    except Exception as e:
        # 任何阶段失败都尽快返回，但保证 history 落盘
        log.error(f"部署 {deploy_id} 失败: {e}")
        err_msgs = [str(e)] if str(e) else ["（异常无消息）"]
        try:
            so, _, _ = _ssh_exec(client, f"tail -40 /tmp/qqbot_stdout_{deploy_id}.log 2>/dev/null", timeout=10)
            if so.strip():
                err_msgs.append(f"stdout 末尾:\n{so.strip()}")
        except Exception:
            pass
        record = {
            "id": deploy_id,
            "timestamp": ts_iso,
            "files": files,
            "file_count": file_count,
            "bytes": total_bytes,
            "restart": restart,
            "restart_via": restart_via,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "status": "failed",
            "errors": err_msgs,
            "note": note,
            "backup_dir": backup_dir if not unzip_error else None,
        }
        _append_history(record)
        for _p in (local_zip, local_zip + ".enc"):
            try:
                os.remove(_p)
            except OSError:
                pass
        client.close()
        return _format_deploy_result(record, [], phase="中途失败")

    client.close()

    # 4. 等待 + 拉日志
    log_lines = []
    log_preview = []
    errors_found: list[str] = []
    if restart and new_pid:
        log.info(f"等 {wait_secs}s 让 Bot 启动...")
        time.sleep(wait_secs)
        try:
            data = api("GET", "/api/logs", params={"lines": 100, "level": "WARNING"})
            if isinstance(data, dict):
                log_lines = data.get("lines", [])
                errors_found = _scan_log_for_errors(log_lines)
                log_preview = log_lines[-15:]
        except Exception as e:
            log.warning(f"拉启动日志失败（不影响部署）: {e}")

    # 5. 判断成败
    status = "ok"
    if restart_error:
        status = "failed"
        errors_found.insert(0, f"重启: {restart_error}")
    elif restart and new_pid and errors_found:
        status = "failed"

    record = {
        "id": deploy_id,
        "timestamp": ts_iso,
        "files": files,
        "file_count": file_count,
        "bytes": total_bytes,
        "restart": restart,
        "restart_via": restart_via,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "status": status,
        "errors": errors_found,
        "note": note,
        "backup_dir": backup_dir,
    }
    _append_history(record)

    for _p in (local_zip, local_zip + ".enc"):
        try:
            os.remove(_p)
        except OSError:
            pass

    return _format_deploy_result(record, log_preview, phase="完成")


@server.tool()
def get_deploy_history(limit: int = 10) -> str:
    """获取最近 N 次部署历史记录。

    Args:
        limit: 返回最近多少条（默认 10）

    Returns:
        部署历史列表，每条含 id/时间/文件/状态/错误/备注。
    """
    try:
        records = _load_history()
        if not records:
            return "（暂无部署历史）"
        recent = records[-limit:]
        recent.reverse()  # 最新在前
        out = [f"📋 部署历史（最近 {len(recent)} 条，最新在前）:\n"]
        for r in recent:
            status_emoji = "✅" if r.get("status") == "ok" else "❌"
            out.append(f"[{r.get('timestamp', '?')}] id={r.get('id')} {status_emoji} {r.get('status', '?')}")
            files = r.get("files", [])
            if files:
                preview = ", ".join(files[:5]) + (f" ... (+{len(files) - 5})" if len(files) > 5 else "")
                out.append(f"   {r.get('file_count', len(files))} 文件: {preview}")
            pid_change = ""
            if r.get("old_pid") or r.get("new_pid"):
                pid_change = f"PID: {r.get('old_pid', '?')} → {r.get('new_pid', '?')}"
                out.append(f"   {pid_change}")
            if r.get("errors"):
                for e in r["errors"][:3]:
                    out.append(f"   ⚠️ {e[:100]}")
            if r.get("note"):
                out.append(f"   备注: {r['note']}")
            out.append("")
        return "\n".join(out)
    except Exception as e:
        return f"❌ 读取部署历史失败: {e}"


def _append_history(record: dict) -> None:
    """追加一条部署记录到本地历史文件。"""
    records = _load_history()
    records.append(record)
    _save_history(records)


def _format_deploy_result(record: dict, log_preview: list[str], phase: str = "完成") -> str:
    """把部署结果格式化成给 AI 看的字符串。"""
    status = record.get("status", "?")
    emoji = "✅" if status == "ok" else "❌"
    files = record.get("files", [])
    size_kb = record.get("bytes", 0) / 1024

    lines = [
        f"{emoji} 部署{phase} (id={record.get('id')})",
        f"   状态: {status}",
        f"   文件: {record.get('file_count', 0)} 个 ({size_kb:.1f} KB)",
    ]
    if files:
        preview = ", ".join(files[:8]) + (f" ... (+{len(files) - 8})" if len(files) > 8 else "")
        lines.append(f"   清单: {preview}")
    if record.get("old_pid") or record.get("new_pid"):
        lines.append(f"   PID: {record.get('old_pid', '?')} → {record.get('new_pid', '?')}")
    if record.get("restart_via"):
        lines.append(f"   重启方式: {record['restart_via']}")
    if record.get("errors"):
        lines.append(f"   ⚠️ 错误 ({len(record['errors'])} 条):")
        for e in record["errors"][:5]:
            lines.append(f"      {e[:120]}")
    if record.get("backup_dir"):
        lines.append(f"   备份: {record['backup_dir']}")
        if status == "failed":
            lines.append(f"   💡 回滚: SSH 到服务器，把 {record['backup_dir']}/* 拷回 {REMOTE_PROJECT_DIR}/ 后重启")
    if log_preview:
        lines.append("   日志预览（最后 15 行）:")
        for ln in log_preview:
            lines.append(f"      {ln[:140]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 启动前先试一次登录，密码错了立刻报错而不是让所有工具都失败
    if not PASSWORD:
        sys.stderr.write(
            "[qqbot-mcp] ⚠️ QQBOT_PASSWORD 未设置 — 工具调用时会尝试登录并失败。\n"
            "  请在环境变量或 opencode.json 的 environment 中配置 QQBOT_PASSWORD。\n"
        )
    else:
        try:
            _login()
        except Exception as e:
            sys.stderr.write(f"[qqbot-mcp] ⚠️ 初始登录失败（工具仍会重试）: {e}\n")

    log.info(f"启动 qqbot MCP server (target={BASE_URL})")
    server.run("stdio")
