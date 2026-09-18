"""A-share data backend using akshare.

Provides A-share (Shanghai/Shenzhen/Beijing) market data through akshare,
matching the function signatures and return shapes of ``y_finance.py`` so the
routing layer can substitute them transparently for A-share symbols.

akshare API surface used here:
- ``stock_zh_a_hist``                    daily OHLCV (qfq-adjusted)
- ``stock_individual_info_em``           company fundamentals snapshot
- ``stock_balance_sheet_by_report_em``  balance sheet by report period
- ``stock_cash_flow_sheet_by_report_em`` cash flow statement by report period
- ``stock_profit_sheet_by_report_em``    income statement (a.k.a. profit sheet)

All functions raise ``NoMarketDataError`` / ``VendorRateLimitError`` from
``.errors`` so the symbol-router in ``interface.py`` can treat akshare like
any other vendor (try, fall back, emit the typed sentinel).
"""

import logging
from datetime import datetime
from typing import Annotated

import akshare as ak
import pandas as pd
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from .errors import NoMarketDataError, VendorRateLimitError
from .stockstats_utils import filter_financials_by_date
from .utils import vendor_reachable

logger = logging.getLogger(__name__)

# akshare's web endpoints all live under eastmoney's datacenter; we use this
# only to tell "akshare is unreachable" from "this symbol has no data", mirroring
# yfinance's ``raise_for_empty`` outage detection.
_AKSHARE_HOST = "https://datacenter-web.eastmoney.com"

# Suffixes that mark a ticker as an A-share (Shanghai/Shenzhen/Beijing).
_ASHARE_SUFFIXES = (".SH", ".SZ", ".BJ")


# ---------------------------------------------------------------------------
# Symbol helpers (used by interface.py's symbol-router too)
# ---------------------------------------------------------------------------

def is_ashare(symbol: str) -> bool:
    """True if ``symbol`` carries an A-share exchange suffix (.SH/.SZ/.BJ).

    Case-insensitive. Used by the routing layer to short-circuit A-share
    tickers to this backend regardless of the configured vendor chain.
    """
    if not isinstance(symbol, str) or not symbol:
        return False
    return symbol.upper().endswith(_ASHARE_SUFFIXES)


def strip_suffix(symbol: str) -> str:
    """Strip the exchange suffix: ``"600519.SH"`` -> ``"600519"``.

    akshare's A-share endpoints want the bare 6-digit code (no .SH/.SZ).
    """
    if not isinstance(symbol, str):
        return symbol
    upper = symbol.upper()
    for suffix in _ASHARE_SUFFIXES:
        if upper.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


def _exchange_for(symbol: str) -> str:
    """Return the bare exchange tag ('SH'/'SZ'/'BJ') for an A-share symbol."""
    upper = symbol.upper()
    for suffix in _ASHARE_SUFFIXES:
        if upper.endswith(suffix):
            return suffix[1:]  # strip the leading dot
    return ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _raise_for_empty_akshare(symbol: str, canonical: str, what: str) -> None:
    """Report an empty akshare result as either no-data or an outage.

    akshare swallows most HTTP errors and returns an empty frame, so without
    this an eastmoney outage would read as "this symbol has no {what}".
    Mirrors yfinance's ``raise_for_empty`` in ``stockstats_utils.py``.
    """
    if not vendor_reachable(_AKSHARE_HOST):
        raise VendorRateLimitError(
            f"akshare/eastmoney is unreachable; no {what} was retrieved"
        )
    raise NoMarketDataError(symbol, canonical, f"no {what}")


def _format_ohlcv_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Rename akshare's Chinese columns to yfinance's names and set Date index.

    akshare's ``stock_zh_a_hist`` returns 日期/开盘/收盘/最高/最低/成交量/...;
    yfinance returns Date/Open/High/Low/Close/Adj Close/Volume. The qfq adjust
    already returns forward-adjusted prices, so ``Adj Close`` == ``Close``.
    """
    column_map = {
        "日期": "Date",
        "开盘": "Open",
        "收盘": "Close",
        "最高": "High",
        "最低": "Low",
        "成交量": "Volume",
        "成交额": "Amount",
        "振幅": "Amplitude",
        "涨跌幅": "PctChange",
        "涨跌额": "Change",
        "换手率": "TurnoverRate",
    }
    renamed = raw.rename(columns=column_map)

    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in renamed.columns]
    renamed = renamed[keep]

    if "Date" not in renamed.columns:
        raise NoMarketDataError(detail="akshare OHLCV response missing Date column")

    renamed["Date"] = pd.to_datetime(renamed["Date"], errors="coerce")
    renamed = renamed.dropna(subset=["Date"])

    # qfq adjust makes Close == Adj Close; yfinance exposes both columns.
    renamed["Adj Close"] = renamed["Close"]

    renamed = renamed.set_index("Date").sort_index()

    # Round numeric columns to 2dp, mirroring yfinance formatting so the
    # analyst prompt sees identically-shaped numbers across vendors.
    for col in ["Open", "High", "Low", "Close", "Adj Close"]:
        if col in renamed.columns:
            renamed[col] = pd.to_numeric(renamed[col], errors="coerce").round(2)

    return renamed


def _fetch_ohlcv_range(symbol: str, start_date: str, end_date_inclusive: str) -> pd.DataFrame:
    """Fetch and format an inclusive [start, end] range of A-share OHLCV."""
    code = strip_suffix(symbol)
    # akshare wants YYYYMMDD (no dashes) and treats end_date as INCLUSIVE,
    # so no +1 day shift is needed (unlike yfinance's exclusive end).
    ak_start = start_date.replace("-", "")
    ak_end = end_date_inclusive.replace("-", "")

    try:
        raw = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=ak_start,
            end_date=ak_end,
            adjust="qfq",
        )
    except Exception as e:
        # akshare raises generic Exception on network/HTTP errors; the router
        # treats NoMarketDataError as "no data from this vendor, try next".
        raise NoMarketDataError(symbol, code, f"akshare OHLCV fetch failed: {e}") from e

    if raw is None or raw.empty:
        _raise_for_empty_akshare(symbol, code, f"rows between {start_date} and {end_date_inclusive}")
    return _format_ohlcv_frame(raw)


# ---------------------------------------------------------------------------
# 1. Daily OHLCV - matches yfinance.get_YFin_data_online
# ---------------------------------------------------------------------------

def get_akshare_stock_data(
    symbol: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch A-share daily OHLCV via akshare (qfq-adjusted).

    Returns a header + CSV string in the same shape as
    ``y_finance.get_YFin_data_online``: a ``Date``-indexed frame with
    ``Open/High/Low/Close/Adj Close/Volume`` columns.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = _fetch_ohlcv_range(symbol, start_date, end_date)

    csv_string = data.to_csv()
    header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


# ---------------------------------------------------------------------------
# 2. Technical indicators - matches yfinance.get_stock_stats_indicators_window
# ---------------------------------------------------------------------------

# Indicator descriptions copied from y_finance.py so the analyst prompts see
# the same explanatory text regardless of which vendor served the call.
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
    ),
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes. "
        "Tips: Confirm with other indicators in low-volatility or sideways markets."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades. "
        "Tips: Should be part of a broader strategy to avoid false positives."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early. "
        "Tips: Can be volatile; complement with additional filters in fast-moving markets."
    ),
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
        "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
    ),
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement. "
        "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones. "
        "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions. "
        "Tips: Use additional analysis to avoid false reversal signals."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
        "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
    ),
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data. "
        "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    ),
    "mfi": (
        "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
        "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
    ),
}


def _load_ohlcv_window(symbol: str, curr_date: str, years: int = 5) -> pd.DataFrame:
    """Fetch ``years`` years of A-share OHLCV up to ``curr_date`` for indicators.

    Mirrors ``stockstats_utils.load_ohlcv`` but pulls from akshare instead of
    yfinance. Rows after ``curr_date`` are dropped to prevent look-ahead.
    """
    curr_dt = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(curr_dt):
        raise NoMarketDataError(symbol, None, f"bad curr_date: {curr_date!r}")
    curr_dt = curr_dt.normalize()

    start_dt = curr_dt - pd.DateOffset(years=years)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = curr_dt.strftime("%Y-%m-%d")

    data = _fetch_ohlcv_range(symbol, start_str, end_str)
    data = data[data.index <= curr_dt]
    if data.empty:
        _raise_for_empty_akshare(symbol, strip_suffix(symbol), f"OHLCV up to {curr_date}")
    return data.reset_index()


def get_akshare_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Compute a technical indicator over a look-back window using akshare OHLCV.

    Mirrors ``y_finance.get_stock_stats_indicators_window`` signature and
    return format. Uses ``stockstats`` (pure pandas) so no ta-lib dependency
    is required - akshare doesn't bundle ta-lib.
    """
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: "
            f"{list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    try:
        data = _load_ohlcv_window(symbol, curr_date)
        # stockstats.wrap expects a DataFrame with a Date column and OHLCV.
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        # Trigger stockstats to compute the indicator for every row at once.
        df[indicator]

        # Walk back calendar days from curr_date; non-trading days report N/A,
        # matching the yfinance implementation's row layout.
        indicator_lookup = (
            df.set_index("Date")[indicator].to_dict() if "Date" in df.columns else {}
        )

        date_values = []
        current_dt = curr_date_dt
        while current_dt >= before:
            date_str = current_dt.strftime("%Y-%m-%d")
            if date_str in indicator_lookup:
                value = indicator_lookup[date_str]
                indicator_value = "N/A" if pd.isna(value) else str(value)
            else:
                indicator_value = "N/A: Not a trading day (weekend or holiday)"
            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        ind_string = "".join(f"{d}: {v}\n" for d, v in date_values)
    except (NoMarketDataError, VendorRateLimitError):
        raise  # Let the router try the next vendor / emit the sentinel.
    except Exception as e:
        # An empty string in the table reads as "no value that day"; raise so
        # the router can react, mirroring yfinance's behaviour.
        raise NoMarketDataError(
            symbol, symbol, f"{indicator} could not be read for {curr_date}: {e}"
        ) from e

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + _INDICATOR_DESCRIPTIONS.get(indicator, "No description available.")
    )
    return result_str


# ---------------------------------------------------------------------------
# 3. Fundamentals - matches yfinance.get_fundamentals
# ---------------------------------------------------------------------------

# Map akshare's stock_individual_info_em "item" labels to the yfinance-shape
# field names the analyst prompt already knows how to read.
_FUNDAMENTALS_FIELD_MAP = {
    "股票简称": "Name",
    "行业": "Sector",
    "总市值": "Market Cap",
    "流通市值": "Float Market Cap",
    "市盈率(TTM)": "PE Ratio (TTM)",
    "市盈率(静)": "PE Ratio (Static)",
    "市盈率(动态)": "Forward PE",
    "市净率": "Price to Book",
    "总股本": "Shares Outstanding",
    "流通股": "Float Shares",
    "换手率": "Turnover Rate",
    "量比": "Volume Ratio",
    "涨跌幅": "Pct Change",
    "最高": "Day High",
    "最低": "Day Low",
    "今开": "Open",
    "昨收": "Previous Close",
    "60日涨跌幅": "60D Pct Change",
    "年初至今涨跌幅": "YTD Pct Change",
    "上市时间": "Listing Date",
}


def get_akshare_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    curr_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None,
) -> str:
    """Get A-share company fundamentals overview via akshare.

    ``stock_individual_info_em`` is a present-day snapshot with no historical
    vintage, so a past ``curr_date`` withholds it (the analyst prompt knows
    this is a live-only field). Matches ``y_finance.get_fundamentals`` return
    shape: a header followed by ``"Label: value"`` lines.
    """
    # No historical point-in-time data for A-share fundamentals via akshare:
    # a past curr_date would be served a today-snapshot, so withhold it.
    from .date_window import withhold_live_profile
    withheld = withhold_live_profile(curr_date, ticker)
    if withheld:
        return withheld

    code = strip_suffix(ticker)
    try:
        raw = ak.stock_individual_info_em(symbol=code)
    except Exception as e:
        raise NoMarketDataError(ticker, code, f"fundamentals unavailable: {e}") from e

    if raw is None or raw.empty or "item" not in raw.columns:
        _raise_for_empty_akshare(ticker, code, "fundamentals")

    info = dict(zip(raw["item"].astype(str), raw["value"]))

    lines = []
    for cn, en_label in _FUNDAMENTALS_FIELD_MAP.items():
        if cn in info and info[cn] not in (None, "", "nan", "NaN"):
            lines.append(f"{en_label}: {info[cn]}")

    if not lines:
        raise NoMarketDataError(ticker, code, "no fundamental fields returned")

    header = f"# Company Fundamentals for {ticker}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)


# ---------------------------------------------------------------------------
# 4-6. Financial statements - match yfinance.get_balance_sheet / cashflow / income_statement
# ---------------------------------------------------------------------------

# This vendor dates a statement by the period it covers, not by the day it was
# filed, and carries no filing date to do better. Same caveat as yfinance.
_PERIOD_END_VINTAGE = (
    "# Periods are cut at the fiscal period end; this vendor does not report "
    "filing dates, so the most recent period may not have been published yet.\n\n"
)


def _filter_reports_by_freq(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Keep only rows matching the requested fiscal frequency.

    akshare returns all reports (quarterly + annual) interleaved. We split by
    month-of-period: 03/06/09/12 = quarterly, 12 = annual. yfinance's API
    exposes these as separate endpoints (quarterly_balance_sheet vs balance_sheet).
    """
    if df.empty or "报告期" not in df.columns:
        return df
    periods = pd.to_datetime(df["报告期"], errors="coerce")
    month = periods.dt.month
    if freq.lower() == "quarterly":
        mask = month.isin([3, 6, 9, 12])
    else:
        mask = month == 12
    return df[mask]


def _format_statement(raw: pd.DataFrame, freq: str, curr_date: str | None) -> pd.DataFrame:
    """Transpose akshare's "one row per period" frame to yfinance's shape:
    index = line items, columns = fiscal period end dates.
    """
    if raw is None or raw.empty or "报告期" not in raw.columns:
        return pd.DataFrame()

    df = _filter_reports_by_freq(raw, freq)
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    # Drop metadata columns that should not become line items.
    drop_cols = [c for c in ["更新日期", "报表类型", "公司代码", "公司名称"] if c in df.columns]
    df = df.drop(columns=drop_cols, errors="ignore")
    df = df.drop_duplicates(subset=["报告期"]).set_index("报告期")
    df.columns.name = None

    # Transpose: rows = line items, columns = period end dates.
    transposed = df.T
    transposed.columns = pd.to_datetime(transposed.columns, errors="coerce")
    return filter_financials_by_date(transposed, curr_date)


def _get_statement_em(
    fetch,
    *,
    ticker: str,
    freq: str,
    curr_date: str | None,
    label: str,
) -> str:
    """Shared body for balance sheet / cashflow / income statement."""
    code = strip_suffix(ticker)
    try:
        raw = fetch(symbol=code)
    except Exception as e:
        raise NoMarketDataError(ticker, code, f"{label} unavailable: {e}") from e

    data = _format_statement(raw, freq, curr_date)
    if data.empty:
        _raise_for_empty_akshare(ticker, code, f"{label} data")

    csv_string = data.to_csv()
    header = f"# {label} data for {ticker} ({freq})\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    header += _PERIOD_END_VINTAGE
    return header + csv_string


def get_akshare_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get A-share balance sheet via akshare."""
    return _get_statement_em(
        ak.stock_balance_sheet_by_report_em,
        ticker=ticker, freq=freq, curr_date=curr_date, label="Balance Sheet",
    )


def get_akshare_cashflow(
    ticker: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get A-share cash flow statement via akshare."""
    return _get_statement_em(
        ak.stock_cash_flow_sheet_by_report_em,
        ticker=ticker, freq=freq, curr_date=curr_date, label="Cash Flow",
    )


def get_akshare_income_statement(
    ticker: Annotated[str, "ticker symbol of the company, e.g. 600519.SH"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get A-share income statement via akshare.

    akshare calls the income statement the "profit sheet" - same statement,
    different naming convention. We expose it under the yfinance name so the
    routing layer is symmetric across vendors.
    """
    return _get_statement_em(
        ak.stock_profit_sheet_by_report_em,
        ticker=ticker, freq=freq, curr_date=curr_date, label="Income Statement",
    )
