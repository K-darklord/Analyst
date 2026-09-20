"""Route ticker symbols to their canonical market.

This is the single source of truth for "which market does this ticker belong
to?" — used by the DataSourceRegistry to pick the right per-market source
chain from `config/data_sources.yaml`.

Markets:
  - US       — bare tickers (NVDA, AAPL) or anything that doesn't match the
               A-share / HK patterns (including forex, crypto, futures)
  - A_SHARE  — .SH / .SZ / .BJ suffix (e.g. 600519.SH, 000001.SZ, 830799.BJ)
  - HK       — .HK suffix (e.g. 0700.HK, 9988.HK)

The HK pattern matches the same shape as Yahoo's HKEX convention so the
existing symbol_utils normalization path keeps working: incoming broker
symbols like ``700.HK`` get zero-padded upstream and arrive here as
``0700.HK``, which the registry then routes to the HK chain.

This module is intentionally tiny and free of vendor imports so it can be
used from the registry, the dashboard, and tests without pulling in pandas /
yfinance / akshare.
"""

from __future__ import annotations

import re
from typing import Literal

Market = Literal["US", "A_SHARE", "HK"]

# Public enum-ish constants for callers that prefer importing names over
# strings (e.g. `if route == Markets.A_SHARE`). String values match the
# keys used in data_sources.yaml.
class Markets:
    US = "US"
    A_SHARE = "A_SHARE"
    HK = "HK"


# .SH / .SZ / .BJ — Shanghai, Shenzhen, Beijing exchanges. Case-insensitive.
_ASHARE_RE = re.compile(r"\.(SH|SZ|BJ)$", re.IGNORECASE)

# .HK — Hong Kong Stock Exchange. Number-only codes are zero-padded to 4
# digits upstream by symbol_utils.normalize_symbol(); we accept any 1-5
# digit body to be permissive about pre-normalization input.
_HK_RE = re.compile(r"^(\d{1,5})\.HK$", re.IGNORECASE)


def route(symbol: str) -> Market:
    """Return the market key for ``symbol``.

    Unknown / non-matching symbols default to ``US``. This preserves
    backwards compatibility for tickers that have no exchange suffix
    (the historical default in Analyst) and for forex / crypto /
    futures symbols that route through yfinance.
    """
    if not isinstance(symbol, str) or not symbol:
        return Markets.US
    if _ASHARE_RE.search(symbol):
        return Markets.A_SHARE
    if _HK_RE.match(symbol):
        return Markets.HK
    return Markets.US


def is_ashare(symbol: str) -> bool:
    """True if ``symbol`` carries an A-share exchange suffix."""
    return bool(symbol) and bool(_ASHARE_RE.search(symbol))


def is_hk(symbol: str) -> bool:
    """True if ``symbol`` carries the .HK exchange suffix."""
    return bool(symbol) and bool(_HK_RE.match(symbol))


def is_us(symbol: str) -> bool:
    """True if ``symbol`` does not carry an A-share or HK suffix."""
    return route(symbol) == Markets.US


def strip_suffix(symbol: str) -> str:
    """Strip the exchange suffix from an A-share or HK ticker.

    ``"600519.SH"``  -> ``"600519"``
    ``"0700.HK"``    -> ``"0700"``
    ``"NVDA"``       -> ``"NVDA"``  (unchanged)
    """
    if not isinstance(symbol, str):
        return symbol
    # A-share: strip everything from the dot onwards (".SH" / ".SZ" / ".BJ")
    m = _ASHARE_RE.search(symbol)
    if m:
        return symbol[: m.start()]
    # HK: the regex captures the numeric body; return it directly
    m = _HK_RE.match(symbol)
    if m:
        return m.group(1)
    return symbol


def exchange_tag(symbol: str) -> str:
    """Return the bare exchange tag for an A-share or HK symbol.

    ``"600519.SH"``  -> ``"SH"``
    ``"0700.HK"``    -> ``"HK"``
    ``"NVDA"``       -> ``""``  (US tickers have no tag)
    """
    if not isinstance(symbol, str):
        return ""
    m = _ASHARE_RE.search(symbol)
    if m:
        return m.group(1).upper()
    m = _HK_RE.match(symbol)
    if m:
        return "HK"
    return ""


__all__ = [
    "Market",
    "Markets",
    "route",
    "is_ashare",
    "is_hk",
    "is_us",
    "strip_suffix",
    "exchange_tag",
]
