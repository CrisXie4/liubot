"""
网页截图 — 让 AI 打开的网站能"发张图给群友看看"
================================================
服务器上没有浏览器引擎（playwright+chromium 太重），改用公共截图服务出图：
  1. thum.io — 同步渲染，GET 即返回截图
  2. mShots  — WordPress 截图服务，异步生成：未就绪时返回小占位图，
               需要间隔重试

产出图片字节由 ai_tools 转 base64:// CQ 码直接发群——NapCat 跑在 docker
容器里看不见宿主机文件路径，base64 不经文件系统最稳。

防滥用：同群 5 分钟最多 3 张；SSRF 口径与 web_browse 一致（只截公网
http/https——截图服务在它自己的网络里访问目标站，本来也碰不到我们内网）。
"""
import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from .web_browse import UA, _url_allowed

logger = logging.getLogger("qqbot")

SHOT_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
MIN_IMAGE_BYTES = 4000          # 小于这个基本是"生成中"占位图/错误页

_THUM = "https://image.thum.io/get/width/1280/crop/800/noanimate/{url}"
_MSHOTS = "https://s0.wp.com/mshots/v1/{encoded}?w=1280&h=800"
_MSHOTS_WAITS = (2.5, 4.0)      # mShots 异步生成：占位图后的两轮重试间隔

# 同群限速：5 分钟窗口内最多 3 张
RATE_WINDOW = 300.0
RATE_MAX = 3
_rate: dict[int, list] = {}


def check_rate(group_id: int) -> str:
    """返回空串=放行；非空=拒绝理由（直接回给 AI）。"""
    now = time.time()
    dq = _rate.setdefault(int(group_id or 0), [])
    while dq and now - dq[0] > RATE_WINDOW:
        dq.pop(0)
    if len(dq) >= RATE_MAX:
        return f"⨯ 截图太频繁了（{int(RATE_WINDOW//60)} 分钟最多 {RATE_MAX} 张），先聊点别的"
    dq.append(now)
    return ""


async def _get_image(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "image" not in ctype and "octet-stream" not in ctype:
        raise ValueError(f"返回的不是图片({ctype.split(';')[0]})")
    return resp.content


def _compress(data: bytes) -> bytes:
    """转 JPEG 压体积——PNG 截图动辄 2MB+，base64 塞 CQ 码太肥。"""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, "JPEG", quality=82)
        out = buf.getvalue()
        return out if 0 < len(out) < len(data) else data
    except Exception:
        return data


async def capture(url: str) -> bytes | None:
    """抓网页截图，返回图片字节；拿不到图返回 None。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    if not await asyncio.to_thread(_url_allowed, url):
        return None

    async with httpx.AsyncClient(
        timeout=SHOT_TIMEOUT, follow_redirects=True,
        headers={"User-Agent": UA},
    ) as client:
        # 1) thum.io：同步渲染，一把出图
        try:
            data = await _get_image(client, _THUM.format(url=url))
            if len(data) >= MIN_IMAGE_BYTES:
                return _compress(data)
            logger.debug(f"thum.io 出图过小({len(data)}B) 视为失败")
        except Exception as e:
            logger.debug(f"thum.io 截图失败: {str(e)[:80]}")

        # 2) mShots：异步生成，未就绪时是小占位图，按间隔重试
        try:
            u = _MSHOTS.format(encoded=quote(url, safe=""))
            data = await _get_image(client, u)
            for wait in _MSHOTS_WAITS:
                if len(data) >= MIN_IMAGE_BYTES:
                    return _compress(data)
                await asyncio.sleep(wait)
                data = await _get_image(client, u)
            if len(data) >= MIN_IMAGE_BYTES:
                return _compress(data)
        except Exception as e:
            logger.debug(f"mShots 截图失败: {str(e)[:80]}")

    logger.info(f"网页截图失败(两个服务都没出图): {url[:100]}")
    return None
