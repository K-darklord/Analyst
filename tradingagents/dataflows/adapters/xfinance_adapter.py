"""xFinance adapter — multi-source failover for Yahoo Finance.

xfinance (https://pypi.org/project/xfinance/) wraps Yahoo Finance with
automatic failover to Stooq, SEC EDGAR, ECB, Binance, and CoinGecko.
When Yahoo rate-limits or blocks, the router transparently tries the next
source. Each Ticker also carries a 5-minute in-memory cache, reducing
repeated hits to Yahoo for the same instrument.

This adapter is a drop-in sibling of YFinanceAdapter: it produces the
exact same string payloads (CSV for OHLCV / statements, key:value lines
for fundamentals, CSV for insider transactions) so the agents see no
difference when the registry falls back from yfinance to xfinance.

Capabilities supported (mirrors yfinance_adapter):
  - market_data            -> xf.Ticker.history
  - fundamentals           -> xf.Ticker.info
  - balance_sheet          -> xf.Ticker.balance_sheet
  - cashflow               -> xf.Ticker.cashflow
  - income_statement       -> xf.Ticker.income_stmt
  - news                   -> xf.Ticker.news
  - insider_transactions   -> xf.Ticker.insider_transactions

Failure translation:
  - xfinance.exceptions.AllSourcesFailedError -> NoMarketDataError
  - xfinance.exceptions.SourceRateLimitError  -> VendorRateLimitError
  - anything else                              -> NoMarketDataError
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..base_adapter import BaseAdapter
from ..errors import NoMarketDataError, VendorRateLimitError
from ..registry import register_adapter
from ..symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

_FUNDAMENTAL_FIELDS = [
    ("Name", "longName"),
    ("Sector", "sector"),
    ("Industry", "industry"),
    ("Market Cap", "marketCap"),
    ("PE Ratio (TTM)", "trailingPE"),
    ("Forward PE", "forwardPE"),
    ("PEG Ratio", "pegRatio"),
    ("Price to Book", "priceToBook"),
    ("EPS (TTM)", "trailingEps"),
    ("Forward EPS", "forwardEps"),
    ("Dividend Yield", "dividendYield"),
    ("Beta", "beta"),
    ("52 Week High", "fiftyTwoWeekHigh"),
    ("52 Week Low", "fiftyTwoWeekLow"),
    ("50 Day Average", "fiftyDayAverage"),
    ("200 Day Average", "twoHundredDayAverage"),
    ("Revenue (TTM)", "totalRevenue"),
    ("Gross Profit", "grossProfits"),
    ("EBITDA", "ebitda"),
    ("Net Income", "netIncomeToCommon"),
    ("Profit Margin", "profitMargins"),
    ("Operating Margin", "operatingMargins"),
    ("Return on Equity", "returnOnEquity"),
    ("Return on Assets", "returnOnAssets"),
    ("Debt to Equity", "debtToEquity"),
    ("Current Ratio", "currentRatio"),
    ("Book Value", "bookValue"),
    ("Free Cash Flow", "freeCashflow"),
]


def _import_xfinance():
    try:
        import xfinance as xf
        return xf
    except ImportError as e:
        raise NoMarketDataError(
            "xfinance", "xfinance",
            f"xfinance package not installed: {e}",
        )


def _translate_error(symbol: str, exc: Exception) -> Exception:
    try:
        from xfinance.exceptions import (
            AllSourcesFailedError,
            SourceRateLimitError,
        )
    except ImportError:
        return NoMarketDataError(symbol, symbol, str(exc))
    if isinstance(exc, SourceRateLimitError):
        return VendorRateLimitError(str(exc))
    if isinstance(exc, AllSourcesFailedError):
        return NoMarketDataError(symbol, symbol, f"xfinance all sources failed: {exc}")
    return NoMarketDataError(symbol, symbol, str(exc))


@register_adapter("xfinance")
class XFinanceAdapter(BaseAdapter):
    """Adapter for xFinance (Yahoo + multi-source failover).

    xfinance is keyless; the only per-source config it accepts is an
    optional ``proxy`` URL forwarded to xf.Ticker(..., proxy=...).
    """

    name = "xfinance"

    supported_capabilities = frozenset({
        "market_data",
        "fundamentals",
        "balance_sheet",
        "cashflow",
        "income_statement",
        "news",
        "insider_transactions",
    })

    def initialize(self) -> None:
        self.logger.debug("xfinance adapter ready (keyless, multi-source failover)")

    def _ticker(self, symbol: str):
        xf = _import_xfinance()
        proxy = self.config.get("proxy")
        return xf.Ticker(normalize_symbol(symbol), proxy=proxy)

    def fetch_market_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        **kwargs: Any,
    ) -> str:
        canonical = normalize_symbol(symbol)
        try:
            t = self._ticker(symbol)
            data = t.history(start=start_date, end=end_date)
        except Exception as e:
            raise _translate_error(canonical, e) from e

        if data is None or (hasattr(data, "empty") and data.empty):
            raise NoMarketDataError(
                symbol, canonical,
                f"no OHLCV rows between {start_date} and {end_date}",
            )

        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)
        numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
        for col in numeric_columns:
            if col in data.columns:
                data[col] = data[col].round(2)
        csv_string = data.to_csv()

        label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
        header = f"# Stock data for {label} from {start_date} to {end_date}\n"
        header += f"# Total records: {len(data)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        header += "# Source: xfinance (Yahoo/Stooq/... multi-source failover)\n\n"
        return header + csv_string

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        canonical = normalize_symbol(symbol)
        try:
            info = self._ticker(symbol).info
        except Exception as e:
            raise _translate_error(canonical, e) from e

        if not info:
            raise NoMarketDataError(symbol, canonical, "no fundamental fields returned")

        lines = [
            f"{label}: {info.get(key)}"
            for label, key in _FUNDAMENTAL_FIELDS
            if info.get(key) is not None
        ]
        if not lines:
            raise NoMarketDataError(symbol, canonical, "no fundamental fields returned")

        header = f"# Company Fundamentals for {canonical}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        header += "# Source: xfinance\n\n"
        return header + "\n".join(lines)

    def fetch_balance_sheet(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "balance_sheet", "Balance Sheet", kwargs.get("freq", "quarterly"))

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "cashflow", "Cash Flow", kwargs.get("freq", "quarterly"))

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "income_stmt", "Income Statement", kwargs.get("freq", "quarterly"))

    def _fetch_statement(self, symbol: str, attr: str, label: str, freq: str) -> str:
        canonical = normalize_symbol(symbol)
        try:
            t = self._ticker(symbol)
            data = getattr(t, attr)
        except Exception as e:
            raise _translate_error(canonical, e) from e

        if data is None or (hasattr(data, "empty") and data.empty):
            raise NoMarketDataError(symbol, canonical, f"no {label.lower()} data")

        csv_string = data.to_csv()
        header = f"# {label} data for {canonical} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        header += "# Source: xfinance\n\n"
        return header + csv_string

    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        canonical = normalize_symbol(symbol)
        try:
            news = self._ticker(symbol).news or []
        except Exception as e:
            raise _translate_error(canonical, e) from e

        if not news:
            raise NoMarketDataError(symbol, canonical, "no news returned")

        lines = [f"# News for {canonical}", f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
        for item in news:
            title = item.get("title") or item.get("headline") or "(no title)"
            link = item.get("link") or item.get("url") or ""
            pub = item.get("providerPublishTime") or item.get("published") or ""
            if isinstance(pub, (int, float)):
                pub = datetime.fromtimestamp(pub).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {title}")
            if pub:
                lines.append(f"  Published: {pub}")
            if link:
                lines.append(f"  Link: {link}")
            lines.append("")
        return "\n".join(lines)

    def fetch_insider_transactions(self, symbol: str, **kwargs: Any) -> str:
        canonical = normalize_symbol(symbol)
        try:
            data = self._ticker(symbol).insider_transactions
        except Exception as e:
            raise _translate_error(canonical, e) from e

        if data is None or (hasattr(data, "empty") and data.empty):
            return f"No insider transactions reported for symbol '{canonical}'"

        csv_string = data.to_csv()
        header = f"# Insider Transactions data for {canonical}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        header += "# Source: xfinance\n\n"
        return header + csv_string
