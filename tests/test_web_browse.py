"""web_browse 单元测试 — 解析 / SSRF 防护 / 浏览会话前进回退（全部离线，不联网）"""
import asyncio

import pytest

from bot import web_browse


HTML = """
<html><head><title>测试页面标题</title>
<meta name="description" content="这是meta描述">
<script>var dirty = "脚本领里的脏数据不应该出现";</script>
<style>.a { color: red; }</style>
</head>
<body>
<nav><a href="/menu">菜单入口</a></nav>
<h1>正文大标题</h1>
<p>第一段正文内容，讲的是网页浏览。</p>
<p>第二段里有 <a href="/rel/path">相对链接</a> 和
   <a href="https://example.com/ext">外部链接</a>。</p>
<a href="https://example.com/ext">重复的外部链接</a>
<a href="javascript:void(0)">脚本假链接</a>
<a href="#anchor">页内锚点</a>
<a href="mailto:someone@example.com">邮件链接</a>
<img src="/pic.png"><a href="/pic-link"><img src="/x.png"></a>
<footer><p>页脚版权文字</p></footer>
</body></html>
"""


def test_parse_page_extracts_title_text_links():
    info = web_browse._parse_page("https://example.com/post/1", HTML)
    assert info["title"] == "测试页面标题"

    text = info["text"]
    assert "第一段正文内容" in text
    assert "脏数据不应该出现" not in text   # script 被剔除
    assert "color: red" not in text         # style 被剔除
    assert "页脚版权文字" not in text        # footer 正文被剔除

    urls = [u for _, u in info["links"]]
    # 相对链接 → 绝对
    assert "https://example.com/rel/path" in urls
    # 去重：同一 URL 只留第一个
    assert urls.count("https://example.com/ext") == 1
    # javascript / mailto / # 锚点 全部剔除
    assert all(not u.startswith(("javascript", "mailto:")) for u in urls)
    assert not any(u.endswith("#anchor") for u in urls)
    # nav 里的菜单链接保留（正文剔除但链接可点）
    assert "https://example.com/menu" in urls
    # 空锚文本的图片链接用路径尾段兜底
    texts = [t for t, _ in info["links"]]
    assert "pic-link" in texts


def test_parse_page_tolerates_broken_html():
    info = web_browse._parse_page(
        "https://example.com/", "<html><body><p>残缺页面没有闭合标签")
    assert "残缺页面" in info["text"]


# ─── SSRF 防护（字面 IP，getaddrinfo 离线可解析）───

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",                        # 非 http(s)
    "http://localhost:3000/api",                  # localhost
    "http://127.0.0.1:3001/",                     # 回环
    "http://192.168.1.10/admin",                  # 内网
    "http://10.0.0.5/",                           # 内网
    "http://172.16.0.9/",                         # 内网
    "http://169.254.169.254/latest/meta-data",    # 云元数据
    "http://[::1]/",                              # IPv6 回环
    "http://[fe80::1]/",                          # IPv6 链路本地
    "not a url at all",                           # 无 hostname
    "",
])
def test_ssrf_blocked(url):
    assert web_browse._url_allowed(url) is False


def test_ssrf_allows_public_ip():
    # 公网字面 IP 放行（不发起连接，只做地址校验）
    assert web_browse._url_allowed("http://93.184.216.34/page") is True


# ─── 浏览会话：打开 → 点击 → 返回 ───

def _mkpage(url, title, links):
    return web_browse._Page(url, title, f"{title}的正文内容", links)


@pytest.fixture
def fake_pages(monkeypatch):
    pages = {
        "https://a.example.com/": _mkpage(
            "https://a.example.com/", "A页", [("去B页", "https://b.example.com/")]),
        "https://b.example.com/": _mkpage(
            "https://b.example.com/", "B页", [("去C页", "https://c.example.com/")]),
        "https://c.example.com/": _mkpage(
            "https://c.example.com/", "C页", []),
    }

    async def fake_open(url):
        if url not in pages:
            raise ValueError(f"未知页面 {url}")
        return pages[url]

    monkeypatch.setattr(web_browse, "_open", fake_open)
    monkeypatch.setattr(web_browse, "FETCH_COOLDOWN", 0.0)  # 测试里关掉翻页冷却
    web_browse._sessions.clear()
    return pages


def test_session_open_click_back(fake_pages):
    r = asyncio.run(web_browse.handle(
        "web_open", {"url": "https://a.example.com/"}, 100, 200))
    assert "【网页】A页" in r
    assert "[1] 去B页" in r

    r = asyncio.run(web_browse.handle("web_click", {"n": 1}, 100, 200))
    assert "【网页】B页" in r

    r = asyncio.run(web_browse.handle("web_click", {"n": 1}, 100, 200))
    assert "【网页】C页" in r

    r = asyncio.run(web_browse.handle("web_back", {}, 100, 200))
    assert "【网页】B页" in r

    r = asyncio.run(web_browse.handle("web_back", {}, 100, 200))
    assert "【网页】A页" in r

    r = asyncio.run(web_browse.handle("web_back", {}, 100, 200))
    assert "没有上一页" in r


def test_session_click_out_of_range(fake_pages):
    asyncio.run(web_browse.handle(
        "web_open", {"url": "https://a.example.com/"}, 1, 2))
    r = asyncio.run(web_browse.handle("web_click", {"n": 99}, 1, 2))
    assert "编号超出范围" in r


def test_session_click_without_open(fake_pages):
    r = asyncio.run(web_browse.handle("web_click", {"n": 1}, 3, 4))
    assert "还没打开任何网页" in r


def test_sessions_are_isolated(fake_pages):
    asyncio.run(web_browse.handle(
        "web_open", {"url": "https://a.example.com/"}, 1, 2))
    # 另一个人/另一个群的会话是空的
    r = asyncio.run(web_browse.handle("web_click", {"n": 1}, 1, 999))
    assert "还没打开任何网页" in r


def test_fetch_cooldown_blocks_rapid_refetch(fake_pages, monkeypatch):
    """同一会话连环翻页要被冷却拦住；回退走缓存不受限"""
    monkeypatch.setattr(web_browse, "FETCH_COOLDOWN", 60.0)
    r1 = asyncio.run(web_browse.handle(
        "web_open", {"url": "https://a.example.com/"}, 7, 8))
    assert "【网页】A页" in r1
    r2 = asyncio.run(web_browse.handle("web_click", {"n": 1}, 7, 8))
    assert "翻页太快" in r2
    # 点击被拦 → 栈里仍只有 A 页，回退如实说没有上一页
    r3 = asyncio.run(web_browse.handle("web_back", {}, 7, 8))
    assert "没有上一页" in r3
