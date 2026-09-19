"""Tushare adapter — A-share + HK market data and fundamentals.

Tushare (api.tushare.pro) is the primary data source for A-share (Shanghai,
Shenzhen, Beijing) and Hong Kong stocks. The user has 10000 points which
unlocks daily OHLCV, daily indicators (PE/PB/ROE), financial statements,
money flow, margin trading, top list (龙虎榜), research reports, company
announcements, shareholder trades, and macro indicators (CPI/PMI/SHIBOR).

Capabilities supported:
  - market_data            -> pro.daily (A-share) / pro.hk_daily (HK)
  - fundamentals           -> pro.stock_basic + pro.daily_basic
  - balance_sheet          -> pro.balancesheet
  - cashflow               -> pro.cashflow
  - income_statement       -> pro.income
  - news                   -> pro.anns_d + pro.research_report + pro.major_news
  - global_news            -> pro.major_news (market-wide financial news)
  - insider_transactions   -> pro.stk_holdertrade (major shareholder trades)

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
        "news",
        "global_news",
        "insider_transactions",
        "macro_data",
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
        """Return the cached tushare pro_api client, instantiating on first use.

        We set the token via env var instead of ts.set_token() to avoid
        tushare writing ~/tk.csv (which fails when the home dir isn't
        writable, e.g. inside a sandboxed dashboard process).
        """
        if self._pro is None:
            try:
                import tushare as ts
            except ImportError as e:
                raise VendorNotConfiguredError(f"tushare package not installed: {e}") from e
            os.environ["TUSHARE_TOKEN"] = self._token
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

        # HK EXTENSION: HK stocks use a separate path (hk_basic + hk_income)
        # because hk_daily_basic is not available at this permission tier.
        # The HK path returns company info + latest income figures instead
        # of PE/PB/market cap.
        if is_hk(symbol):
            return self._fetch_hk_fundamentals(symbol, **kwargs)

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
        # HK EXTENSION: route HK stocks to the long-format HK endpoints.
        if is_hk(symbol):
            return self._fetch_hk_statement(
                symbol, "hk_balancesheet", "Balance Sheet", **kwargs,
            )
        return self._fetch_statement(symbol, "balancesheet", "Balance Sheet", **kwargs)

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        # HK EXTENSION: route HK stocks to the long-format HK endpoints.
        if is_hk(symbol):
            return self._fetch_hk_statement(
                symbol, "hk_cashflow", "Cash Flow", **kwargs,
            )
        return self._fetch_statement(symbol, "cashflow", "Cash Flow", **kwargs)

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        # HK EXTENSION: route HK stocks to the long-format HK endpoints.
        if is_hk(symbol):
            return self._fetch_hk_statement(
                symbol, "hk_income", "Income Statement", **kwargs,
            )
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

    # ------------------------------------------------------------------
    # HK EXTENSION: Hong Kong financial statements (港股财报独立权限)
    # ------------------------------------------------------------------
    # Tushare's HK statement endpoints (hk_income / hk_balancesheet /
    # hk_cashflow) return "long format" data:
    #   columns: ts_code, end_date, name, ind_name, ind_value
    # where each row is a single financial item (ind_name) for a single
    # reporting period (end_date). The A-share endpoints return wide
    # format (one row per period, columns per item), so the HK path
    # pivots long -> wide before formatting.
    #
    # hk_daily_basic is not available at this permission tier, so the HK
    # fundamentals path returns company info (hk_basic) + the latest
    # income statement figures (hk_income) instead of PE/PB/market cap.
    # ------------------------------------------------------------------
    def _fetch_hk_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        """Fetch HK fundamentals from tushare.

        Uses ``pro.hk_basic`` for company info (name, industry, list_date)
        and ``pro.hk_income`` for the latest income statement figures
        (营业额, 毛利, 销售成本, etc.). ``hk_daily_basic`` is not available
        at this permission tier, so PE/PB are omitted; the income figures
        fill in financial context.
        """
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol)

        # Company profile from hk_basic
        try:
            basic = pro.hk_basic(ts_code=ts_code, limit=1)
        except Exception as e:
            raise NoMarketDataError(
                symbol, ts_code, f"tushare hk_basic failed: {e}",
            ) from e

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

        # Latest income statement figures from hk_income (long format).
        # Each row is (ts_code, end_date, name, ind_name, ind_value); we
        # take the most recent end_date and dump every item as
        # "  ind_name: ind_value" so the LLM sees revenue / gross profit /
        # cost of sales / net profit etc. for the latest period.
        try:
            income_df = pro.hk_income(ts_code=ts_code)
            if income_df is not None and not income_df.empty:
                income_df = income_df.dropna(subset=["ind_name", "ind_value"])
                if not income_df.empty:
                    latest_period = income_df["end_date"].max()
                    latest = income_df[income_df["end_date"] == latest_period]
                    lines.append(f"Latest Income Period: {latest_period}")
                    for _, r in latest.iterrows():
                        ind_name = r.get("ind_name", "")
                        ind_value = r.get("ind_value", "")
                        if pd.notna(ind_name) and pd.notna(ind_value):
                            lines.append(f"  {ind_name}: {ind_value}")
        except Exception as e:
            logger.info("tushare hk_income failed for %s: %s", ts_code, e)

        if not lines:
            raise NoMarketDataError(symbol, ts_code, "no fundamentals fields returned")

        header = f"# Company Fundamentals for {symbol}\n"
        header += f"# Source: tushare\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + "\n".join(lines) + "\n"

    def _fetch_hk_statement(
        self, symbol: str, api_name: str, label: str, **kwargs: Any,
    ) -> str:
        """Fetch an HK financial statement from tushare.

        HK statement endpoints (``hk_income`` / ``hk_balancesheet`` /
        ``hk_cashflow``) return long-format data with columns ``ts_code,
        end_date, name, ind_name, ind_value``. Pivot to wide format
        (ind_name -> columns, ind_value -> values, one row per end_date)
        so the output matches the A-share statement shape consumed by
        ``to_statement_markdown``.
        """
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol)
        params = {"ts_code": ts_code}
        if kwargs.get("period"):
            params["period"] = _to_tushare_date(kwargs["period"])

        try:
            df = pro.query(api_name, **params)
        except Exception as e:
            msg = str(e)
            if "频率超限" in msg:
                raise VendorRateLimitError(
                    f"tushare {api_name} rate-limited: {msg}",
                ) from e
            raise NoMarketDataError(
                symbol, ts_code, f"tushare {api_name} failed: {msg}",
            ) from e
        if df is None or df.empty:
            raise NoMarketDataError(symbol, ts_code, f"no rows from tushare {api_name}")

        # Pivot long -> wide. Drop rows missing ind_name/ind_value first
        # so pivot_table doesn't drop the whole period. Coerce ind_value
        # to numeric so to_statement_markdown can round floats cleanly.
        if {"ind_name", "ind_value"}.issubset(df.columns):
            df = df.dropna(subset=["ind_name", "ind_value"])
            df["ind_value"] = pd.to_numeric(df["ind_value"], errors="coerce")
            df = (
                df.pivot_table(
                    index="end_date",
                    columns="ind_name",
                    values="ind_value",
                    aggfunc="first",  # dedupe if (end_date, ind_name) repeats
                )
                .reset_index()
            )
            # Tidy: flatten the columns index back to a simple list
            df.columns.name = None

        # Filter to the latest 4 reports (matches the A-share path).
        if "end_date" in df.columns:
            df = df.sort_values("end_date", ascending=False).head(4)

        curr_date = kwargs.get("curr_date")
        return to_statement_markdown(
            df, label, symbol, source="tushare", curr_date=curr_date,
        )

    # ------------------------------------------------------------------
    # News & announcements (10000-point tier)
    # ------------------------------------------------------------------
    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        """Fetch stock-specific news from tushare.

        Tushare has no single per-stock news endpoint at 10000 points, so we
        combine three stock-specific feeds into one markdown report:
          1. pro.anns_d           — company announcements (公告)
          2. pro.research_report  — analyst research reports (研报)
          3. pro.major_news       — recent market-wide financial news (context)

        The output mirrors the yfinance news shape: a ``## Ticker News`` header
        followed by ``### Title (source: ...)`` sections.
        """
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol) if is_hk(symbol) else symbol

        start_date = kwargs.get("start_date")
        end_date = kwargs.get("end_date")
        ts_start = _to_tushare_date(start_date) if start_date else None
        ts_end = _to_tushare_date(end_date) if end_date else None

        sections: list[str] = []

        # 1. Company announcements (stock-specific, high value)
        try:
            anns = pro.anns_d(ts_code=ts_code, start_date=ts_start, end_date=ts_end)
            if anns is not None and not anns.empty:
                anns = anns.head(20)  # cap to avoid prompt bloat
                lines = ["## Company Announcements (公告)\n"]
                for _, row in anns.iterrows():
                    date = _from_tushare_date(row.get("ann_date", ""))
                    title = row.get("title", "")
                    url = row.get("url", "")
                    lines.append(f"### {title} (source: 巨潮资讯, {date})")
                    if url and pd.notna(url):
                        lines.append(f"Link: {url}")
                    lines.append("")
                sections.append("\n".join(lines))
        except Exception as e:
            logger.info("tushare anns_d failed for %s: %s", ts_code, e)

        # 2. Research reports (stock-specific, high value)
        try:
            reports = pro.research_report(ts_code=ts_code, start_date=ts_start, end_date=ts_end)
            if reports is not None and not reports.empty:
                reports = reports.head(15)
                lines = ["## Analyst Research Reports (研报)\n"]
                for _, row in reports.iterrows():
                    date = _from_tushare_date(row.get("trade_date", ""))
                    title = row.get("title", "")
                    inst = row.get("inst_csname", "")
                    author = row.get("author", "")
                    url = row.get("url", "")
                    rtype = row.get("report_type", "")
                    lines.append(
                        f"### {title} (source: {inst}, {rtype}, {date})"
                    )
                    if author and pd.notna(author):
                        lines.append(f"Author: {author}")
                    if url and pd.notna(url):
                        lines.append(f"Link: {url}")
                    lines.append("")
                sections.append("\n".join(lines))
        except Exception as e:
            logger.info("tushare research_report failed for %s: %s", ts_code, e)

        # 3. Market-wide major news (context, not stock-specific)
        try:
            mnews = pro.major_news(start_date=ts_start, end_date=ts_end)
            if mnews is not None and not mnews.empty:
                mnews = mnews.head(15)
                lines = ["## Recent Market News (市场要闻)\n"]
                for _, row in mnews.iterrows():
                    title = row.get("title", "")
                    src = row.get("src", "")
                    pub = row.get("pub_time", "")
                    lines.append(f"### {title} (source: {src}, {pub})")
                    lines.append("")
                sections.append("\n".join(lines))
        except Exception as e:
            logger.info("tushare major_news failed: %s", e)

        if not sections:
            raise NoMarketDataError(
                symbol, ts_code, "no news/announcements/reports returned from tushare"
            )

        header = (
            f"# {symbol} News & Announcements, "
            f"from {start_date} to {end_date}:\n"
            f"# Source: tushare (anns_d + research_report + major_news)\n\n"
        )
        return header + "\n".join(sections)

    def fetch_global_news(self, **kwargs: Any) -> str:
        """Fetch market-wide financial news from tushare's major_news endpoint.

        major_news returns general A-share market news (no per-stock filter),
        which is a good fit for the global_news capability.
        """
        pro = self._get_pro()
        curr_date = kwargs.get("curr_date")
        look_back = kwargs.get("look_back_days") or 7
        limit = kwargs.get("limit") or 30

        end = pd.Timestamp(curr_date) if curr_date else pd.Timestamp.now()
        start = end - pd.Timedelta(days=int(look_back))
        ts_start = start.strftime("%Y%m%d")
        ts_end = end.strftime("%Y%m%d")

        try:
            df = pro.major_news(start_date=ts_start, end_date=ts_end)
        except Exception as e:
            raise NoMarketDataError("GLOBAL", "GLOBAL", f"tushare major_news failed: {e}") from e

        if df is None or df.empty:
            return f"No global news found from tushare between {start.date()} and {end.date()}"

        df = df.head(int(limit))
        lines = [
            f"## Global A-share Market News, from {start.date()} to {end.date()}:\n",
        ]
        for _, row in df.iterrows():
            title = row.get("title", "")
            src = row.get("src", "")
            pub = row.get("pub_time", "")
            lines.append(f"### {title} (source: {src}, {pub})")
            url = row.get("url", "")
            if url and pd.notna(url):
                lines.append(f"Link: {url}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Insider transactions (10000-point tier)
    # ------------------------------------------------------------------
    def fetch_insider_transactions(self, symbol: str, **kwargs: Any) -> str:
        """Fetch major shareholder trades from tushare's stk_holdertrade.

        stk_holdertrade covers major shareholder increases/decreases (股东增减持),
        which is the A-share equivalent of US insider transactions.
        """
        pro = self._get_pro()
        ts_code = _hk_ts_code(symbol) if is_hk(symbol) else symbol

        curr_date = kwargs.get("curr_date")
        # stk_holdertrade needs a date window; default to the last 2 years.
        end = pd.Timestamp(curr_date) if curr_date else pd.Timestamp.now()
        start = end - pd.Timedelta(days=730)
        ts_start = start.strftime("%Y%m%d")
        ts_end = end.strftime("%Y%m%d")

        try:
            df = pro.stk_holdertrade(
                ts_code=ts_code, start_date=ts_start, end_date=ts_end,
            )
        except Exception as e:
            raise NoMarketDataError(
                symbol, ts_code, f"tushare stk_holdertrade failed: {e}",
            ) from e

        if df is None or df.empty:
            # No shareholder trades in the window — report plainly, matching
            # the yfinance "No insider transactions reported" sentinel.
            return (
                f"No major shareholder trades (股东增减持) reported for "
                f"'{ts_code}' between {start.date()} and {end.date()}."
            )

        # Format as CSV with a header, mirroring yfinance insider output.
        header = (
            f"# Major Shareholder Trades (股东增减持) for {symbol}\n"
            f"# Source: tushare stk_holdertrade\n"
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        csv = df.to_csv(index=False)
        return header + csv

    # ------------------------------------------------------------------
    # Macro data (10000-point tier) — China macro indicators
    # ------------------------------------------------------------------
    # Friendly alias -> (tushare_api, value_column, label, units)
    # cn_cpi:  monthly CPI (全国居民消费价格指数)
    # cn_pmi:  monthly PMI (制造业采购经理指数) — PMI010000 is the headline
    # shibor:  daily Shanghai Interbank Offered Rate
    _CN_MACRO_MAP = {
        "cpi": ("cn_cpi", "nt_val", "China CPI (全国居民消费价格指数)", "Index (上年同月=100)"),
        "pmi": ("cn_pmi", "PMI010000", "China Manufacturing PMI (制造业PMI)", "Index (>50 = expansion)"),
        "shibor": ("shibor", "on", "SHIBOR Overnight Rate", "%"),
        "shibor_on": ("shibor", "on", "SHIBOR Overnight Rate", "%"),
        "shibor_1w": ("shibor", "1w", "SHIBOR 1-Week Rate", "%"),
        "shibor_1m": ("shibor", "1m", "SHIBOR 1-Month Rate", "%"),
        "shibor_3m": ("shibor", "3m", "SHIBOR 3-Month Rate", "%"),
    }

    def fetch_macro_data(self, indicator: str, **kwargs: Any) -> str:
        """Fetch China macro indicators from tushare.

        Supported indicators:
          - cpi        -> cn_cpi (全国CPI, 同比/环比)
          - pmi        -> cn_pmi (制造业PMI headline)
          - shibor     -> shibor (隔夜/1周/1月/3月利率)

        Output mirrors the FRED macro format: header + latest value +
        change-over-window + observation table.
        """
        pro = self._get_pro()
        key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
        mapping = self._CN_MACRO_MAP.get(key)
        if mapping is None:
            return (
                f"Tushare: '{indicator}' is not a supported China macro alias. "
                f"Use: cpi, pmi, shibor, shibor_on, shibor_1w, shibor_1m, shibor_3m."
            )

        api_name, value_col, label, units = mapping
        curr_date = kwargs.get("curr_date")
        look_back = kwargs.get("look_back_days") or 365

        end = pd.Timestamp(curr_date) if curr_date else pd.Timestamp.now()
        start = end - pd.Timedelta(days=int(look_back))

        # cn_cpi / cn_pmi use month format (YYYYMM); shibor uses date (YYYYMMDD).
        # cn_pmi returns 'MONTH' (uppercase); cn_cpi returns 'month' (lowercase).
        if api_name in ("cn_cpi", "cn_pmi"):
            params = dict(start_m=start.strftime("%Y%m"), end_m=end.strftime("%Y%m"))
            date_col = "MONTH" if api_name == "cn_pmi" else "month"
        else:
            params = dict(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
            date_col = "date"

        try:
            df = pro.query(api_name, **params)
        except Exception as e:
            raise NoMarketDataError(
                "MACRO", api_name, f"tushare {api_name} failed: {e}",
            ) from e

        if df is None or df.empty or value_col not in df.columns:
            return (
                f"## Tushare: {label}\n"
                f"- Units: {units}\n"
                f"- Window: {start.date()} to {end.date()}\n\n"
                f"No observations for {api_name}.{value_col} in this window."
            )

        df = df.sort_values(date_col).dropna(subset=[value_col])
        points = [(str(r[date_col]), float(r[value_col])) for _, r in df.iterrows()]

        header = (
            f"## Tushare: {label}\n"
            f"- Units: {units}\n"
            f"- Source: tushare {api_name}\n"
            f"- Window: {start.date()} to {end.date()}\n"
        )

        if not points:
            return header + f"\nNo valid observations in this window.\n"

        first_date, first_val = points[0]
        last_date, last_val = points[-1]
        delta = last_val - first_val
        base = first_val if first_val != 0 else 1.0
        pct = f" ({delta / base * 100:+.2f}%)"
        summary = (
            f"\n**Latest:** {last_val:.2f} ({last_date}) | "
            f"**Change over window:** {delta:+.2f}{pct} "
            f"from {first_val:.2f} ({first_date})\n"
        )

        shown = points[-40:]  # cap table length
        note = ""
        if len(points) > 40:
            note = f"\n_(showing the most recent 40 of {len(points)} observations)_\n"

        table = (
            "\n| Date | Value |\n| --- | --- |\n"
            + "\n".join(f"| {d} | {v:.2f} |" for d, v in shown)
            + "\n"
        )
        return header + summary + note + table
