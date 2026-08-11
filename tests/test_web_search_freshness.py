"""web_search 时间过滤测试 — 用 httpx mock 验证"""
import sys
import os
import unittest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import web_search


def _make_result(title, published_date="", days_ago=None):
    r = {"title": title, "url": "http://x.com", "content": "内容",
         "engines": ["google"]}
    if days_ago is not None:
        dt = datetime.utcnow() - timedelta(days=days_ago)
        r["publishedDate"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
    elif published_date:
        r["publishedDate"] = published_date
    return r


def _patch_client(mock_response):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)
    return patch("httpx.AsyncClient", return_value=mock_client)


class TestWebSearchFreshness(unittest.TestCase):

    def test_old_results_filtered_by_max_age_days(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                _make_result("今天的新闻", days_ago=1),
                _make_result("旧闻", days_ago=30),
                _make_result("没日期的"),
            ]
        }
        with _patch_client(mock_response):
            results = asyncio.run(web_search.search("test", max_results=5, max_age_days=7))
        titles = [r["title"] for r in results]
        self.assertIn("今天的新闻", titles)
        self.assertNotIn("旧闻", titles, "30 天前的结果应被 max_age_days=7 过滤掉")

    def test_no_date_kept(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [_make_result("无日期结果")]}
        with _patch_client(mock_response):
            results = asyncio.run(web_search.search("test", max_age_days=7))
        self.assertEqual(len(results), 1)

    def test_published_field_in_result(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [_make_result("有日期", days_ago=2)]}
        with _patch_client(mock_response):
            results = asyncio.run(web_search.search("test"))
        self.assertIn("published", results[0])
        self.assertTrue(len(results[0]["published"]) > 0)

    def test_search_recent_helper(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [_make_result("新", days_ago=1)]}
        with _patch_client(mock_response):
            results = asyncio.run(web_search.search_recent("test", max_age_days=7))
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
