"""
网页浏览模块 — 让 AI 能真正"打开网页并点击链接"
================================================
web_search 只给摘要；这个模块让 AI 像真人一样打开一个网页，看到正文和
页内可点击的链接清单，再用 web_click"点击"编号链接往深处翻，web_back 返回。

设计要点:
  - 纯 stdlib html.parser 解析，零新依赖（httpx 由 anthropic SDK 自带）
  - 每个会话（群+发言人）一个浏览栈：web_open 开新页、web_click 压栈、
    web_back 弹栈回退
  - SSRF 防护：只允许公网 http(s)，域名解析后拒绝内网/回环/链路本地地址，
    重定向逐跳复检——防止 AI 被诱导访问 NapCat/面板等本机服务
  - 输出体量控制：正文节选 + 编号链接清单，整体低于 chat 侧裁剪阈值，
    工具结果进上下文后不会再被腰斩
"""
import asyncio
import ipaddress
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("qqbot")

# 整行长得像标签的文本（站点在正文里放转义的 <style>/<div> 标记，还原后混进来）
_TAG_SOUP_RE = re.compile(r"^<[a-zA-Z!/]")

# ─── 抓取参数 ───
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FETCH_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
MAX_REDIRECTS = 4
MAX_BODY_BYTES = 3 * 1024 * 1024   # 页面体积上限
BODY_CHARS = 2200                  # 正文节选上限（字符）
MAX_LINKS = 25                     # 链接清单上限
LINK_TEXT_CHARS = 40               # 单个链接锚文本上限

# ─── 会话参数 ───
SESSION_TTL = 30 * 60              # 30 分钟没动静的浏览栈过期
FETCH_COOLDOWN = 2.0               # 同一会话两次抓取最小间隔（防连环刷）
MAX_STACK = 6                      # 浏览栈深度上限
MAX_SESSIONS = 200


# ═══════════════════════════════════════════════════════════
#  HTML 解析（stdlib）
# ═══════════════════════════════════════════════════════════

class _PageExtractor(HTMLParser):
    """提取标题 / 正文文本 / 页内链接。

    script/style 等内容整体剔除；nav/header/footer/aside 只剔除正文文本、
    保留其中的链接（菜单恰恰是 AI 可能想点的东西）。
    """

    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
    _CHROME = {"nav", "header", "footer", "aside"}
    _BLOCK = {"p", "div", "section", "article", "main", "li", "tr", "ul", "ol",
              "table", "blockquote", "pre", "hr", "br", "h1", "h2", "h3", "h4",
              "h5", "h6", "form", "figure", "figcaption", "dl", "dt", "dd", "option"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self.title = ""
        self.meta_desc = ""
        self.links: list[tuple[str, str]] = []   # (锚文本, 绝对URL)
        self._in_title = 0
        self._skip = 0
        self._chrome = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._buf: list[str] = []
        self.lines: list[str] = []
        self._seen_urls: set[str] = set()

    # -- 标签 --
    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title += 1
            return
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in self._CHROME:
            self._chrome += 1
        if self._skip:
            return
        if tag == "meta":
            d = dict(attrs)
            if (d.get("name") or "").lower() == "description" and not self.meta_desc:
                self.meta_desc = d.get("content") or ""
        elif tag == "a" and self._link_href is None:
            href = (dict(attrs).get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                absu = urljoin(self._base, href)
                if absu.startswith(("http://", "https://")):
                    self._link_href = absu
                    self._link_text = []
        elif tag in self._BLOCK:
            self._flush_line()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = max(0, self._in_title - 1)
            return
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in self._CHROME:
            self._chrome = max(0, self._chrome - 1)
        if tag == "a" and self._link_href is not None:
            text = " ".join("".join(self._link_text).split())[:LINK_TEXT_CHARS]
            if not text:
                # 图片链接/空锚点：用路径尾段当文字，保证编号清单可读
                tail = urlparse(self._link_href).path.rstrip("/").rsplit("/", 1)[-1]
                text = tail[:LINK_TEXT_CHARS] or self._link_href
            if self._link_href not in self._seen_urls:
                self._seen_urls.add(self._link_href)
                self.links.append((text, self._link_href))
            self._link_href = None
            self._link_text = []
        if tag in self._BLOCK:
            self._flush_line()

    # -- 文本 --
    def handle_data(self, data):
        if not data:
            return
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._link_href is not None:
            self._link_text.append(data)
        if not self._chrome:
            self._buf.append(data)

    def _flush_line(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.lines.append(text)

    def close(self):
        super().close()
        self._flush_line()
        self.title = " ".join(self.title.split())[:120]


def _parse_page(url: str, html: str) -> dict:
    """解析 HTML → {title, text, links}。文本超长时按行边界节选。"""
    ex = _PageExtractor(url)
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        pass  # 烂 HTML 也要把已解析出的部分用起来
    text = "\n".join(
        ln for ln in "\n".join(ex.lines).split("\n") if not _TAG_SOUP_RE.match(ln)
    )
    if len(text) < 120 and ex.meta_desc:
        text = (ex.meta_desc + "\n" + text).strip()
    if len(text) > BODY_CHARS:
        cut = text[:BODY_CHARS]
        nl = cut.rfind("\n")
        cut = cut[:nl if nl > BODY_CHARS // 2 else BODY_CHARS]
        text = cut + "\n…（正文过长，已节选）"
    host = urlparse(url).netloc
    return {"title": ex.title or host, "text": text, "links": ex.links[:MAX_LINKS]}


# ═══════════════════════════════════════════════════════════
#  抓取（SSRF 防护 + 手动重定向）
# ═══════════════════════════════════════════════════════════

def _url_allowed(url: str) -> bool:
    """只允许公网 http(s)。解析域名逐个 IP 检查，拦内网/回环/元数据地址。"""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


async def _fetch(url: str) -> tuple[str, str, str]:
    """抓取页面，返回 (最终URL, 文本, content-type)。重定向逐跳复检 SSRF。"""
    current = url
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT, follow_redirects=False,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if not await asyncio.to_thread(_url_allowed, current):
                raise ValueError("不允许访问的地址（仅限公网 http/https 链接）")
            resp = await client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if not loc:
                    break
                current = urljoin(current, loc)
                continue
            resp.raise_for_status()
            if len(resp.content) > MAX_BODY_BYTES:
                raise ValueError("页面太大，看不了")
            return str(resp.url), resp.text, resp.headers.get("content-type", "").lower()
    raise ValueError("重定向次数过多")


class _Page:
    __slots__ = ("url", "title", "text", "links")

    def __init__(self, url: str, title: str, text: str, links: list):
        self.url = url
        self.title = title
        self.text = text
        self.links = links   # [(锚文本, 绝对URL)]


async def _open(url: str) -> _Page:
    final_url, body, ctype = await _fetch(url)
    if "json" in ctype:
        return _Page(final_url, "JSON 数据", body[:BODY_CHARS], [])
    if "text/plain" in ctype:
        return _Page(final_url, urlparse(final_url).netloc, body[:BODY_CHARS], [])
    if "html" in ctype or "xml" in ctype or not ctype:
        info = await asyncio.to_thread(_parse_page, final_url, body)
        return _Page(final_url, info["title"], info["text"], info["links"])
    raise ValueError(f"该链接不是网页（{ctype.split(';')[0]}），没法浏览")


# ═══════════════════════════════════════════════════════════
#  浏览会话（群+发言人 各自一个栈）
# ═══════════════════════════════════════════════════════════

class _Session:
    __slots__ = ("stack", "last_fetch", "ts")

    def __init__(self):
        self.stack: list[_Page] = []
        self.last_fetch = 0.0
        self.ts = time.time()


_sessions: dict[str, _Session] = {}


def _get_session(key: str) -> _Session:
    now = time.time()
    expired = [k for k, s in _sessions.items() if now - s.ts > SESSION_TTL]
    for k in expired:
        del _sessions[k]
    if len(_sessions) > MAX_SESSIONS:
        oldest = sorted(_sessions.items(), key=lambda kv: kv[1].ts)
        for k, _ in oldest[: len(_sessions) - MAX_SESSIONS]:
            del _sessions[k]
    s = _sessions.get(key)
    if s is None:
        s = _sessions[key] = _Session()
    s.ts = now
    return s


def current_url(group_id: int, caller_qq: int) -> str:
    """当前浏览栈栈顶页面的 URL（没有会话/栈空返回空串）。web_screenshot 用。"""
    sess = _sessions.get(f"{int(group_id or 0)}:{int(caller_qq or 0)}")
    if sess and sess.stack:
        return sess.stack[-1].url
    return ""


def _format_page(page: _Page, note: str = "") -> str:
    """渲染成 AI 可读的页面文本。控制在 ~3.5K 字符内，低于裁剪阈值。"""
    lines = []
    if note:
        lines.append(note)
    lines.append(f"【网页】{page.title or '(无标题)'}")
    lines.append(page.url)
    lines.append("")
    lines.append(page.text or "（正文没有可提取的文字，可能是纯图片或JS渲染的页面）")
    if page.links:
        lines.append("")
        lines.append("—— 页内链接（要点的告诉我编号，或直接调 web_click）——")
        for i, (txt, _href) in enumerate(page.links, 1):
            lines.append(f"[{i}] {txt}")
    return "\n".join(lines)


async def handle(tool_name: str, tool_input: dict,
                 group_id: int, caller_qq: int) -> str:
    """ai_tools 的执行入口：web_open / web_click / web_back。"""
    key = f"{int(group_id or 0)}:{int(caller_qq or 0)}"
    sess = _get_session(key)
    # 同一会话连续抓取限速：防止模型连环翻页刷站点（回退不抓取不受限）
    if tool_name in ("web_open", "web_click"):
        now = time.time()
        if now - sess.last_fetch < FETCH_COOLDOWN:
            return "⨯ 翻页太快了，先消化一下当前页面再点下一个"
        sess.last_fetch = now
    try:
        if tool_name == "web_open":
            url = str(tool_input.get("url", "")).strip()
            if not url:
                return "⨯ 需要 url 参数"
            page = await _open(url)
            sess.stack = [page]   # 开新页 = 开始一次新浏览
            return _format_page(page)

        if tool_name == "web_click":
            if not sess.stack:
                return "还没打开任何网页，先用 web_open 打开一个链接"
            try:
                n = int(tool_input.get("n", 0))
            except (TypeError, ValueError):
                return "⨯ n 要是数字（链接清单里的编号）"
            cur = sess.stack[-1]
            if not (1 <= n <= len(cur.links)):
                if not cur.links:
                    return "当前页面没有可点击的链接"
                return f"⨯ 编号超出范围：当前页只有 1-{len(cur.links)} 号链接"
            _, href = cur.links[n - 1]
            page = await _open(href)
            sess.stack.append(page)
            if len(sess.stack) > MAX_STACK:
                sess.stack.pop(0)
            return _format_page(page)

        if tool_name == "web_back":
            if len(sess.stack) < 2:
                return "没有上一页了"
            sess.stack.pop()
            return _format_page(sess.stack[-1], note="（已返回上一页，内容来自缓存）")

        return f"未知工具: {tool_name}"
    except ValueError as e:
        return f"⨯ 打不开: {e}"
    except httpx.HTTPStatusError as e:
        code = getattr(e.response, "status_code", "?")
        logger.info(f"web_browse HTTP {code}: {str(getattr(e.request, 'url', ''))[:120]}")
        return f"⨯ 网站返回了 {code}，打不开这个页面"
    except (httpx.TimeoutException, OSError):
        return "⨯ 网络超时或连不上这个网站"
    except Exception as e:
        logger.debug(f"web_browse 异常: {e}")
        return f"⨯ 打不开: {str(e)[:60]}"
