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
