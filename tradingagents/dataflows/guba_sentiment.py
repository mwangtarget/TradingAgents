"""East Money Guba (股吧) — Chinese retail sentiment forum fetcher.

East Money's Guba (股吧) is the closest Chinese equivalent to StockTwits:
a per-stock message board where retail investors post opinions, news commentary,
and discussion threads. Each post carries click count and comment count as
engagement signals (analogous to Reddit upvotes/comment counts).

Data is extracted from the server-rendered HTML page at
``guba.eastmoney.com/list,{code},1,f_1.html`` which embeds a
``var article_list = {...}`` JSON block in a ``<script>`` tag.

No API key required. Returns formatted plaintext blocks ready for prompt
injection and degrades gracefully — returns a placeholder string rather than
raising, so callers never special-case missing data.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .date_window import in_window
from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

_GUBA_URL = "https://guba.eastmoney.com/list,{code},1,f_{page}.html"
_UA = "tradingagents/0.4 (+https://github.com/TauricResearch/TradingAgents)"
_BEIJING_TZ = timezone(timedelta(hours=8))

# Regex to extract the embedded article_list JSON from the HTML
_ARTICLE_LIST_RE = re.compile(
    r"var\s+article_list\s*=\s*(\{.*?\});\s*</script>",
    re.DOTALL,
)


def _ts_code_to_guba_code(ticker: str) -> str:
    """Convert a ts_code (e.g. 300308.SZ) to a Guba stock code (300308)."""
    # Guba uses bare 6-digit codes without the .SS/.SZ suffix
    normalized = normalize_symbol(ticker)
    # normalize_symbol returns Yahoo format like 300308.SZ; strip suffix
    if "." in normalized:
        return normalized.split(".")[0]
    return normalized


def _parse_guba_datetime(raw: str) -> datetime | None:
    """Parse a Guba timestamp string (Beijing time) to UTC datetime.

    Guba timestamps look like "2026-09-12 21:09:00" (Beijing time, UTC+8).
    Returns None if parsing fails.
    """
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
        # Interpret as Beijing time, convert to UTC
        dt = dt.replace(tzinfo=_BEIJING_TZ)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=_BEIJING_TZ)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None


def _within_window(posts: list[dict], start_date: str | None, end_date: str | None) -> list[dict]:
    """Keep only posts published in [start_date, end_date] (look-ahead safe).

    No window (both None) leaves the list untouched for live callers.
    A post with no parseable timestamp is dropped in a historical window.
    """
    if not (start_date and end_date):
        return posts
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    kept = []
    for p in posts:
        created = _parse_guba_datetime(p.get("post_publish_time", ""))
        if in_window(created, start_dt, end_dt):
            kept.append(p)
    return kept


def _fetch_guba_posts(ticker: str, limit: int = 50) -> list[dict]:
    """Fetch raw Guba posts for a given A-share ticker.

    Returns a list of post dicts with keys: post_title, post_publish_time,
    post_click_count, post_comment_count, user_nickname, Art_Url.
    """
    code = _ts_code_to_guba_code(ticker)
    posts: list[dict] = []

    # Fetch up to 2 pages (80 posts per page) to cover ~7 days
    for page in (1, 2):
        url = _GUBA_URL.format(code=code, page=page)
        req = Request(url, headers={"User-Agent": _UA})
        try:
            with urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            logger.warning("Guba fetch failed for %s page %d: %s", ticker, page, exc)
            break

        match = _ARTICLE_LIST_RE.search(html)
        if not match:
            logger.debug("No article_list found in Guba page %d for %s", page, ticker)
            break

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            logger.warning("Failed to parse Guba article_list JSON for %s", ticker)
            break

        page_posts = data.get("re", [])
        if not page_posts:
            break
        posts.extend(page_posts)

        if len(posts) >= limit:
            break

    return posts[:limit]


def fetch_guba_posts(
    ticker: str,
    limit: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Fetch East Money Guba (股吧) posts for a Chinese A-share ticker.

    This is the Chinese equivalent of StockTwits — a per-stock retail investor
    forum with engagement signals (click count ≈ reach, comment count ≈ engagement).

    Parameters
    ----------
    ticker : str
        A-share ts_code, e.g. ``"300308.SZ"``.
    limit : int
        Maximum number of posts to return (default 30).
    start_date, end_date : str | None
        Optional ``"YYYY-MM-DD"`` window for look-ahead-safe filtering.

    Returns
    -------
    str
        Formatted plaintext ready for LLM prompt injection. Returns an
        ``<unavailable>`` placeholder on failure so the caller never
        special-cases missing data.
    """
    try:
        raw_posts = _fetch_guba_posts(ticker, limit=limit * 2)  # over-fetch before date filter
    except Exception as exc:
        logger.warning("Guba fetch error for %s: %s", ticker, exc)
        return "<unavailable>"

    if not raw_posts:
        return f"No Guba posts found for {ticker}."

    posts = _within_window(raw_posts, start_date, end_date)
    if not posts:
        return f"No Guba posts found for {ticker} in the requested period."

    lines: list[str] = []
    for p in posts[:limit]:
        title = p.get("post_title", "(no title)").strip()
        pub = p.get("post_publish_time", "").strip()
        clicks = p.get("post_click_count", 0)
        comments = p.get("post_comment_count", 0)
        author = p.get("user_nickname", "").strip()
        url = p.get("Art_Url", "").strip()
        lines.append(f"- [{pub}] {title}")
        lines.append(f"  clicks={clicks}, comments={comments}, author={author}")
        if url:
            lines.append(f"  link: {url}")
        lines.append("")

    header = f"East Money Guba (股吧) posts for {ticker}"
    if start_date and end_date:
        header += f", {start_date} to {end_date}"
    header += f" ({len(posts[:limit])} posts):"
    return header + "\n\n" + "\n".join(lines)
