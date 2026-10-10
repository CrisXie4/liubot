"""
联网搜索模块 — SearXNG 集成
============================
为 AI 聊天提供实时联网搜索能力。
AI 可通过 tool_use 调用 web_search 获取最新信息。
"""
import logging
import os
import re
from typing import Optional
from urllib.parse import quote

import httpx

from .config import config

logger = logging.getLogger("qqbot")

# SearXNG 地址（可用 .env 的 SEARXNG_URL 覆盖；服务器 IP 变动时改 env 不用改代码）
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8080")

# ─── Tool Definition（Anthropic tool_use 格式）───
SEARCH_TOOL = {
    "name": "web_search",
    "description": "搜索互联网获取最新信息。当用户询问当前新闻、实时数据、最新事件、或者你需要核实最新信息时使用。返回相关的搜索结果摘要。",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，用简洁的语言描述要查找的内容",
            },
        },
        "required": ["query"],
    },
}


async def search(query: str, max_results: int = 5,
                 time_range: str = None, max_age_days: int = None) -> list[dict]:
    """
    调用 SearXNG 搜索。优先 JSON API，回退 HTML 解析。

    Args:
        time_range: SearXNG 时间过滤，取值 day/week/month/year/None
        max_age_days: 二次过滤，丢弃 publishedDate 早于 N 天前的结果。
                      None = 不做二次过滤。
    Returns:
        [{"title": "...", "url": "...", "content": "...", "engine": "...",
          "published": "YYYY-MM-DD"}, ...]
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"q": query, "format": "json", "language": "zh-CN", "categories": "general"}
            if time_range:
                params["time_range"] = time_range

            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params=params,
                headers={"User-Agent": "QQBot/5.0"},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    results = data.get("results", [])[:max_results]
                    formatted = _format_results(results)
                    if max_age_days is not None:
                        formatted = _filter_by_age(formatted, max_age_days)
                    if not formatted:
                        # 0 结果时把引擎异常暴露出来（上游 CAPTCHA/限流一眼可查）
                        unresp = data.get("unresponsive_engines") or []
                        if unresp:
                            logger.warning(
                                f"SearXNG 0 结果，上游引擎异常: {unresp}")
                    logger.info(f"SearXNG JSON: '{query}' → {len(formatted)} 条")
                    return formatted
                except Exception:
                    pass

            params_html = {"q": query, "language": "zh-CN", "categories": "general"}
            if time_range:
                params_html["time_range"] = time_range
            resp2 = await client.get(
                f"{SEARXNG_URL}/search",
                params=params_html,
                headers={"User-Agent": "QQBot/5.0", "Accept": "text/html"},
            )
            if resp2.status_code == 200:
                results = _parse_html_results(resp2.text, max_results)
                logger.info(f"SearXNG HTML: '{query}' → {len(results)} 条")
                return results

            logger.warning(f"SearXNG 搜索失败 HTTP {resp.status_code}: {query}")
            return []
    except httpx.TimeoutException:
        logger.warning(f"SearXNG 搜索超时: {query}")
        return []
    except Exception as e:
        logger.warning(f"SearXNG 搜索异常: {e}")
        return []


def _filter_by_age(results: list[dict], max_age_days: int) -> list[dict]:
    """丢弃 publishedDate 早于 max_age_days 的结果。无日期的保留。"""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    kept = []
    for r in results:
        pub = r.get("published", "")
        if not pub:
            kept.append(r)
            continue
        try:
            pub_dt = datetime.fromisoformat(pub[:10])
            if pub_dt >= cutoff:
                kept.append(r)
            else:
                logger.debug(f"旧闻过滤: «{r.get('title','')[:30]}» published={pub[:10]}")
        except Exception:
            kept.append(r)
    return kept


def _format_results(results: list[dict]) -> list[dict]:
    """格式化 JSON API 结果"""
    formatted = []
    for r in results:
        pub_raw = r.get("publishedDate", "") or ""
        published = ""
        if pub_raw:
            try:
                published = pub_raw[:10]
            except Exception:
                published = ""
        formatted.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": (r.get("content", "") or r.get("snippet", ""))[:300],
            "engine": ", ".join(r.get("engines", [])) if isinstance(r.get("engines"), list) else r.get("engine", ""),
            "published": published,
        })
    return formatted


def _parse_html_results(html: str, max_results: int) -> list[dict]:
    """从 SearXNG HTML 页面提取搜索结果"""
    import re
    results = []

    # 匹配 <article class="result">...</article>
    articles = re.findall(r'<article[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</article>', html, re.DOTALL)

    for art in articles[:max_results]:
        title = ""
        url = ""
        content = ""

        # 提取所有 <a href="..."> 链接
        links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', art, re.DOTALL)

        # 找 h3 里的标题（主链接）
        h3_m = re.search(r'<h\d[^>]*>(.*?)</h\d>', art, re.DOTALL)
        if h3_m:
            title = re.sub(r'<[^>]+>', '', h3_m.group(1)).strip()
            # 找到对应的 url
            h3_link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>', h3_m.group(1))
            if h3_link:
                url = h3_link.group(1)

        # 如果没有 h3，用第一个有意义的链接
        if not title and links:
            for l_url, l_text in links:
                l_text = re.sub(r'<[^>]+>', '', l_text).strip()
                if l_text and len(l_text) > 5 and not l_text.startswith("http"):
                    title = l_text
                    url = l_url
                    break

        # 提取内容摘要 <p>
        content_m = re.search(r'<p[^>]*class="[^"]*(?:content|snippet|result-content)[^"]*"[^>]*>(.*?)</p>', art, re.DOTALL)
        if not content_m:
            content_m = re.search(r'<p[^>]*>(.*?)</p>', art, re.DOTALL)
        if content_m:
            content = re.sub(r'<[^>]+>', '', content_m.group(1)).strip()
            content = re.sub(r'\s+', ' ', content)[:300]

        if title:
            results.append({"title": title, "url": url, "content": content, "engine": "searxng"})

    return results


def format_search_results(results: list[dict]) -> str:
    """将搜索结果格式化为 AI 可读的文本"""
    if not results:
        return "（未找到相关搜索结果）"

    lines = ["【联网搜索结果】"]
    for i, r in enumerate(results, 1):
        pub = r.get("published", "")
        pub_str = f" (发布:{pub})" if pub else ""
        lines.append(f"{i}. {r['title']}{pub_str}")
        lines.append(f"   链接: {r['url']}")
        lines.append(f"   摘要: {r['content']}")
        lines.append("")
    return "\n".join(lines)


async def search_and_format(query: str, max_results: int = 5) -> str:
    """搜索并返回格式化文本"""
    results = await search(query, max_results)
    return format_search_results(results)


async def search_recent(query: str, max_age_days: int = 7, max_results: int = 5) -> list[dict]:
    """便捷封装：结果按 max_age_days 做新鲜度过滤。

    注意：不要传 time_range——实测 SearXNG 的 bing 引擎对中文查询 +
    time_range=week 恒返回 0 条（2026-09-06 排障结论）；
    新鲜度由 _filter_by_age 按 publishedDate 二次过滤兜底。

    韧性：白天 SearXNG 上游引擎被限流时经常 0 结果——此时换措辞
    （去掉尾部时间限定词用 topic 裸词，或给裸词补当月月份词）重试一次，
    仍 0 才返回空。签名与返回形状不变，调用方零改动。
    """
    results = await search(query, max_results=max_results,
                           max_age_days=max_age_days)
    if results:
        return results

    # 0 结果 → 换措辞重试一次
    bare = _strip_time_qualifier(query)
    if not bare:
        # 整个查询都是限定词，没法换措辞
        return results
    retry_query = bare if bare != (query or "").strip() else f"{bare} {_month_qualifier()}"
    logger.info(f"SearXNG 0 结果，换措辞重试: '{query}' → '{retry_query}'")
    return await search(retry_query, max_results=max_results,
                        max_age_days=max_age_days)


# 查询尾部可剥的时间限定词（含动态月份词，如"10月"；与 ai_brain 的 angle 池同族）
_TIME_QUALIFIER_RE = re.compile(r"(最新|今天|本周|最近|今年|去年|昨日|上周|上月|20\d{2}|\d{1,2}月)\s*$")


def _strip_time_qualifier(query: str) -> str:
    """去掉查询尾部的"最新/本周/10月"之类时间限定词，得到 topic 裸词。"""
    q = (query or "").strip()
    prev = None
    while prev != q and q:
        prev = q
        q = _TIME_QUALIFIER_RE.sub("", q).strip()
    return q


def _month_qualifier() -> str:
    """当前月份限定词（如"10月"），用权威上海时间动态生成。"""
    from . import timeutil
    return f"{timeutil.now_shanghai().month}月"


# ─── 搜索结果缓存 ───
_cache: dict[str, tuple[float, str]] = {}
import time as _time
CACHE_TTL = 300  # 5 分钟


async def search_cached(query: str, max_results: int = 5) -> str:
    """带缓存的搜索（相同 query 5 分钟内不重复请求）"""
    key = query.strip().lower()
    now = _time.time()
    if key in _cache:
        ts, result = _cache[key]
        if now - ts < CACHE_TTL:
            return result
    result = await search_and_format(query, max_results)
    _cache[key] = (now, result)
    # 清理旧缓存
    expired = [k for k, (t, _) in _cache.items() if now - t > CACHE_TTL * 2]
    for k in expired:
        del _cache[k]
    return result


# ─── 图片搜索 ───

async def search_images(query: str, max_results: int = 5) -> list[dict]:
    """
    通过 SearXNG 搜索图片。
    Returns:
        [{"title": "...", "url": "...", "img_src": "...", "thumbnail_src": "...", "source": "..."}, ...]
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q": query,
                    "format": "json",
                    "language": "zh-CN",
                    "categories": "images",
                },
                headers={"User-Agent": "QQBot/5.0"},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    results = data.get("results", [])[:max_results]
                    logger.info(f"SearXNG Images: '{query}' → {len(results)} 张")
                    formatted = []
                    for r in results:
                        formatted.append({
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "img_src": r.get("img_src", ""),
                            "thumbnail_src": r.get("thumbnail_src", ""),
                            "source": r.get("source", "") or r.get("engine", ""),
                        })
                    return formatted
                except Exception:
                    pass
            logger.debug(f"图片搜索失败 HTTP {resp.status_code}: {query}")
            return []
    except Exception as e:
        logger.debug(f"图片搜索异常: {e}")
        return []
