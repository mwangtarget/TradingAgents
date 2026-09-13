"""East Money (东方财富) news and announcement data vendor.

Provides Chinese A-share stock news via the East Money public API:
  - Per-stock announcements (公告) from ``np-anotice-stock.eastmoney.com``
  - Per-stock news (新闻) from ``search-api-web.eastmoney.com``

This vendor is keyless and free, making it the best option for A-share
news when Tushare's ``news`` interface is unavailable (requires ≥2000
credits). It follows the same interface contract as the yfinance and
Alpha Vantage news vendors: ``get_news(ticker, start_date, end_date)``
returns a formatted string, ``get_global_news(curr_date, ...)`` returns
macro/market headlines.

All timestamps are normalized to UTC for look-ahead-safe filtering via
``date_window.in_window``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from .date_window import in_window
from .errors import NoMarketDataError, VendorRateLimitError
from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

# East Money API endpoints
_ANNO_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_NEWS_API = "https://search-api-web.eastmoney.com/jsonp"

# Request defaults
_TIMEOUT = 15
_MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

# A-share ts_code patterns: 6 digits + .SH/.SZ/.BJ
_A_SHARE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)


def is_a_share(ticker: str) -> bool:
    """True when *ticker* looks like a Chinese A-share ts_code (e.g. 300308.SZ)."""
    return bool(_A_SHARE_RE.match(ticker.strip()))


def _ts_code_to_eastmoney(ts_code: str) -> tuple[str, str]:
    """Convert a Tushare ts_code to East Money's (market, code) pair.

    East Money uses numeric market codes:
      .SH -> 1 (Shanghai)
      .SZ -> 0 (Shenzhen)
      .BJ -> 2 (Beijing)
    """
    code, suffix = ts_code.strip().upper().split(".")
    market = {"SH": "1", "SZ": "0", "BJ": "2"}.get(suffix, "1")
    return market, code


# ---------------------------------------------------------------------------
# Announcement fetching (公告)
# ---------------------------------------------------------------------------

def _parse_eastmoney_date(date_val) -> datetime | None:
    """Parse East Money's various date formats into a UTC datetime.

    Handles:
      - String: "2026-09-11 00:00:00" or "2026-09-11"
      - Epoch ms: 1723718400000 (int)
      - Epoch sec: 1723718400 (int)
      - None → None
    """
    if date_val is None:
        return None
    # String format (most common in current API)
    if isinstance(date_val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S:%f", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_val.strip(), fmt)
                # East Money dates are Beijing time (UTC+8)
                return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
            except ValueError:
                continue
        return None
    # Numeric (epoch)
    try:
        ts_val = int(date_val)
        if ts_val > 1e12:  # milliseconds
            ts_val //= 1000
        return datetime.fromtimestamp(ts_val, tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None


def _fetch_announcements(ts_code: str, page: int = 1, page_size: int = 100) -> list[dict]:
    """Fetch one page of announcements for *ts_code* from East Money.

    Returns a list of article dicts with keys: title, pub_date, url.
    Raises ``NoMarketDataError`` if the API returns no items.
    """
    market, code = _ts_code_to_eastmoney(ts_code)
    params = {
        "sr": "-1",
        "page_size": str(page_size),
        "page_index": str(page),
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
        "f_node": market,
        "s_node": "0",
    }
    try:
        resp = requests.get(_ANNO_API, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("East Money announcement API error for %s: %s", ts_code, exc)
        return []

    if not data or "data" not in data:
        return []

    rows = data.get("data", {}).get("list", [])
    if not rows:
        return []

    articles = []
    for row in rows:
        title = row.get("title", "").strip()
        if not title:
            continue
        # notice_date is a string like "2026-09-11 00:00:00";
        # eiTime may also be present as "2026-09-11 19:56:39:000"
        pub_date = _parse_eastmoney_date(row.get("notice_date")) or _parse_eastmoney_date(row.get("eiTime"))

        # Build URL from art_code
        art_code = row.get("art_code", "")
        url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html" if art_code else ""

        articles.append({
            "title": title,
            "pub_date": pub_date,
            "url": url,
            "type": "announcement",
        })

    return articles


# ---------------------------------------------------------------------------
# News fetching (新闻)
# ---------------------------------------------------------------------------

def _fetch_news(ts_code: str, page: int = 1, page_size: int = 20) -> list[dict]:
    """Fetch news articles for *ts_code* via Tushare research reports.

    The East Money news search endpoints currently return HTML rather than
    JSON/JSONP. As a reliable secondary source, we use Tushare's ``report_rc``
    interface (research report ratings), which provides analyst coverage news.
    This requires a Tushare token but is already configured in the project's
    ``.env``.

    Returns a list of article dicts.
    """
    import os

    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TUSHARE_API_KEY")
    if not token:
        logger.debug("Tushare token not set; skipping research report news for %s", ts_code)
        return []

    try:
        import tushare as ts_api
    except ImportError:
        logger.debug("tushare not installed; skipping research report news for %s", ts_code)
        return []

    try:
        pro = ts_api.pro_api(token=token)
        # report_rc returns research report ratings — analyst coverage
        df = pro.report_rc(ts_code=ts_code, page_size=page_size)
    except Exception as exc:
        logger.debug("Tushare report_rc failed for %s: %s", ts_code, exc)
        return []

    if df is None or getattr(df, "empty", True):
        return []

    articles = []
    for _, row in df.iterrows():
        title = str(row.get("title", "")).strip()
        if not title:
            continue
        pub_date = None
        ann_date = row.get("ann_date")
        if ann_date:
            try:
                pub_date = datetime.strptime(str(ann_date), "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        org = row.get("org_name", "")
        rating = row.get("rating", "")
        summary_parts = []
        if org:
            summary_parts.append(f"机构: {org}")
        if rating:
            summary_parts.append(f"评级: {rating}")
        summary = " | ".join(summary_parts) if summary_parts else ""

        articles.append({
            "title": title,
            "pub_date": pub_date,
            "url": "",
            "summary": summary,
            "type": "research_report",
        })

    return articles


# ---------------------------------------------------------------------------
# Public vendor interface (matches yfinance/alpha_vantage news contract)
# ---------------------------------------------------------------------------

def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Retrieve news and announcements for an A-share stock.

    Args:
        ticker: Tushare ts_code (e.g., "300308.SZ") or any A-share code.
        start_date: Start date in yyyy-mm-dd format.
        end_date: End date in yyyy-mm-dd format.

    Returns:
        Formatted string containing news articles and announcements.
    """
    if not is_a_share(ticker):
        # Not an A-share ticker — let the router fall back to another vendor.
        raise NoMarketDataError(ticker, ticker, "not an A-share ts_code (expected format: 300308.SZ)")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Fetch announcements (primary source — most reliable)
    announcements = _fetch_announcements(ticker, page_size=100)

    # Fetch news (secondary — may fail due to JSONP instability)
    news = _fetch_news(ticker, page_size=20)

    all_articles = announcements + news

    if not all_articles:
        return f"No news found for {ticker} between {start_date} and {end_date}"

    # Filter by date window (look-ahead safe)
    filtered = []
    for article in all_articles:
        if in_window(article["pub_date"], start_dt, end_dt):
            filtered.append(article)

    if not filtered:
        return f"No news found for {ticker} between {start_date} and {end_date}"

    # Format output
    parts = [f"## {ticker} News & Announcements, from {start_date} to {end_date}:\n"]
    for a in filtered[:50]:  # Cap at 50 articles to bound token usage
        source_label = "公告" if a["type"] == "announcement" else "新闻"
        parts.append(f"### {a['title']} (source: 东方财富{source_label})\n")
        if a.get("summary"):
            parts.append(f"{a['summary']}\n")
        if a.get("url"):
            parts.append(f"Link: {a['url']}\n")
        parts.append("\n")

    return "".join(parts)


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 30) -> str:
    """Retrieve Chinese A-share market headlines via East Money announcements.

    Searches for market-wide announcements using key macro/policy keywords
    on the East Money announcement API. This complements FRED (US macro)
    and yfinance (global news) with China-specific macro context.

    Args:
        curr_date: Current date in yyyy-mm-dd format.
        look_back_days: Number of days to look back (default 7).
        limit: Maximum number of articles (default 30).

    Returns:
        Formatted string containing Chinese market news.
    """
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)

    # Query market-wide announcements using broad keyword search
    # East Money ann API supports keyword filtering via the 'sr' param
    keywords = ["A股", "央行", "经济数据", "北向资金", "半导体"]

    all_articles = []
    seen_titles = set()

    for keyword in keywords:
        try:
            resp = requests.get(
                _ANNO_API,
                params={
                    "sr": "-1",
                    "page_size": str(limit),
                    "page_index": "1",
                    "ann_type": "A",
                    "client_source": "web",
                    "keyword": keyword,
                    "f_node": "0",
                    "s_node": "0",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", {}).get("list", [])
        except Exception as exc:
            logger.debug("East Money global news keyword '%s' failed: %s", keyword, exc)
            continue

        for row in rows:
            title = row.get("title", "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            pub_date = _parse_eastmoney_date(row.get("notice_date"))
            art_code = row.get("art_code", "")
            url = f"https://data.eastmoney.com/notices/detail/0/{art_code}.html" if art_code else ""

            all_articles.append({
                "title": title,
                "pub_date": pub_date,
                "url": url,
                "summary": "",
            })

        if len(all_articles) >= limit:
            break

    if not all_articles:
        return f"No Chinese market news found for {curr_date}"

    # Filter by date window
    filtered = []
    for a in all_articles:
        if in_window(a["pub_date"], start_dt, curr_dt):
            filtered.append(a)

    if not filtered:
        return f"No Chinese market news found between {start_dt.strftime('%Y-%m-%d')} and {curr_date}"

    parts = [f"## China A-Share Market News, from {start_dt.strftime('%Y-%m-%d')} to {curr_date}:\n"]
    for a in filtered[:limit]:
        parts.append(f"### {a['title']} (source: 东方财富)\n")
        if a.get("summary"):
            parts.append(f"{a['summary']}\n")
        if a.get("url"):
            parts.append(f"Link: {a['url']}\n")
        parts.append("\n")

    return "".join(parts)
