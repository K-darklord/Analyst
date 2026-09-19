"""Data fetching layer for the Strategist.

Fetches daily OHLCV for a basket of ETFs and returns a wide DataFrame of
log-returns indexed by date. Supports US ETFs (yfinance) and A-share ETFs
(akshare), with graceful fallback when a source is rate-limited.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_yfinance_etf(ticker: str, start: str, end: str) -> pd.Series:
    """Fetch adjusted close for one US ETF via yfinance."""
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        return pd.Series(dtype=float)
    # yfinance may return MultiIndex columns for single ticker
    close = raw["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.rename(ticker)


def _fetch_akshare_etf(code: str, start: str, end: str) -> pd.Series:
    """Fetch close for one A-share ETF via akshare (eastmoney)."""
    import akshare as ak

    try:
        raw = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="qfq",
        )
    except Exception as e:
        logger.warning("akshare ETF %s failed: %s", code, e)
        return pd.Series(dtype=float)
    if raw is None or raw.empty:
        return pd.Series(dtype=float)
    raw["日期"] = pd.to_datetime(raw["日期"])
    raw = raw.set_index("日期").sort_index()
    return raw["收盘"].rename(code).astype(float)



def _fetch_tushare_sw(ts_code: str, start: str, end: str) -> pd.Series:
    """Fetch close for one 申万 sector index via tushare sw_daily."""
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    os.environ.setdefault("NO_PROXY", "*")
    import tushare as ts

    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        return pd.Series(dtype=float)
    ts.set_token(token)
    pro = ts.pro_api()
    try:
        df = pro.sw_daily(
            ts_code=ts_code,
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
        )
    except Exception as e:
        logger.warning("tushare sw_daily %s failed: %s", ts_code, e)
        return pd.Series(dtype=float)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date").sort_index()
    return df["close"].rename(ts_code).astype(float)


def fetch_etf_basket(
    etfs: list[tuple[str, str, str]],
    start: str,
    end: str,
) -> pd.DataFrame:
    """Fetch close prices for a basket of ETFs.

    Parameters
    ----------
    etfs : list of (ticker, vendor, name)
    start, end : ISO date strings (YYYY-MM-DD)

    Returns
    -------
    pd.DataFrame
        Wide frame of adjusted close prices, columns = ticker, index = date.
        ETFs that fail to fetch are dropped (with a warning).
    """
    series: list[pd.Series] = []
    for ticker, vendor, _name in etfs:
        try:
            if vendor == "yfinance":
                s = _fetch_yfinance_etf(ticker, start, end)
            elif vendor == "akshare":
                s = _fetch_akshare_etf(ticker, start, end)
            elif vendor == "tushare_sw":
                s = _fetch_tushare_sw(ticker, start, end)
            else:
                logger.warning("Unknown vendor %s for %s, skipping", vendor, ticker)
                continue
            if s is not None and not s.empty:
                series.append(s)
            else:
                logger.warning("No data for %s (%s)", ticker, vendor)
        except Exception as e:
            logger.warning("Fetch failed for %s: %s", ticker, e)

    if not series:
        raise RuntimeError("All ETF fetches failed — check network / rate limits")

    df = pd.concat(series, axis=1).sort_index()
    # Forward-fill small gaps (holidays), then drop rows that are mostly NaN.
    df = df.ffill(limit=3)
    df = df.dropna(how="all")
    return df


def to_log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Convert a price frame to daily log-returns, dropping the first row."""
    log_ret = np.log(prices / prices.shift(1))
    return log_ret.iloc[1:]


def build_returns_matrix(
    etfs: list[tuple[str, str, str]],
    end_date: str,
    lookback_days: int = 365,
) -> pd.DataFrame:
    """Fetch prices for the ETF basket and return a log-returns frame.

    Parameters
    ----------
    etfs : list of (ticker, vendor, name)
    end_date : ISO date string — the most recent date to include.
    lookback_days : calendar days of history to fetch (>= lookback_window).

    Returns
    -------
    pd.DataFrame of daily log-returns, columns = ticker.
    """
    end_dt = pd.Timestamp(end_date)
    start_dt = end_dt - timedelta(days=lookback_days)
    prices = fetch_etf_basket(etfs, start_dt.strftime("%Y-%m-%d"), end_date)
    returns = to_log_returns(prices)
    # Drop tickers with too many NaN (more than 20% of rows).
    nan_frac = returns.isna().mean()
    keep = nan_frac[nan_frac < 0.2].index
    returns = returns[keep].dropna(how="all")
    logger.info(
        "Built returns matrix: %d ETFs x %d days", returns.shape[1], returns.shape[0]
    )
    return returns
