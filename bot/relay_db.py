"""
中转站数据库访问层（Neon Postgres）
====================================
给 AI 提供 LLM 中转站（new-api 系）数据库的访问能力：

- 读取：所有人可用（只读事务，PostgreSQL 服务端强制 READ ONLY）
- 修改：仅主人可用（ai_tools.execute_tool 里校验 caller 身份后才调 run_write）

安全设计（纵深防御）：
1. 单条语句：拒绝多语句 / 注释伪装（psycopg 扩展协议本身也不允许多语句）
2. 非主人查询包在 ``BEGIN READ ONLY`` 事务里执行 —— 即使 SQL 藏了
   data-modifying CTE（``WITH x AS (DELETE ...) SELECT ...``），PostgreSQL
   也会直接报错，不靠正则兜底
3. ``set_config('statement_timeout', %s, true)`` 参数化设置语句超时，防慢查询
4. 敏感列（password / key / secret / *_hash 等）输出时自动打码，
   防止凭据漏进群聊
5. ``prepare_threshold=None``：Neon pooler(pgbouncer 事务模式) 下
   禁用服务端预编译语句，避免跨事务复用报错
6. 同步 psycopg 驱动 + ``asyncio.to_thread``：不挑事件循环类型
   （Windows Proactor / Linux epoll 都能跑）

关于动态执行 AI 生成 SQL 的说明：run_read/run_write 收到的 ``sql`` 参数
本身就是一条【完整的单条语句】（由 LLM 产出、经 check_*_sql 校验），
不是把外部输入拼进 SQL 模板 —— 全模块没有任何用户数据被拼接/format 进
SQL 文本；参数一律走 %s 绑定。数据修改风险由「READ ONLY 事务（服务端
强制）+ 写路径仅主人可达」控制。
"""
import asyncio
import logging
import re
import time

from .config import config

logger = logging.getLogger("qqbot")

# ── 语句限制 ──────────────────────────────────────────────
# 只读语句允许的起始关键字（WITH 也放行：分析型 CTE 很常用；
# 内藏 DML 的情况由 READ ONLY 事务在服务端拦截）
_READ_ONLY_PREFIXES = ("select", "with", "show", "explain", "table", "values")

_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_MAX_SQL_LEN = 5000

# ── 输出限制（给 LLM 看的结果要紧凑）───────────────────────
MAX_ROWS = 40          # 最多返回行数（超出提示截断）
CELL_LIMIT = 80        # 单个单元格最多字符
OUTPUT_LIMIT = 4500    # 整段结果最多字符（超出尾部截断）

# ── 敏感列打码 ────────────────────────────────────────────
# 命中这些模式的列，输出时替换为 ***（密码/密钥/令牌哈希不进群聊）
_SENSITIVE_COL_RE = re.compile(
    r"(password|passwd|secret|credential|token_hash|refresh_hash|"
    r"code_hash|private_key|access_token|(^|_)key$)",
    re.IGNORECASE,
)

# ── 超时（毫秒，走 set_config 参数绑定）────────────────────
READ_TIMEOUT_MS = 8000     # 只读查询语句超时
WRITE_TIMEOUT_MS = 15000   # 主人写操作语句超时
CONNECT_TIMEOUT_SECS = 15

# ── schema 缓存 ───────────────────────────────────────────
_SCHEMA_TTL = 600  # 10 分钟
_schema_cache: dict = {"text": "", "ts": 0.0}

# ── 查询冷却（防止 LLM 一轮对话里刷库）────────────────────
_QUERY_COOLDOWN = 8.0
_query_last: dict = {}


def is_configured() -> bool:
    return bool(getattr(config, "RELAY_DB_URL", ""))


def check_query_cooldown(key: int) -> bool:
    """读查询冷却（key=群号或私聊对方QQ）。过冷却返回 True。"""
    now = time.time()
    if now - _query_last.get(key, 0.0) < _QUERY_COOLDOWN:
        return False
    _query_last[key] = now
    return True


# ═══════════════════════════════════════════════════════════
#  语句检查（纯函数，便于测试）
# ═══════════════════════════════════════════════════════════

def _strip_sql_comments(sql: str) -> str:
    return _SQL_COMMENT_RE.sub(" ", sql)


def _is_single_statement(sql: str) -> bool:
    """去掉注释后只允许一个结尾分号（分号出现在字符串字面量里会被误拒，
    可接受 —— 提示 AI 拆成单条即可）。"""
    body = sql.strip().rstrip(";").strip()
    return ";" not in body


def check_read_sql(sql: str) -> tuple[bool, str]:
    """校验只读 SQL。返回 (ok, 错误信息)。"""
    if not sql or not sql.strip():
        return False, "SQL 为空"
    if len(sql) > _MAX_SQL_LEN:
        return False, "SQL 太长（>5000 字符），请精简"
    if not _is_single_statement(sql):
        return False, "只允许单条语句（不要用分号拼接多条）"
    head = _strip_sql_comments(sql).strip().lstrip("( \t\n").split(None, 1)
    first = (head[0] if head else "").lower()
    if first not in _READ_ONLY_PREFIXES:
        return False, (
            "只读查询必须以 SELECT / WITH / SHOW / EXPLAIN 开头；"
            "要修改数据请让主人来操作"
        )
    return True, ""


def check_write_sql(sql: str) -> tuple[bool, str]:
    """校验写 SQL（主人专用，但仍限制单条 + 长度）。"""
    if not sql or not sql.strip():
        return False, "SQL 为空"
    if len(sql) > _MAX_SQL_LEN:
        return False, "SQL 太长（>5000 字符），请精简"
    if not _is_single_statement(sql):
        return False, "只允许单条语句（不要用分号拼接多条）"
    return True, ""


# ═══════════════════════════════════════════════════════════
#  连接管理（同步连接缓存 + to_thread 执行 + async 层串行）
# ═══════════════════════════════════════════════════════════

_conn = None            # psycopg 同步 Connection（只在 worker 线程里碰）
_run_lock = asyncio.Lock()   # async 层串行化，保证同一时刻只有一条查询在跑


def _connect():
    import psycopg
    return psycopg.connect(
        config.RELAY_DB_URL,
        connect_timeout=CONNECT_TIMEOUT_SECS,
        autocommit=True,           # 事务手动 BEGIN/COMMIT，见模块 docstring
        prepare_threshold=None,    # pgbouncer 事务模式兼容
    )


def _drop_conn():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def _safe_rollback():
    if _conn is not None and not _conn.closed:
        try:
            with _conn.cursor() as cur:
                cur.execute("ROLLBACK")
        except Exception:
            _drop_conn()


def _short_err(e: Exception) -> str:
    return str(e).strip().split("\n")[0][:200]


def _run_in_txn_sync(sql: str, read_only: bool, timeout_ms: int,
                     cell_limit: int = CELL_LIMIT,
                     output_limit: int = OUTPUT_LIMIT) -> str:
    """【worker 线程里跑】在事务里执行单条完整语句 ``sql``。

    ``sql`` 必须已经过 check_read_sql / check_write_sql 校验（单条、
    长度受限；本函数不再拼接任何值进 SQL）。
    read_only=True: BEGIN READ ONLY ... ROLLBACK（服务端强制只读）
    read_only=False: BEGIN ... COMMIT（主人写路径）
    连接级错误重建连接重试一次；SQL 错误原样返回错误文本。
    """
    import psycopg
    global _conn
    for attempt in (1, 2):
        try:
            if _conn is None or _conn.closed:
                _conn = _connect()
            conn = _conn
        except Exception as e:
            return f"数据库连接失败: {_short_err(e)}"
        try:
            with conn.cursor() as cur:
                if read_only:
                    cur.execute("BEGIN READ ONLY")
                else:
                    cur.execute("BEGIN")
                # 超时走参数绑定（SET 语句本身不支持占位符，set_config 支持）
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
                cur.execute(sql)
                if cur.description is not None:
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(MAX_ROWS + 1)
                    result = _format_result(cols, rows,
                                            cell_limit=cell_limit,
                                            output_limit=output_limit)
                else:
                    result = f"已执行，影响 {cur.rowcount} 行"
            if read_only:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            else:
                with conn.cursor() as cur:
                    cur.execute("COMMIT")
            return result
        except psycopg.errors.QueryCanceled as e:
            _safe_rollback()
            return f"查询超时（>{timeout_ms // 1000}s），请优化 SQL 或缩小范围: {_short_err(e)}"
        except Exception as e:
            _safe_rollback()
            # 连接级故障（断线/网络）→ 重建一次再试；SQL 语法/权限错误直接返回
            if _conn is not None and not _conn.closed and attempt == 1 \
                    and isinstance(e, psycopg.OperationalError):
                _drop_conn()
                continue
            return f"执行失败: {_short_err(e)}"
    return "执行失败: 未知错误"


async def _run_in_txn(sql: str, read_only: bool, timeout_ms: int,
                      cell_limit: int = CELL_LIMIT,
                      output_limit: int = OUTPUT_LIMIT) -> str:
    async with _run_lock:
        return await asyncio.to_thread(
            _run_in_txn_sync, sql, read_only, timeout_ms,
            cell_limit=cell_limit, output_limit=output_limit,
        )


# ═══════════════════════════════════════════════════════════
#  结果格式化（紧凑文本 + 敏感列打码）
# ═══════════════════════════════════════════════════════════

def _fmt_cell(v, limit: int = CELL_LIMIT) -> str:
    if v is None:
        return "NULL"
    s = str(v).replace("\n", "⏎").replace("\r", "")
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _format_result(columns: list, rows: list,
                   cell_limit: int = CELL_LIMIT,
                   output_limit: int = OUTPUT_LIMIT) -> str:
    masked = [
        "***" if _SENSITIVE_COL_RE.search(c or "") else None
        for c in columns
    ]
    lines = [" | ".join(str(c) for c in columns)]
    shown = rows[:MAX_ROWS]
    for r in shown:
        cells = []
        for i, v in enumerate(r):
            if masked[i]:
                cells.append("***")
            else:
                cells.append(_fmt_cell(v, limit=cell_limit))
        lines.append(" | ".join(cells))
    if len(rows) > MAX_ROWS:
        lines.append(f"…（结果超过 {MAX_ROWS} 行，只显示前 {MAX_ROWS} 行，建议加 LIMIT/聚合）")
    out = "\n".join(lines)
    if len(out) > output_limit:
        out = out[:output_limit] + "\n…（结果过长已截断）"
    return out


# ═══════════════════════════════════════════════════════════
#  对外接口
# ═══════════════════════════════════════════════════════════

async def run_read(sql: str) -> str:
    """执行只读查询（任何人）。调用前应先过 check_read_sql。"""
    result = await _run_in_txn(sql, read_only=True, timeout_ms=READ_TIMEOUT_MS)
    # 成功的表结果前加防跑偏说明：AI 看到 username 容易乱映射到群友
    if result and not result.startswith(("执行失败", "数据库连接失败", "查询超时")):
        hint = (
            "（说明：下面这些 username/display_name 是中转站网站的注册用户名，"
            "不是QQ昵称。回答时照实念注册名，不要猜、不要对应到QQ或群友。"
            "quota 换算：500000 ≈ $1。回答必须注明统计的时间范围："
            "users.used_quota 是历史累计；用户问'今天/最近/本月'时，"
            "要用 logs 或 quota_data 的 created_at（Unix秒）过滤出该时段再统计。）"
        )
        result = hint + "\n" + result
    return result


async def run_write(sql: str) -> str:
    """执行写操作（仅主人，调用方负责权限校验）。"""
    return await _run_in_txn(sql, read_only=False, timeout_ms=WRITE_TIMEOUT_MS)


async def describe_schema() -> str:
    """返回 public schema 的表结构清单（列名 + 大约行数），带 TTL 缓存。"""
    if not is_configured():
        return "未配置中转站数据库（RELAY_DB_URL）"
    now = time.time()
    if _schema_cache["text"] and now - _schema_cache["ts"] < _SCHEMA_TTL:
        return _schema_cache["text"]

    # 每表一行聚合（string_agg）：旧写法"每列一行"的原始结果按字母序
    # 在 channels 附近就被 4500 字符输出上限砍掉，模型根本看不到
    # users/logs/quota_data 这些关键表。
    text = await _run_in_txn(
        "SELECT c.table_name, GREATEST(cls.reltuples::bigint, 0) AS approx_rows, "
        "string_agg(c.column_name, ', ' ORDER BY c.ordinal_position) AS cols "
        "FROM information_schema.columns c "
        "JOIN pg_class cls ON cls.relname = c.table_name "
        "JOIN pg_namespace ns ON ns.oid = cls.relnamespace "
        "AND ns.nspname = c.table_schema "
        "WHERE c.table_schema = 'public' AND cls.relkind = 'r' "
        "GROUP BY c.table_name, cls.reltuples "
        "ORDER BY c.table_name",
        read_only=True,
        timeout_ms=READ_TIMEOUT_MS,
        cell_limit=1200,     # 聚合后的列清单单元格较长
        output_limit=16000,  # 全部表都要展示（默认 4500 会在中途截断）
    )
    if text.startswith(("执行失败", "数据库连接失败", "查询超时")):
        return text

    # 原始行格式: "table_name | approx_rows | cols"
    out = [
        "中转站数据库（public schema）表结构：",
        "提示: 时间列(created_at/xxx_time 等)多为 Unix 秒(bigint)，用 to_timestamp(x) 转日期；",
        "      quota/额度换算: 500000 quota ≈ $1。",
    ]
    for line in text.split("\n")[1:]:
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3:
            continue
        tname, rows, cols = parts
        if len(cols) > 1200:
            cols = cols[:1200] + "…"
        out.append(f"- {tname} (约{rows}行): {cols}")
    result = "\n".join(out)
    _schema_cache["text"] = result
    _schema_cache["ts"] = now
    return result


def invalidate_schema_cache() -> None:
    """写操作后可调用，让下次 describe_schema 重新拉取。"""
    _schema_cache["text"] = ""
    _schema_cache["ts"] = 0.0
