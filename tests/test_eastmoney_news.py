"""Tests for the East Money (东方财富) news data vendor.

Covers:
  - A-share symbol detection and conversion
  - Announcement fetching and date filtering
  - News fetching with JSONP wrapper stripping
  - Global news query
  - Look-ahead-safe date window filtering
  - Non-A-share symbol rejection (router fallback)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from tradingagents.dataflows.eastmoney_news import (
    _fetch_announcements,
    _fetch_news,
    _ts_code_to_eastmoney,
    get_global_news,
    get_news,
    is_a_share,
)


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSymbolHelpers:
    def test_is_a_share_sh(self):
        assert is_a_share("300308.SZ") is True

    def test_is_a_share_sz(self):
        assert is_a_share("688256.SH") is True

    def test_is_a_share_bj(self):
        assert is_a_share("430047.BJ") is True

    def test_not_a_share_us(self):
        assert is_a_share("AAPL") is False

    def test_not_a_share_yahoo(self):
        assert is_a_share("000001.SS") is False

    def test_ts_code_to_eastmoney_sh(self):
        assert _ts_code_to_eastmoney("688256.SH") == ("1", "688256")

    def test_ts_code_to_eastmoney_sz(self):
        assert _ts_code_to_eastmoney("300308.SZ") == ("0", "300308")

    def test_ts_code_to_eastmoney_bj(self):
        assert _ts_code_to_eastmoney("430047.BJ") == ("2", "430047")


# ---------------------------------------------------------------------------
# Announcement fetching
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnnouncements:
    @mock.patch("tradingagents.dataflows.eastmoney_news.requests.get")
    def test_fetch_announcements_parses_response(self, mock_get):
        """Announcement API returns string timestamps (Beijing time)."""
        mock_resp = mock.Mock()
        mock_resp.json.return_value = {
            "data": {
                "list": [
                    {
                        "title": "关于回购股份的公告",
                        "notice_date": "2026-08-15 00:00:00",
                        "art_code": "123456",
                    },
                    {
                        "title": "2026年半年度报告",
                        "notice_date": "2026-08-14 00:00:00",
                        "art_code": "789012",
                    },
                ]
            }
        }
        mock_resp.raise_for_status = mock.Mock()
        mock_get.return_value = mock_resp

        articles = _fetch_announcements("300308.SZ")

        assert len(articles) == 2
        assert articles[0]["title"] == "关于回购股份的公告"
        assert articles[0]["type"] == "announcement"
        assert "eastmoney.com" in articles[0]["url"]
        assert articles[0]["pub_date"] is not None
        # Verify Beijing time conversion (2026-08-15 00:00 CST = 2026-08-14 16:00 UTC)
        assert articles[0]["pub_date"].hour == 16

    @mock.patch("tradingagents.dataflows.eastmoney_news.requests.get")
    def test_fetch_announcements_empty(self, mock_get):
        mock_resp = mock.Mock()
        mock_resp.json.return_value = {"data": {"list": []}}
        mock_resp.raise_for_status = mock.Mock()
        mock_get.return_value = mock_resp

        articles = _fetch_announcements("300308.SZ")
        assert articles == []

    @mock.patch("tradingagents.dataflows.eastmoney_news.requests.get")
    def test_fetch_announcements_network_error(self, mock_get):
        import requests as req

        mock_get.side_effect = req.ConnectionError("timeout")
        articles = _fetch_announcements("300308.SZ")
        assert articles == []


# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNewsFetch:
    @mock.patch("os.getenv", return_value="fake_token")
    def test_fetch_news_via_tushare(self, mock_env):
        """News fetching uses Tushare report_rc as secondary source."""
        import pandas as pd

        # Mock the tushare module that gets imported inside _fetch_news
        mock_ts_module = mock.Mock()
        mock_pro = mock.Mock()
        mock_ts_module.pro_api.return_value = mock_pro
        mock_pro.report_rc.return_value = pd.DataFrame([
            {
                "title": "中际旭创深度研究",
                "ann_date": "20260815",
                "org_name": "中信证券",
                "rating": "买入",
            }
        ])

        # The function does `import tushare as ts_api` inside the function body;
        # we patch __import__ to return our mock.
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *a, **k):
            if name == "tushare":
                return mock_ts_module
            return real_import(name, *a, **k)
        with mock.patch("builtins.__import__", side_effect=fake_import):
            articles = _fetch_news("300308.SZ")

        assert len(articles) == 1
        assert articles[0]["title"] == "中际旭创深度研究"
        assert articles[0]["type"] == "research_report"
        assert articles[0]["pub_date"] is not None
        assert "中信证券" in articles[0]["summary"]

    @mock.patch("os.getenv", return_value=None)
    def test_fetch_news_no_token(self, mock_env):
        """Without Tushare token, news fetching returns empty list."""
        articles = _fetch_news("300308.SZ")
        assert articles == []


# ---------------------------------------------------------------------------
# Public interface: get_news()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGetNews:
    def test_non_a_share_rejected(self):
        """Non-A-share tickers raise NoMarketDataError so the router can fall back."""
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError):
            get_news("AAPL", "2024-01-01", "2024-08-15")

    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_news", return_value=[])
    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_announcements")
    def test_get_news_formats_output(self, mock_anno, mock_news):
        mock_anno.return_value = [
            {
                "title": "回购股份公告",
                "pub_date": datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
                "url": "https://data.eastmoney.com/notices/detail/300308/123.html",
                "summary": "",
                "type": "announcement",
            }
        ]

        result = get_news("300308.SZ", "2026-08-01", "2026-08-15")

        assert "300308.SZ" in result
        assert "回购股份公告" in result
        assert "东方财富公告" in result

    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_news", return_value=[])
    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_announcements")
    def test_get_news_filters_by_date(self, mock_anno, mock_news):
        """Articles outside the date window are excluded (look-ahead safe)."""
        mock_anno.return_value = [
            {
                "title": "Old announcement",
                "pub_date": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
                "url": "",
                "summary": "",
                "type": "announcement",
            },
            {
                "title": "In-window announcement",
                "pub_date": datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
                "url": "",
                "summary": "",
                "type": "announcement",
            },
        ]

        result = get_news("300308.SZ", "2026-08-01", "2026-08-15")

        assert "In-window announcement" in result
        assert "Old announcement" not in result

    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_news", return_value=[])
    @mock.patch("tradingagents.dataflows.eastmoney_news._fetch_announcements", return_value=[])
    def test_get_news_no_data(self, mock_anno, mock_news):
        result = get_news("300308.SZ", "2026-01-01", "2026-08-15")
        assert "No news found" in result


# ---------------------------------------------------------------------------
# Public interface: get_global_news()
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGlobalNews:
    @mock.patch("tradingagents.dataflows.eastmoney_news.requests.get")
    def test_global_news_returns_articles(self, mock_get):
        mock_resp = mock.Mock()
        mock_resp.json.return_value = {
            "data": {
                "list": [
                    {
                        "title": "央行宣布降息",
                        "notice_date": "2026-09-14 00:00:00",
                        "art_code": "123",
                    }
                ]
            }
        }
        mock_resp.raise_for_status = mock.Mock()
        mock_get.return_value = mock_resp

        result = get_global_news("2026-09-15", look_back_days=7, limit=5)

        assert "央行宣布降息" in result
        assert "东方财富" in result

    @mock.patch("tradingagents.dataflows.eastmoney_news.requests.get")
    def test_global_news_all_fail(self, mock_get):
        import requests as req

        mock_get.side_effect = req.ConnectionError("timeout")
        result = get_global_news("2026-09-15", look_back_days=7, limit=5)
        assert "No Chinese market news" in result


# ---------------------------------------------------------------------------
# Integration: router registration
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRouterRegistration:
    def test_eastmoney_in_vendor_list(self):
        from tradingagents.dataflows.interface import VENDOR_LIST

        assert "eastmoney" in VENDOR_LIST

    def test_get_news_has_eastmoney(self):
        from tradingagents.dataflows.interface import VENDOR_METHODS

        assert "eastmoney" in VENDOR_METHODS["get_news"]

    def test_get_global_news_has_eastmoney(self):
        from tradingagents.dataflows.interface import VENDOR_METHODS

        assert "eastmoney" in VENDOR_METHODS["get_global_news"]
