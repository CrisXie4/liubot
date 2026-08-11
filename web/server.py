"""
FastAPI Web 服务器 v3 — 安全加固版
- JWT 鉴权中间件
- 安全响应头
- IP 白名单
- 请求体大小限制
- 限流中间件
"""
import logging
import os
import time

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bot.config import config
from bot.security import (
    verify_jwt_token, api_limiter, SECURITY_HEADERS,
    sanitize_html,
)

logger = logging.getLogger("qqbot")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="QQ Bot 控制面板",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,  # 生产环境关闭 OpenAPI schema
)

# ─── CORS ───
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,
)

# ─── 安全响应头中间件 ───
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        if value:
            response.headers[key] = value
    # HTML 片段（动态注入的页面）不缓存，避免更新后浏览器仍用旧版本
    if request.url.path.endswith(".html") or request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    for h in ("X-Powered-By", "Server"):
        try: del response.headers[h]
        except (KeyError, TypeError): pass
    return response


# ─── JWT 鉴权中间件 ───
PUBLIC_PATHS = {
    "/", "/index.html", "/favicon.ico",
    "/api/auth", "/api/auth/login", "/api/auth/status",
    "/static", "/chart.min.js",
    "/login.html", "/assets",
    "/v",  # 网站认证公开页
    "/api/verify/code",  # 网站认证拉词接口
    "/api/verify/config",  # 网站认证公开配置（sitekey）
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """JWT 鉴权：除了公开路径和静态资源，都需要 token"""
    path = request.url.path

    # 公开路径放行（含网站认证的 /v/<token> 和 /api/verify/code/<token>）
    if (path in PUBLIC_PATHS
            or path.startswith("/static")
            or path.startswith("/v/")
            or path.startswith("/api/verify/code/")
            or path.startswith("/api/verify/config")
            or path.endswith((".js", ".css", ".png", ".svg", ".ico", ".woff2", ".map", ".html"))):
        return await call_next(request)

    # API 鉴权
    if path.startswith("/api/"):
        # /api/auth 放行
        if path.startswith("/api/auth"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"detail": "未登录", "code": "UNAUTHORIZED"},
                status_code=401,
            )

        token = auth_header[7:]
        payload = verify_jwt_token(token)
        if payload is None:
            return JSONResponse(
                {"detail": "登录已过期，请重新登录", "code": "TOKEN_EXPIRED"},
                status_code=401,
            )

        # 将用户信息存入 request.state
        request.state.user = payload.get("sub", "admin")
        request.state.login_time = payload.get("iat", 0)

    return await call_next(request)


# ─── IP 白名单中间件 ───
@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    """IP 白名单检查"""
    allowed_ips = config.WEB_ALLOWED_IPS
    if not allowed_ips:
        return await call_next(request)

    client_ip = request.client.host if request.client else ""
    # 检查 X-Forwarded-For
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    if client_ip in allowed_ips or client_ip in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)

    logger.warning(f"拒绝未授权IP访问: {client_ip} → {request.url.path}")
    return JSONResponse(
        {"detail": "IP 未授权", "code": "IP_DENIED"},
        status_code=403,
    )


# ─── 请求体大小限制中间件 ───
MAX_BODY_SIZE = 2 * 1024 * 1024  # 2MB


@app.middleware("http")
async def body_size_limit(request: Request, call_next):
    """限制请求体大小"""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_SIZE:
        return JSONResponse(
            {"detail": "请求体过大", "code": "PAYLOAD_TOO_LARGE"},
            status_code=413,
        )
    return await call_next(request)


# ─── 请求日志中间件 ───
@app.middleware("http")
async def request_logging(request: Request, call_next):
    """记录 API 请求（敏感路径跳过）"""
    t0 = time.time()
    response = await call_next(request)
    elapsed_ms = (time.time() - t0) * 1000
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/auth/status",):
        logger.debug(f"API {request.method} {path} → {response.status_code} ({elapsed_ms:.0f}ms)")
    return response


# ─── 引入路由 ───
from .api import router as api_router
app.include_router(api_router)

# ─── 静态文件 ───
if os.path.exists(STATIC_DIR):
    # 把 css/js/pages/modals 等子目录挂在 /static
    app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    # 把 chart.min.js 等放在根目录
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="root-static")
