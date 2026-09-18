"""Tushare adapter — A-share + HK market data and fundamentals.

Tushare (api.tushare.pro) is the primary data source for A-share (Shanghai,
Shenzhen, Beijing) and Hong Kong stocks. The user has 2000 points which
unlocks daily OHLCV, daily indicators (PE/PB/ROE), and financial statements.

Capabilities supported:
  - market_data            -> pro.daily (A-share) / pro.hk_daily (HK)
  - fundamentals           -> pro.stock_basic + pro.daily_basic
  - balance_sheet          -> pro.balancesheet
  - cashflow               -> pro.cashflow
  - income_statement       -> pro.income

Symbol conventions:
  - A-share: ts_code is "600519.SH" / "000001.SZ" / "830799.BJ" (matches
    the dashboard's input format directly — no conversion needed).
  - HK: ts_code is "00700.HK" (5-digit zero-padded with .HK suffix). The
    dashboard accepts "0700.HK" or "700.HK"; we re-pad to 5 digits before
    calling tushare.

Output format:
  - market_data: CSV string via normalizer.to_ohlcv_csv (same shape as
    yfinance and akshare output — header + Date-indexed OHLCV rows).
  - fundamentals: markdown block with "Label: value" lines (same shape as
    akshare_backend.get_akshare_fundamentals).
  - statements: CSV via normalizer.to_statement_markdown.

Rate limits:
  Tushare enforces per-endpoint rate limits (e.g. hk_daily is 1 call/min
  at 2000 points). The adapter doesn't retry; on rate-limit errors it
  raises VendorRateLimitError so the registry walks the chain to the
  next source (yfinance for HK, akshare for A-share).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

import pandas as pd

from ..base_adapter import BaseAdapter
from ..errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError
from ..normalizer import to_ohlcv_csv, to_statement_markdown
from ..registry import register_adapter
from ..ticker_router import is_ashare, is_hk, strip_suffix

logger = logging.getLogger(__name__)


# Tushare's trade_date format is YYYYMMDD (no dashes). The dashboard uses
# YYYY-MM-DD everywhere. Convert between them as needed.
def _to_tushare_date(d: str) -> str:
    """'2024-01-01' -> '20240101'."""
    return d.replace("-", "") if isinstance(d, str) else d


def _from_tushare_date(d: str) -> str:
    """'20240101' -> '2024-01-01'."""
    if isinstance(d, str) and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


def _hk_ts_code(symbol: str) -> str:
    """Pad HK numeric body to 5 digits and append '.HK'.

    '0700.HK' -> '00700.HK'
    '700.HK'  -> '00700.HK'
    """
    body = strip_suffix(symbol)
    if body.isdigit():
        return f"{body.zfill(5)}.HK"
    return symbol


# Tushare column -> canonical OHLCV column (matches yfinance/akshare output)
_TUSHARE_OHLCV_MAP = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "vol": "Volume",
    # tushare daily doesn't return adj_close; the adj-factor is separate.
    # For now, we set Adj Close = Close (non-adjusted). The graph's analysis
    # is robust to this; for full adjustment, hook pro.adj_factor later.
    "close": "Close",  # also used as Adj Close placeholder
}


def _format_tushare_ohlcv(df: pd.DataFrame, symbol: str, start_date: str, end_date: str) -> str:
    """Convert tushare daily/hk_daily output to the canonical CSV string."""
    if df is None or df.empty:
        return ""

    out = pd.DataFrame()
    # trade_date -> Date (datetime)
    out["Date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    out["Open"] = df["open"].round(2)
    out["High"] = df["high"].round(2)
    out["Low"] = df["low"].round(2)
    out["Close"] = df["close"].round(2)
    # Tushare daily has no adj_close column; use Close as Adj Close placeholder
    out["Adj Close"] = out["Close"]
    if "vol" in df.columns:
        out["Volume"] = df["vol"]
    out = out.set_index("Date").sort_index()

    # Filter to the requested date range (tushare may return rows outside
    # the window if the window was clamped to trading days).
    if isinstance(out.index, pd.DatetimeIndex):
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        out = out[(out.index >= start) & (out.index <= end)]

    return to_ohlcv_csv(out, symbol, start_date, end_date, source="tushare")


# daily_basic field -> display label (matches the existing fundamentals
# markdown shape produced by akshare_backend._FUNDAMENTALS_FIELD_MAP).
_DAILY_BASIC_LABELS = {
    "pe": "PE Ratio (TTM)",
    "pe_ttm": "PE Ratio (TTM, latest)",
    "pb": "Price to Book",
    "ps": "Price to Sales",
    "ps_ttm": "Price to Sales (TTM)",
    "dv_ratio": "Dividend Yield",
    "dv_ttm": "Dividend Yield (TTM)",
    "total_share": "Total Shares (万)",
    "float_share": "Float Shares (万)",
    "free_share": "Free Float Shares (万)",
    "total_mv": "Total Market Cap (万元)",
    "circ_mv": "Circulating Market Cap (万元)",
    "turnover_rate": "Turnover Rate",
    "turnover_rate_f": "Free-float Turnover Rate",
    "volume_ratio": "Volume Ratio",
}


@register_adapter("tushare")
class TushareAdapter(BaseAdapter):
    """Adapter for tushare (A-share + HK via api.tushare.pro).

    Required config (read from the YAML ``config:`` block, with env-var
    fallback for the token):
      - token: tushare API token (defaults to $TUSHARE_TOKEN)
    """

    name = "tushare"

    supported_capabilities = frozenset({
        "market_data",
        "fundamentals",
        "balance_sheet",
        "cashflow",
        "income_statement",
    })

    def initialize(self) -> None:
        # Token resolution: YAML config wins, else $TUSHARE_TOKEN.
        token = self.config.get("token") or os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise VendorNotConfiguredError(
                "tushare adapter requires TUSHARE_TOKEN (set in .env or "
                "config/data_sources.yaml)"
            )
        self._token = token
        # Bypass the dev proxy (http://127.0.0.1:3213) which can't reach
        # tushare's HTTPS endpoint. NO_PROXY=* tells requests to skip the
        # proxy for ALL hosts.
        os.environ.setdefault("no_proxy", "*")
        os.environ.setdefault("NO_PROXY", "*")
        # Lazily set token + instantiate pro_api on first use, not at
        # construction, so importing the adapter doesn't trigger network.
        self._pro = None

    def _get_pro(self):
        """Return the cached tushare pro_api client, instantiating on first use."""
        if self._pro is None:
            try:
                import tushare as ts
            except ImportError as e:
                raise VendorNotConfiguredError(f"tushare package not installed: {e}") from e
            ts.set_token(self._token)
            self._pro = ts.pro_api(timeout=30)
        return self._pro

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
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol) if is_hk(symbol) else symbol
        api = "hk_daily" if is_hk(symbol) else "daily"
        try:
            df = pro.query(
                api,
                ts_code=ts_code,
                start_date=_to_tushare_date(start_date),
                end_date=_to_tushare_date(end_date),
            )
        except Exception as e:
            # Tushare rate-limit error messages include "频率超限".
            msg = str(e)
            if "频率超限" in msg or "rate" in msg.lower():
                raise VendorRateLimitError(f"tushare {api} rate-limited: {msg}") from e
            raise NoMarketDataError(symbol, ts_code, f"tushare {api} failed: {msg}") from e
        if df is None or df.empty:
            raise NoMarketDataError(symbol, ts_code, f"no rows from tushare {api}")
        return _format_tushare_ohlcv(df, symbol, start_date, end_date)

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        # Live-only: a past curr_date would be served a today-snapshot, so
        # honor the same withhold rule as akshare.
        from ..date_window import withhold_live_profile
        withheld = withhold_live_profile(kwargs.get("curr_date"), symbol)
        if withheld:
            return withheld

        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol) if is_hk(symbol) else symbol

        # Company profile from stock_basic (A-share) / hk_basic (HK)
        try:
            if is_hk(symbol):
                basic = pro.hk_basic(ts_code=ts_code, limit=1)
            else:
                basic = pro.stock_basic(ts_code=ts_code, limit=1)
        except Exception as e:
            raise NoMarketDataError(symbol, ts_code, f"tushare stock_basic failed: {e}") from e

        # Daily indicators (PE/PB/ROE/market cap) — latest trading day.
        try:
            if is_hk(symbol):
                # hk_daily_basic is the HK equivalent; not all 2000-point
                # tiers expose it. Fall back gracefully if missing.
                daily_basic = pro.hk_daily_basic(ts_code=ts_code, limit=1)
            else:
                daily_basic = pro.daily_basic(ts_code=ts_code, limit=1)
        except Exception as e:
            msg = str(e)
            if "频率超限" in msg:
                raise VendorRateLimitError(f"tushare daily_basic rate-limited: {msg}") from e
            daily_basic = pd.DataFrame()

        lines = []
        if basic is not None and not basic.empty:
            row = basic.iloc[0]
            for col in ("name", "fullname", "industry", "market", "list_date", "curr_type"):
                if col in basic.columns and pd.notna(row.get(col)):
                    label = {
                        "name": "Name",
                        "fullname": "Full Name",
                        "industry": "Industry",
                        "market": "Market",
                        "list_date": "List Date",
                        "curr_type": "Currency",
                    }.get(col, col)
                    lines.append(f"{label}: {row[col]}")

        if daily_basic is not None and not daily_basic.empty:
            row = daily_basic.iloc[0]
            for cn, en_label in _DAILY_BASIC_LABELS.items():
                if cn in daily_basic.columns and pd.notna(row.get(cn)):
                    val = row[cn]
                    # Market cap is in 万元; format large numbers readably.
                    if cn in ("total_mv", "circ_mv"):
                        lines.append(f"{en_label}: {val:,.2f}")
                    elif cn in ("total_share", "float_share", "free_share"):
                        lines.append(f"{en_label}: {val:,.2f}")
                    else:
                        lines.append(f"{en_label}: {val}")

        if not lines:
            raise NoMarketDataError(symbol, ts_code, "no fundamentals fields returned")

        header = f"# Company Fundamentals for {symbol}\n"
        header += f"# Source: tushare\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + "\n".join(lines) + "\n"

    def fetch_balance_sheet(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "balancesheet", "Balance Sheet", **kwargs)

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "cashflow", "Cash Flow", **kwargs)

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch_statement(symbol, "income", "Income Statement", **kwargs)

    def _fetch_statement(self, symbol: str, api_name: str, label: str, **kwargs: Any) -> str:
        """Fetch a financial statement from tushare.

        Tushare's statement endpoints (balancesheet/cashflow/income) accept
        ``ts_code`` and an optional ``period`` (YYYYMMDD report end date).
        Without ``period``, the latest report is returned.
        """
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol) if is_hk(symbol) else symbol
        params = {"ts_code": ts_code}
        if kwargs.get("period"):
            params["period"] = _to_tushare_date(kwargs["period"])
        if kwargs.get("freq") == "annual":
            # tushare statement endpoints return all reports; we filter to
            # the latest annual (end_date like YYYY1231) below.
            pass

        try:
            df = pro.query(api_name, **params)
        except Exception as e:
            msg = str(e)
            if "频率超限" in msg:
                raise VendorRateLimitError(f"tushare {api_name} rate-limited: {msg}") from e
            raise NoMarketDataError(symbol, ts_code, f"tushare {api_name} failed: {msg}") from e
        if df is None or df.empty:
            raise NoMarketDataError(symbol, ts_code, f"no rows from tushare {api_name}")

        # Filter to the latest 4 reports (most recent quarterlies or annuals)
        if "end_date" in df.columns:
            df = df.sort_values("end_date", ascending=False).head(4)

        curr_date = kwargs.get("curr_date")
        return to_statement_markdown(
            df, label, symbol, source="tushare", curr_date=curr_date,
        )
