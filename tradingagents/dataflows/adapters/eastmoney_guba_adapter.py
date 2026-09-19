"""Eastmoney Guba (东方财富股吧) adapter — A-share retail forum sentiment.

Scrapes the per-stock forum page from guba.eastmoney.com to collect recent
post titles, authors, read counts, and reply counts. Post titles serve as
retail-investor sentiment signals (bullish/bearish framing).

The adapter uses plain HTTP + BeautifulSoup (no browser required). Each
forum page lists ~80 posts; we cap the output to the most recent N posts.

Capability: sentiment
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from tradingagents.dataflows.base_adapter import BaseAdapter
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.registry import register_adapter
from tradingagents.dataflows.ticker_router import is_ashare, strip_suffix

logger = logging.getLogger(__name__)

GUBA_URL = "http://guba.eastmoney.com/list,{code}.html"

# Headers to mimic a real browser request.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://guba.eastmoney.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@register_adapter("eastmoney_guba")
class EastmoneyGubaAdapter(BaseAdapter):
    """Fetches A-share stock forum posts from Eastmoney Guba."""

    name = "eastmoney_guba"

    supported_capabilities = frozenset({"sentiment"})

    def initialize(self) -> None:
        # No token required; the forum is publicly accessible.
        pass

    # ------------------------------------------------------------------
    # Sentiment (forum posts)
    # ------------------------------------------------------------------
    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        """Return recent Guba posts as a sentiment text block.

        Although the capability is registered as ``sentiment``, the registry
        dispatch calls ``fetch_news`` by convention (the first capability in
        the sentiment category). We route the call to ``fetch_sentiment``.
        """
        return self.fetch_sentiment(symbol, **kwargs)

    def fetch_sentiment(self, symbol: str, **kwargs: Any) -> str:
        """Fetch recent forum posts for an A-share stock.

        Args:
            symbol: A-share ticker (e.g. ``688521.SH``).
            start_date / end_date: optional window (currently advisory; the
                forum lists posts by recency).
            limit: max posts to return (default 30).
        """
        if not is_ashare(symbol):
            raise NoMarketDataError(
                symbol, symbol,
                "eastmoney_guba only supports A-share tickers (.SH/.SZ/.BJ)",
            )

        code = strip_suffix(symbol)
        limit = int(kwargs.get("limit", 30))

        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError as e:
            raise NoMarketDataError(
                symbol, code, f"missing dependency for eastmoney_guba: {e}",
            ) from e

        url = GUBA_URL.format(code=code)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            raise NoMarketDataError(
                symbol, code, f"failed to fetch guba page: {e}",
            ) from e

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.listitem")
        if not rows:
            raise NoMarketDataError(
                symbol, code, "no posts found (page structure changed?)",
            )

        posts = []
        for row in rows:
            title_el = row.select_one("div.title a")
            read_el = row.select_one("div.read")
            reply_el = row.select_one("div.reply")
            author_el = row.select_one("div.author a")
            date_el = row.select_one("div.update")
            if not title_el:
                continue
            posts.append({
                "title": title_el.get_text(strip=True),
                "reads": read_el.get_text(strip=True) if read_el else "0",
                "replies": reply_el.get_text(strip=True) if reply_el else "0",
                "author": author_el.get_text(strip=True) if author_el else "",
                "date": date_el.get_text(strip=True) if date_el else "",
            })
            if len(posts) >= limit:
                break

        if not posts:
            raise NoMarketDataError(
                symbol, code, "parsed posts but all were empty",
            )

        header = (
            f"## 东方财富股吧 posts for {symbol} (code={code})\n"
            f"# Source: eastmoney guba\n"
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# Post count: {len(posts)}\n\n"
        )
        lines = [
            f"[{p['reads']} reads / {p['replies']} replies] "
            f"{p['title']}  —{p['author']} ({p['date']})"
            for p in posts
        ]
        return header + "\n".join(lines)
