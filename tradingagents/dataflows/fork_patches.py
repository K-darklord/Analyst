"""Fork extension: patch load_ohlcv to route A-share and HK symbols through
registry-configured vendors (tushare / akshare) instead of yfinance.

The upstream ``stockstats_utils.load_ohlcv`` calls ``yf.download()``
directly, bypassing the DataSourceRegistry. For A-share symbols (.SH/.SZ/.BJ)
yfinance converts the suffix to .SS/.SZ and frequently gets rate-limited,
causing the entire Analyst pipeline to fail even when tushare is
available. For HK symbols (.HK), the upstream ``normalize_symbol`` strips
leading zeros (``01810.HK`` -> ``1810.HK``) and yfinance frequently
returns 429 rate-limited -- the pipeline fails even when akshare's
``stock_hk_daily`` (Sina source) works reliably.

This module monkey-patches ``load_ohlcv`` (and all module-level references
to it) so that:
  - A-share symbols are served by tushare's ``pro.daily()`` endpoint.
  - HK symbols are served by akshare's ``stock_hk_daily`` (Sina source).
  - US symbols are unaffected -- they pass through to the original yfinance.

This is a FORK EXTENSION file -- it does not exist in upstream and carries
zero merge-conflict risk. It is imported from ``interface.py`` (the only
upstream file the fork modifies) via the registry delegation block.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


def _tushare_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    """Fetch A-share OHLCV from tushare, matching load_ohlcv's output shape.

    Returns a DataFrame with columns: Date, Open, High, Low, Close,
    Adj Close, Volume -- the same shape yfinance's download produces.
    """
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN not set")

    os.environ["TUSHARE_TOKEN"] = token
    pro = ts.pro_api(timeout=30)

    curr_dt = pd.to_datetime(curr_date)
    start_dt = curr_dt - pd.DateOffset(years=years)
    start_str = start_dt.strftime("%Y%m%d")
    end_str = curr_dt.strftime("%Y%m%d")

    df = pro.daily(ts_code=symbol, start_date=start_str, end_date=end_str)
    if df is None or df.empty:
        from .errors import NoMarketDataError
        raise NoMarketDataError(symbol, symbol, "no rows from tushare daily")

    # Tushare returns descending by trade_date; sort ascending.
    df = df.sort_values("trade_date").reset_index(drop=True)

    # Build output matching yfinance's shape.
    out = pd.DataFrame({
        "Date": pd.to_datetime(df["trade_date"]),
        "Open": df["open"].astype(float),
        "High": df["high"].astype(float),
        "Low": df["low"].astype(float),
        "Close": df["close"].astype(float),
        # Tushare daily doesn't have adj_close; use close as proxy.
        "Adj Close": df["close"].astype(float),
        "Volume": df["vol"].astype(float),
    })

    # Filter to curr_date to prevent look-ahead bias.
    out = out[out["Date"] <= curr_dt].reset_index(drop=True)
    return out


def _akshare_hk_ohlcv(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    """Fetch HK OHLCV from akshare's Sina source, matching load_ohlcv's shape.

    Uses ``ak.stock_hk_daily`` (Sina) instead of ``ak.stock_hk_hist``
    (eastmoney push2his) because the eastmoney endpoint is often
    WAF-blocked.  Sina returns all history; we filter by date range.
    """
    import akshare as ak
    from .errors import NoMarketDataError

    # Strip .HK suffix -> bare numeric code akshare expects.
    code = symbol[:-3] if symbol.upper().endswith(".HK") else symbol

    curr_dt = pd.to_datetime(curr_date).normalize()
    start_dt = curr_dt - pd.DateOffset(years=years)

    try:
        raw = ak.stock_hk_daily(symbol=code, adjust="qfq")
    except Exception as e:
        raise NoMarketDataError(
            symbol, code, f"akshare HK OHLCV fetch failed: {e}"
        ) from e

    if raw is None or raw.empty:
        raise NoMarketDataError(symbol, code, "no rows from akshare stock_hk_daily")

    # stock_hk_daily returns: date, open, high, low, close, volume, amount
    raw = raw.rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    raw["Date"] = pd.to_datetime(raw["Date"], errors="coerce")
    raw = raw.dropna(subset=["Date"])

    # Filter to curr_date to prevent look-ahead bias.
    raw = raw[raw["Date"] <= curr_dt].copy()
    if raw.empty:
        raise NoMarketDataError(
            symbol, code, f"no rows up to {curr_date}"
        )

    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
    out = raw[keep].reset_index(drop=True).copy()
    out["Adj Close"] = out["Close"]
    # Match yfinance formatting (2dp).
    for col in ["Open", "High", "Low", "Close", "Adj Close"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(2)
    return out


def _patch_load_ohlcv_for_ashare_and_hk() -> None:
    """Monkey-patch load_ohlcv to route A-share through tushare and HK through akshare.

    Patches the function in stockstats_utils (the canonical location)
    AND in every module that imported it by name (y_finance,
    market_data_validator) so all call sites see the patched version.
    """
    from . import stockstats_utils
    from . import y_finance
    from . import market_data_validator
    from .ticker_router import is_ashare, is_hk

    _original = stockstats_utils.load_ohlcv

    def _patched_load_ohlcv(symbol: str, curr_date: str, fill_gaps: bool = True):
        if is_ashare(symbol):
            logger.info("load_ohlcv: routing A-share %s through tushare", symbol)
            data = _tushare_ohlcv(symbol, curr_date)
            if fill_gaps:
                from .stockstats_utils import _fill_price_gaps
                data = _fill_price_gaps(data)
            return data
        if is_hk(symbol):
            logger.info("load_ohlcv: routing HK %s through akshare (Sina)", symbol)
            data = _akshare_hk_ohlcv(symbol, curr_date)
            if fill_gaps:
                from .stockstats_utils import _fill_price_gaps
                data = _fill_price_gaps(data)
            return data
        # US / unknown: original yfinance path.
        return _original(symbol, curr_date, fill_gaps)

    # Patch the canonical location and every module-level import.
    stockstats_utils.load_ohlcv = _patched_load_ohlcv
    y_finance.load_ohlcv = _patched_load_ohlcv
    market_data_validator.load_ohlcv = _patched_load_ohlcv

    # Also patch akshare_backend._load_ohlcv_window, which is called by
    # get_akshare_stock_stats_indicators_window (the akshare implementation
    # of get_indicators). route_to_vendor routes A-share get_indicators
    # to the akshare vendor in the legacy chain, which calls this function.
    from . import akshare_backend
    def _patched_load_ohlcv_window(symbol, curr_date, years=5):
        if is_ashare(symbol):
            logger.info("_load_ohlcv_window: routing A-share %s through tushare", symbol)
            return _tushare_ohlcv(symbol, curr_date, years=years)
        if is_hk(symbol):
            logger.info("_load_ohlcv_window: routing HK %s through akshare (Sina)", symbol)
            return _akshare_hk_ohlcv(symbol, curr_date, years=years)
        orig = getattr(akshare_backend._load_ohlcv_window, "__wrapped__", None)
        if orig is not None:
            return orig(symbol, curr_date, years)
        # Fallback: re-implement via the HK/A-share helpers if the original
        # was already replaced; otherwise call it directly.
        from .akshare_backend import _load_ohlcv_window as _cur
        if _cur is _patched_load_ohlcv_window:
            # Already patched once; nothing else to call.
            from .errors import NoMarketDataError
            raise NoMarketDataError(symbol, symbol, "no original _load_ohlcv_window available")
        return _cur(symbol, curr_date, years)
    akshare_backend._load_ohlcv_window = _patched_load_ohlcv_window

    logger.info(
        "Fork patch applied: load_ohlcv + _load_ohlcv_window route "
        "A-share -> tushare, HK -> akshare"
    )


# Apply the patch once at import time.
_patch_load_ohlcv_for_ashare_and_hk()
