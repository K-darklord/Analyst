"""State file parsing, watchlist storage and reversal detection.

Reads JSON state files written by TradingAgents at
``~/.tradingagents/logs/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<YYYY-MM-DD>.json``.

NOTE: per the project README the canonical path is
``~/.tradingagents/<TICKER>/TradingAgentsStrategy_logs/...`` but on disk the
verified location is ``~/.tradingagents/logs/<TICKER>/...``. We probe both.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
TA_HOME = os.path.expanduser("~/.tradingagents")
DASHBOARD_HOME = os.path.join(TA_HOME, "dashboard")
WATCHLIST_FILE = os.path.join(DASHBOARD_HOME, "watchlist.json")
LAST_RUN_FILE = os.path.join(DASHBOARD_HOME, "last_run_status.json")

# Two candidate state dirs -- verified layout uses ``logs/<TICKER>`` but the
# README documents ``<TICKER>``. We check both, ``logs/`` first.
_STATE_DIR_CANDIDATES = [
    os.path.join(TA_HOME, "logs", "{ticker}", "TradingAgentsStrategy_logs"),
    os.path.join(TA_HOME, "{ticker}", "TradingAgentsStrategy_logs"),
]


def _state_dir(ticker: str) -> str:
    for tmpl in _STATE_DIR_CANDIDATES:
        candidate = tmpl.format(ticker=ticker)
        if os.path.isdir(candidate):
            return candidate
    # Fall back to the verified path even if it doesn't exist yet.
    return _STATE_DIR_CANDIDATES[0].format(ticker=ticker)


def _state_file(ticker: str, date: str) -> str:
    return os.path.join(_state_dir(ticker), f"full_states_log_{date}.json")


# ---------------------------------------------------------------------------
# Date helper
# ---------------------------------------------------------------------------
def get_today_str() -> str:
    """Return today's date as ``YYYY-MM-DD``."""
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Watchlist storage
# ---------------------------------------------------------------------------
def _ensure_watchlist_file() -> None:
    os.makedirs(DASHBOARD_HOME, exist_ok=True)
    if not os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump({"tickers": ["NVDA"]}, f, indent=2)


def load_watchlist() -> list[str]:
    """Return the list of tracked tickers (uppercase, deduped)."""
    _ensure_watchlist_file()
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ["NVDA"]
    tickers = data.get("tickers") or ["NVDA"]
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        tu = str(t).strip().upper()
        if tu and tu not in seen:
            seen.add(tu)
            out.append(tu)
    return out or ["NVDA"]


def save_watchlist(tickers: list[str]) -> None:
    _ensure_watchlist_file()
    seen: set[str] = set()
    cleaned: list[str] = []
    for t in tickers:
        tu = str(t).strip().upper()
        if tu and tu not in seen:
            seen.add(tu)
            cleaned.append(tu)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"tickers": cleaned}, f, indent=2)


def add_ticker(ticker: str) -> None:
    tickers = load_watchlist()
    tu = ticker.strip().upper()
    if tu and tu not in tickers:
        tickers.append(tu)
        save_watchlist(tickers)


def remove_ticker(ticker: str) -> None:
    tu = ticker.strip().upper()
    tickers = [t for t in load_watchlist() if t != tu]
    save_watchlist(tickers)


# ---------------------------------------------------------------------------
# State listing / loading
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$")


def list_states_for_ticker(ticker: str) -> list[str]:
    """Return list of date strings (YYYY-MM-DD) for ticker, sorted desc."""
    folder = _state_dir(ticker)
    if not os.path.isdir(folder):
        return []
    dates: list[str] = []
    for name in os.listdir(folder):
        m = _DATE_RE.search(name)
        if not m:
            continue
        dates.append(m.group(1))
    dates.sort(reverse=True)
    return dates


def load_state(ticker: str, date: str) -> Optional[dict]:
    """Load raw state JSON for a ticker+date. Returns None if missing."""
    path = _state_file(ticker, date)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def latest_state(ticker: str) -> tuple[Optional[dict], Optional[str]]:
    """Return ``(state_dict, date_str)`` for the most recent run."""
    dates = list_states_for_ticker(ticker)
    if not dates:
        return None, None
    latest = dates[0]
    return load_state(ticker, latest), latest


# ---------------------------------------------------------------------------
# Rating parser
# ---------------------------------------------------------------------------
_RATING_LEVELS = {
    "STRONG BUY": 2,
    "BUY": 1,
    "OVERWEIGHT": 1,
    "HOLD": 0,
    "NEUTRAL": 0,
    "UNDERWEIGHT": -1,
    "SELL": -1,
    "STRONG SELL": -2,
}

# Longer phrases first so "Strong Buy" matches before "Buy".
_RATING_PHRASES = sorted(_RATING_LEVELS.keys(), key=len, reverse=True)

_RATING_LINE_RE = re.compile(
    r"^\s*\*{0,2}\s*(Rating|Recommendation|Action)\s*\*{0,2}\s*:\s*(.+?)\s*\*{0,2}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_rating(final_trade_decision: str) -> tuple[str, Optional[int]]:
    """Parse ``**Rating**: Overweight`` (and ``Recommendation``/``Action``
    variants) from the final-trade-decision markdown.

    Returns ``(rating_text, level)`` where ``level`` is the integer signal
    (-2..+2). Returns ``("Unknown", None)`` when no parseable rating found.
    """
    if not final_trade_decision:
        return ("Unknown", None)
    text = final_trade_decision.strip()
    for m in _RATING_LINE_RE.finditer(text):
        raw = m.group(2).strip().strip("*").strip()
        norm = _normalize_rating_text(raw)
        for phrase in _RATING_PHRASES:
            if phrase in norm:
                return (_titlecase_rating(phrase), _RATING_LEVELS[phrase])
        return (raw, None)
    return ("Unknown", None)


def _normalize_rating_text(raw: str) -> str:
    cleaned = raw.strip().strip("*").strip()
    cleaned = re.sub(r"[\*`_]+$", "", cleaned).strip()
    return cleaned.upper()


def _titlecase_rating(phrase: str) -> str:
    return phrase.title()


# ---------------------------------------------------------------------------
# Reversal detection
# ---------------------------------------------------------------------------
def detect_reversal(ticker: str) -> dict:
    """Compare the two most recent state files for ``ticker``.

    Returns a dict with current/previous rating+level, the reversal type
    (``none``/``mild``/``strong``) and the two dates.
    """
    dates = list_states_for_ticker(ticker)
    result: dict = {
        "current_rating": "Unknown",
        "current_level": None,
        "previous_rating": None,
        "previous_level": None,
        "reversal_type": "none",
        "previous_date": None,
        "current_date": None,
    }
    if not dates:
        return result
    current_date = dates[0]
    result["current_date"] = current_date
    current_state = load_state(ticker, current_date) or {}
    crating, clevel = parse_rating(current_state.get("final_trade_decision", ""))
    result["current_rating"] = crating
    result["current_level"] = clevel

    if len(dates) < 2:
        return result
    previous_date = dates[1]
    result["previous_date"] = previous_date
    previous_state = load_state(ticker, previous_date) or {}
    prating, plevel = parse_rating(previous_state.get("final_trade_decision", ""))
    result["previous_rating"] = prating
    result["previous_level"] = plevel

    if clevel is None or plevel is None:
        result["reversal_type"] = "none"
        return result

    if (clevel > 0 and plevel < 0) or (clevel < 0 and plevel > 0):
        result["reversal_type"] = "strong"
    elif clevel == plevel:
        result["reversal_type"] = "none"
    elif abs(clevel - plevel) >= 2:
        if (clevel >= 0 and plevel >= 0) or (clevel <= 0 and plevel <= 0):
            result["reversal_type"] = "mild"
        else:
            result["reversal_type"] = "strong"
    else:
        result["reversal_type"] = "mild"
    return result


# ---------------------------------------------------------------------------
# Portfolio storage
# ---------------------------------------------------------------------------
PORTFOLIO_FILE = os.path.join(DASHBOARD_HOME, "portfolio.json")

# Palette used by the SVG pie chart in the dashboard template. Kept here so
# the same ordering is available to any caller that wants to color slices.
PORTFOLIO_PALETTE = [
    "#ffa726", "#42a5f5", "#26a69a", "#ef5350", "#ab47bc",
    "#78909c", "#ff7043", "#5c6bc0", "#66bb6a", "#ec407a",
]


def _ensure_portfolio_file() -> None:
    os.makedirs(DASHBOARD_HOME, exist_ok=True)
    if not os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump({"holdings": []}, f, indent=2)


def load_portfolio() -> list[dict]:
    """Return the list of holdings (each a dict with ticker/shares/...)."""
    _ensure_portfolio_file()
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    holdings = data.get("holdings") or []
    out: list[dict] = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        ticker = str(h.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        try:
            shares = float(h.get("shares", 0) or 0)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            entry_price = float(h.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        out.append({
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "entry_date": str(h.get("entry_date", "") or ""),
            "notes": str(h.get("notes", "") or ""),
        })
    return out


def save_portfolio(holdings: list[dict]) -> None:
    _ensure_portfolio_file()
    cleaned: list[dict] = []
    seen: set[str] = set()
    for h in holdings:
        if not isinstance(h, dict):
            continue
        ticker = str(h.get("ticker", "")).strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        try:
            shares = float(h.get("shares", 0) or 0)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            entry_price = float(h.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            entry_price = 0.0
        cleaned.append({
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "entry_date": str(h.get("entry_date", "") or ""),
            "notes": str(h.get("notes", "") or ""),
        })
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump({"holdings": cleaned}, f, indent=2, ensure_ascii=False)


def add_holding(
    ticker: str,
    shares: float,
    entry_price: float,
    entry_date: str = "",
    notes: str = "",
) -> list[dict]:
    """Append a holding. If the ticker already exists, merge by adding shares
    and computing the weighted-average entry price."""
    tu = str(ticker).strip().upper()
    try:
        sh = float(shares)
    except (TypeError, ValueError):
        sh = 0.0
    try:
        ep = float(entry_price)
    except (TypeError, ValueError):
        ep = 0.0
    holdings = load_portfolio()
    for h in holdings:
        if h["ticker"] == tu:
            old_shares = h["shares"]
            old_ep = h["entry_price"]
            total = old_shares + sh
            if total > 0:
                h["entry_price"] = round(
                    (old_shares * old_ep + sh * ep) / total, 6
                )
            h["shares"] = total
            if entry_date:
                h["entry_date"] = entry_date
            if notes:
                h["notes"] = notes
            save_portfolio(holdings)
            return holdings
    holdings.append({
        "ticker": tu,
        "shares": sh,
        "entry_price": ep,
        "entry_date": entry_date or "",
        "notes": notes or "",
    })
    save_portfolio(holdings)
    return holdings


def remove_holding(ticker: str) -> list[dict]:
    tu = str(ticker).strip().upper()
    holdings = [h for h in load_portfolio() if h["ticker"] != tu]
    save_portfolio(holdings)
    return holdings


def update_holding(
    ticker: str,
    shares: Optional[float] = None,
    entry_price: Optional[float] = None,
    entry_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> list[dict]:
    """Partial update of a holding by ticker. Returns the new holdings list."""
    tu = str(ticker).strip().upper()
    holdings = load_portfolio()
    for h in holdings:
        if h["ticker"] == tu:
            if shares is not None:
                try:
                    h["shares"] = float(shares)
                except (TypeError, ValueError):
                    pass
            if entry_price is not None:
                try:
                    h["entry_price"] = float(entry_price)
                except (TypeError, ValueError):
                    pass
            if entry_date is not None:
                h["entry_date"] = entry_date
            if notes is not None:
                h["notes"] = notes
            break
    save_portfolio(holdings)
    return holdings


def _rating_direction(rating_text: str) -> str:
    """Classify a rating as ``long`` / ``short`` / ``neutral`` / ``unknown``."""
    if not rating_text:
        return "unknown"
    txt = str(rating_text).strip().upper()
    if txt in ("BUY", "STRONG BUY", "OVERWEIGHT"):
        return "long"
    if txt in ("SELL", "STRONG SELL", "UNDERWEIGHT"):
        return "short"
    if txt in ("HOLD", "NEUTRAL"):
        return "neutral"
    return "unknown"


def compute_portfolio_summary(
    holdings: Optional[list[dict]] = None,
    ratings_map: Optional[dict] = None,
) -> dict:
    """Build the dashboard portfolio summary.

    Args:
        holdings: list of holding dicts (loaded from disk if omitted).
        ratings_map: optional ``{ticker: rating_text}`` map. When omitted the
            rating for each holding ticker is resolved from its latest state
            file via :func:`latest_state`.

    Returns a dict with:
        ``holdings`` -- the input holdings joined with current rating /
            reversal state,
        ``total_cost`` -- sum of shares * entry_price,
        ``allocation`` -- ``{ticker, weight_pct, market_value_proxy, color}``
            list (color comes from :data:`PORTFOLIO_PALETTE`),
        ``long_count`` / ``short_count`` / ``neutral_count`` -- direction
            counts,
        ``reversal_alerts`` -- holdings whose latest reversal is strong/mild,
            sorted strong first.
    """
    if holdings is None:
        holdings = load_portfolio()
    ratings_map = ratings_map or {}

    total_cost = 0.0
    tickers: list[dict] = []
    allocation: list[dict] = []
    long_count = short_count = neutral_count = 0
    reversal_alerts: list[dict] = []

    for idx, h in enumerate(holdings):
        ticker = h["ticker"]
        shares = h["shares"]
        entry_price = h["entry_price"]
        cost = shares * entry_price
        total_cost += cost

        rating_text = ratings_map.get(ticker)
        if rating_text is None:
            state, latest_date = latest_state(ticker)
            if state and state.get("final_trade_decision"):
                rating_text, _ = parse_rating(state["final_trade_decision"])
            else:
                rating_text = "Unknown"

        direction = _rating_direction(rating_text)
        if direction == "long":
            long_count += 1
        elif direction == "short":
            short_count += 1
        elif direction == "neutral":
            neutral_count += 1

        rev = detect_reversal(ticker)
        rev_type = (rev or {}).get("reversal_type", "none")
        last_run_date = (rev or {}).get("current_date")
        if not last_run_date:
            _, last_run_date = latest_state(ticker)

        tickers.append({
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "entry_date": h.get("entry_date", ""),
            "notes": h.get("notes", ""),
            "rating": rating_text,
            "direction": direction,
            "reversal_status": rev_type,
            "previous_rating": (rev or {}).get("previous_rating"),
            "current_rating": (rev or {}).get("current_rating"),
            "last_run_date": last_run_date,
        })

        if rev_type in ("strong", "mild"):
            reversal_alerts.append({
                "ticker": ticker,
                "reversal_type": rev_type,
                "previous_rating": (rev or {}).get("previous_rating"),
                "current_rating": (rev or {}).get("current_rating"),
                "last_run_date": last_run_date,
            })

        allocation.append({
            "ticker": ticker,
            "weight_pct": 0.0,
            "market_value_proxy": cost,
            "color": PORTFOLIO_PALETTE[idx % len(PORTFOLIO_PALETTE)],
        })

    total_proxy = sum(a["market_value_proxy"] for a in allocation) or 1.0
    for a in allocation:
        a["weight_pct"] = round(a["market_value_proxy"] / total_proxy * 100, 2)

    rank = {"strong": 0, "mild": 1}
    reversal_alerts.sort(key=lambda r: (rank.get(r["reversal_type"], 9),
                                        r["ticker"]))

    return {
        "holdings": tickers,
        "total_cost": round(total_cost, 2),
        "allocation": allocation,
        "long_count": long_count,
        "short_count": short_count,
        "neutral_count": neutral_count,
        "reversal_alerts": reversal_alerts,
    }


# ---------------------------------------------------------------------------
# Last run status
# ---------------------------------------------------------------------------
def load_last_run_status() -> dict:
    if not os.path.exists(LAST_RUN_FILE):
        return {}
    try:
        with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_last_run_status(payload: dict) -> None:
    os.makedirs(DASHBOARD_HOME, exist_ok=True)
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
