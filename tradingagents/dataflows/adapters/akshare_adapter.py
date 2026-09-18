"""Akshare adapter — wraps the existing akshare_backend.py functions.

Phase 1 strategy: like the yfinance adapter, this is a thin delegate to
the legacy akshare_backend functions. Same CSV / markdown contracts,
same call signatures. The akshare backend serves A-share market data
and statements; it does not serve news, insider transactions, or US
tickers (those go through yfinance via the per-market YAML chain).

Capabilities supported:
  - market_data            -> get_akshare_stock_data
  - technical_indicators   -> get_akshare_stock_stats_indicators_window
  - fundamentals           -> get_akshare_fundamentals
  - balance_sheet          -> get_akshare_balance_sheet
  - cashflow               -> get_akshare_cashflow
  - income_statement       -> get_akshare_income_statement
"""

from __future__ import annotations

import logging
from typing import Any

from ..akshare_backend import (
    get_akshare_balance_sheet,
    get_akshare_cashflow,
    get_akshare_fundamentals,
    get_akshare_income_statement,
    get_akshare_stock_data,
    get_akshare_stock_stats_indicators_window,
)
from ..base_adapter import BaseAdapter
from ..registry import register_adapter

logger = logging.getLogger(__name__)


@register_adapter("akshare")
class AkshareAdapter(BaseAdapter):
    """Adapter for akshare (free A-share / HK data via eastmoney).

    akshare is keyless; the only per-source config it accepts is an
    optional reachability host override (for tests / proxies).
    """

    name = "akshare"

    supported_capabilities = frozenset({
        "market_data",
        "technical_indicators",
        "fundamentals",
        "balance_sheet",
        "cashflow",
        "income_statement",
    })

    def initialize(self) -> None:
        # akshare is keyless; nothing to validate. Network reachability
        # is checked lazily by the underlying functions via
        # vendor_reachable() in stockstats_utils / utils.py.
        self.logger.debug("akshare adapter ready (keyless)")

    # ------------------------------------------------------------------
    # Capability methods
    # ------------------------------------------------------------------
    def fetch_market_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        **kwargs: Any,
    ) -> str:
        return get_akshare_stock_data(symbol, start_date, end_date)

    def fetch_technical_indicators(
        self,
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int,
        **kwargs: Any,
    ) -> str:
        return get_akshare_stock_stats_indicators_window(
            symbol, indicator, curr_date, look_back_days
        )

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        curr_date = kwargs.get("curr_date")
        return get_akshare_fundamentals(symbol, curr_date=curr_date)

    def fetch_balance_sheet(self, symbol: str, **kwargs: Any) -> str:
        freq = kwargs.get("freq", "quarterly")
        curr_date = kwargs.get("curr_date")
        return get_akshare_balance_sheet(symbol, freq=freq, curr_date=curr_date)

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        freq = kwargs.get("freq", "quarterly")
        curr_date = kwargs.get("curr_date")
        return get_akshare_cashflow(symbol, freq=freq, curr_date=curr_date)

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        freq = kwargs.get("freq", "quarterly")
        curr_date = kwargs.get("curr_date")
        return get_akshare_income_statement(symbol, freq=freq, curr_date=curr_date)
