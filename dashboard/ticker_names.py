"""Ticker name resolution for the dashboard watchlist.

Maps a ticker symbol to a human-readable company name so the watchlist
cards show e.g. "神州细胞" next to "688520.SH" instead of just the numeric
code.

Strategy:
  - A-share (.SH/.SZ/.BJ) and HK (.HK): tushare ``stock_basic`` /
    ``hk_basic`` (fast, reliable, and we already have a TUSHARE_TOKEN).
  - US: yfinance ``Ticker.info['shortName']`` (best effort -- yfinance is
    often rate-limited, so a miss just leaves the name blank).

Results are cached to ``~/.tradingagents/dashboard/ticker_names.json`` so
subsequent page loads are instant. A cached entry can be force-refreshed
by calling :func:`get_ticker_name` with ``refresh=True``.

Fork extension: this file is new and does not exist in upstream.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from tradingagents.dataflows.ticker_router import is_ashare, is_hk

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(
    os.path.expanduser("~/.tradingagents/dashboard"),
    "ticker_names.json",
)

# In-memory cache to avoid hitting the disk on every render.
_MEM_CACHE: dict[str, Optional[str]] = {}
_CACHE_DIRTY = False


def _load_cache() -> dict[str, Optional[str]]:
    """Load the on-disk name cache into memory (once)."""
    global _MEM_CACHE
    if _MEM_CACHE:
        return _MEM_CACHE
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            _MEM_CACHE = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _MEM_CACHE = {}
    return _MEM_CACHE


def _save_cache() -> None:
    """Persist the in-memory cache to disk (best-effort)."""
    global _CACHE_DIRTY
    if not _CACHE_DIRTY:
        return
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_MEM_CACHE, f, ensure_ascii=False, indent=2)
        _CACHE_DIRTY = False
    except OSError as e:
        logger.warning("Could not save ticker name cache: %s", e)


def _set_cached(ticker: str, name: Optional[str]) -> None:
    cache = _load_cache()
    cache[ticker] = name
    global _CACHE_DIRTY
    _CACHE_DIRTY = True
    _save_cache()


def _fetch_tushare_name(ticker: str) -> Optional[str]:
    """Resolve an A-share or HK ticker name via tushare."""
    try:
        import tushare as ts
    except ImportError:
        logger.debug("tushare not installed; cannot resolve %s", ticker)
        return None

    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        logger.debug("TUSHARE_TOKEN not set; cannot resolve %s", ticker)
        return None

    os.environ.setdefault("TUSHARE_TOKEN", token)
    try:
        pro = ts.pro_api(timeout=15)
        if is_ashare(ticker):
            df = pro.stock_basic(ts_code=ticker, fields="ts_code,name")
        else:  # HK
            # tushare hk_basic expects 5-digit zero-padded codes.
            body = ticker.split(".")[0].zfill(5)
            df = pro.hk_basic(ts_code=f"{body}.HK", fields="ts_code,name")
        if df is not None and not df.empty:
            name = str(df.iloc[0]["name"]).strip()
            return name or None
    except Exception as e:
        logger.warning("tushare name lookup failed for %s: %s", ticker, e)
    return None


def _fetch_yfinance_name(ticker: str) -> Optional[str]:
    """Resolve a US ticker name via yfinance (best effort)."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("shortName") or info.get("longName")
    except Exception as e:
        logger.debug("yfinance name lookup failed for %s: %s", ticker, e)
        return None


def get_ticker_name(ticker: str, refresh: bool = False) -> Optional[str]:
    """Return the company name for ``ticker``, cached.

    Returns ``None`` if the name cannot be resolved (caller falls back to
    showing just the ticker). Pass ``refresh=True`` to bypass the cache
    and re-fetch from the data source.
    """
    if not ticker:
        return None

    cache = _load_cache()
    if not refresh and ticker in cache:
        return cache[ticker]  # may be None (negative cache)

    name: Optional[str] = None
    if is_ashare(ticker) or is_hk(ticker):
        name = _fetch_tushare_name(ticker)
    else:
        name = _fetch_yfinance_name(ticker)

    _set_cached(ticker, name)
    return name


def get_watchlist_names(tickers: list[str]) -> dict[str, Optional[str]]:
    """Resolve names for a list of tickers in one call.

    Returns ``{ticker: name}``. Missing names are ``None``. This is the
    fast path for the watchlist summary -- all cached entries are returned
    immediately and only uncached tickers trigger a network call.
    """
    cache = _load_cache()
    result: dict[str, Optional[str]] = {}
    for t in tickers:
        result[t] = cache.get(t) if t in cache else get_ticker_name(t)
    return result
