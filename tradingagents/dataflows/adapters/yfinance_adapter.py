"""YFinance adapter — wraps the existing y_finance.py vendor functions.

Phase 1 strategy: instead of moving code out of y_finance.py (which
would break every import in interface.py and the agents), this adapter
delegates to the legacy functions. The contract is identical: the same
CSV / markdown strings flow to the agents. The registry layer above
becomes the new dispatch path; the legacy interface.route_to_vendor
will be refactored to delegate here, but until then both paths work.

Capabilities supported (mirrors VENDOR_METHODS entries with a yfinance row):
  - market_data            -> get_YFin_data_online
  - technical_indicators   -> get_stock_stats_indicators_window
  - fundamentals           -> get_fundamentals
  - balance_sheet          -> get_balance_sheet
  - cashflow               -> get_cashflow
  - income_statement       -> get_income_statement
  - news                   -> get_news_yfinance
  - global_news            -> get_global_news_yfinance
  - insider_transactions   -> get_insider_transactions
"""

from __future__ import annotations

import logging
from typing import Any

from ..base_adapter import BaseAdapter
from ..registry import register_adapter
from ..y_finance import (
    get_YFin_data_online,
    get_balance_sheet as _yf_balance_sheet,
    get_cashflow as _yf_cashflow,
    get_fundamentals as _yf_fundamentals,
    get_income_statement as _yf_income_statement,
    get_insider_transactions as _yf_insider_transactions,
    get_stock_stats_indicators_window,
)
from ..yfinance_news import get_global_news_yfinance, get_news_yfinance

logger = logging.getLogger(__name__)


@register_adapter("yfinance")
class YFinanceAdapter(BaseAdapter):
    """Adapter for Yahoo Finance (yfinance SDK).

    Configurable per-source options (read from the YAML ``config:``
    block, with env-var fallbacks for credentials):
      - api_key: ignored (yfinance is keyless)
    """

    name = "yfinance"

    supported_capabilities = frozenset({
        "market_data",
        "technical_indicators",
        "fundamentals",
        "balance_sheet",
        "cashflow",
        "income_statement",
        "news",
        "global_news",
        "insider_transactions",
    })

    def initialize(self) -> None:
        # yfinance is keyless; nothing to validate. If we wanted to
        # probe reachability, we'd do it in healthcheck().
        self.logger.debug("yfinance adapter ready (keyless)")

    # ------------------------------------------------------------------
    # Capability methods — thin delegates to the legacy functions.
    # ------------------------------------------------------------------
    def fetch_market_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        **kwargs: Any,
    ) -> str:
        return get_YFin_data_online(symbol, start_date, end_date)

    def fetch_technical_indicators(
        self,
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int,
        **kwargs: Any,
    ) -> str:
        return get_stock_stats_indicators_window(
            symbol, indicator, curr_date, look_back_days
        )

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        return _yf_fundamentals(symbol)

    def fetch_balance_sheet(self, symbol: str, **kwargs: Any) -> str:
        return _yf_balance_sheet(symbol)

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        return _yf_cashflow(symbol)

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        return _yf_income_statement(symbol)

    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        return get_news_yfinance(symbol, **kwargs)

    def fetch_global_news(self, **kwargs: Any) -> str:
        return get_global_news_yfinance(**kwargs)

    def fetch_insider_transactions(self, symbol: str, **kwargs: Any) -> str:
        return _yf_insider_transactions(symbol)
