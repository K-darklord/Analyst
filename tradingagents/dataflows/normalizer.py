"""Output normalization helpers for data source adapters.

Different vendors return different shapes:
- yfinance returns a pandas DataFrame with English column names
  (Open/High/Low/Close/Adj Close/Volume)
- akshare returns a DataFrame with Chinese column names
  (开盘/最高/最低/收盘/成交量)
- tushare returns DataFrames with English names but different cases
  (open/high/low/close/vol)

The agents consume CSV / markdown strings in their LLM prompts, so the
adapter layer must produce a consistent output regardless of source. This
module provides:

1. ``normalize_ohlcv_columns(df)`` — rename vendor-specific column names to
   the canonical OHLCV schema. Returns a new DataFrame.
2. ``to_ohlcv_csv(df, symbol, start_date, end_date)`` — format an OHLCV
   DataFrame as the CSV string the agents expect (with the header block
   that yfinance's get_YFin_data_online produces).
3. ``to_statement_markdown(df, statement_type, symbol)`` — format a
   financial-statement DataFrame as a readable markdown block.

Phase 1 only ships the OHLCV helpers because that's what the existing
yfinance / akshare adapters already produce. Phase 2 (tushare adapter)
will use these to format raw tushare DataFrames into the same shape.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)


# Canonical OHLCV column order. Adapters should produce DataFrames whose
# column set is a superset of (or exactly) this list.
OHLCV_COLUMNS: list[str] = [
    "Open", "High", "Low", "Close", "Adj Close", "Volume",
]


# Vendor column aliases. Keys are the names a vendor returns; values are
# the canonical OHLCV names. Extend as new vendors ship.
_COLUMN_ALIASES: dict[str, str] = {
    # English lowercase / mixed-case variants
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "adj close": "Adj Close",
    "adjclose": "Adj Close",
    "adjusted": "Adj Close",
    "volume": "Volume",
    "vol": "Volume",
    # Chinese (akshare)
    "开盘": "Open",
    "最高": "High",
    "最低": "Low",
    "收盘": "Close",
    "成交量": "Volume",
    # Tushare daily bars
    "trade_date": "Date",   # special: index column, not OHLCV
}


def normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename vendor-specific columns to the canonical OHLCV schema.

    Returns a new DataFrame. Unknown columns are dropped. The Date column
    (if present as ``trade_date``) is moved to the index and renamed
    ``Date``.
    """
    if df is None or df.empty:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    renamed = df.copy()

    # Lowercase the existing column names so the alias table matches
    # case-insensitively (yfinance uses Title Case, tushare uses lowercase,
    # akshare uses Chinese — we treat them all uniformly).
    col_map: dict[str, str] = {}
    for col in renamed.columns:
        key = str(col).strip().lower()
        if key in _COLUMN_ALIASES:
            col_map[col] = _COLUMN_ALIASES[key]
    if col_map:
        renamed = renamed.rename(columns=col_map)

    # Date handling: tushare uses 'trade_date' as a column (YYYYMMDD string),
    # yfinance / akshare put it in the index. Normalize: if a Date column
    # exists, set it as the index.
    if "Date" in renamed.columns:
        # tushare dates come as 'YYYYMMDD' strings; convert for consistency.
        renamed["Date"] = pd.to_datetime(renamed["Date"], format="%Y%m%d", errors="co")
        renamed = renamed.set_index("Date")

    # Drop any columns we didn't recognize — the agents expect a tight
    # OHLCV schema, and stray vendor-specific fields would leak into
    # the LLM prompt as noise.
    keep = [c for c in OHLCV_COLUMNS if c in renamed.columns]
    return renamed[keep].copy()


def to_ohlcv_csv(
    df: pd.DataFrame,
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    source: str = "unknown",
) -> str:
    """Format an OHLCV DataFrame as the CSV string the agents expect.

    Mirrors the header format produced by yfinance's get_YFin_data_online
    so agent prompts look identical regardless of which vendor served the
    request. Rounding to 2 decimals matches the existing yfinance path.
    """
    if df is None or df.empty:
        return ""

    out = df.copy()
    # Round numeric OHLCV columns to 2 decimals (volume stays integer-ish).
    numeric_round = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_round:
        if col in out.columns:
            out[col] = out[col].round(2)

    # Remove timezone info from the index for cleaner output
    if isinstance(out.index, pd.DatetimeIndex) and out.index.tz is not None:
        out.index = out.index.tz_localize(None)

    csv_string = out.to_csv()

    header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Source: {source}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def to_statement_markdown(
    df: pd.DataFrame,
    statement_type: str,
    symbol: str,
    *,
    source: str = "unknown",
    curr_date: str | None = None,
) -> str:
    """Format a financial-statement DataFrame as a markdown block.

    ``statement_type`` is one of: 'fundamentals', 'balance_sheet',
    'cashflow', 'income_statement'. The output is the CSV dump of the
    DataFrame prefixed with a short header — same shape as the existing
    yfinance / akshare statement functions, so agent prompts don't change.
    """
    if df is None or df.empty:
        return ""

    out = df.copy()
    # Statement DataFrames often carry wide numeric columns; round floats
    # to 2 decimals for readability in the LLM prompt.
    for col in out.select_dtypes(include=["float", "float64"]).columns:
        out[col] = out[col].round(2)

    csv_string = out.to_csv()

    header = f"# {statement_type} for {symbol}\n"
    header += f"# Source: {source}\n"
    if curr_date:
        header += f"# As of: {curr_date}\n"
    header += f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


__all__ = [
    "OHLCV_COLUMNS",
    "normalize_ohlcv_columns",
    "to_ohlcv_csv",
    "to_statement_markdown",
]
