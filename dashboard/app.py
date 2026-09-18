"""FastAPI dashboard for the TradingAgents project.

Renders the latest agent-team state for each ticker in the watchlist, with
support for manual ``Run Now`` triggers, date navigation and reversal
detection.

Run (from the project root, in the ``fundteam`` conda env)::

    cd /Users/kevin/PycharmProjects/TradingAgents
    /opt/anaconda3/envs/fundteam/bin/python -m dashboard.app

Then open: http://localhost:8080
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from dashboard import runner
from dashboard.state_reader import (
    add_ticker,
    detect_reversal,
    get_today_str,
    list_states_for_ticker,
    load_state,
    load_watchlist,
    latest_state,
    parse_rating,
    remove_ticker,
)

DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

app = FastAPI(title="TradingAgents Dashboard", version="0.1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ----------------------------------------------------------------------
# Template helpers -- registered as Jinja globals so the index template
# can call them directly without a filter registry.
# ----------------------------------------------------------------------
ANALYST_LABELS = {
    "market_report": "Market",
    "sentiment_report": "Sentiment",
    "news_report": "News",
    "fundamentals_report": "Fundamentals",
}


def _rating_class(rating_text: str) -> str:
    """CSS modifier for a rating chip (pos/neg/neu/unknown)."""
    if not rating_text:
        return "unknown"
    txt = str(rating_text).strip().upper()
    if txt in ("UNKNOWN", ""):
        return "unknown"
    if txt in ("BUY", "STRONG BUY", "OVERWEIGHT"):
        return "pos"
    if txt in ("SELL", "STRONG SELL", "UNDERWEIGHT"):
        return "neg"
    if txt == "HOLD" or txt == "NEUTRAL":
        return "neu"
    return "unknown"


def _reversal_class(reversal_type: str) -> str:
    """CSS class for the reversal badge."""
    rt = (reversal_type or "none").lower()
    if rt == "strong":
        return "red"
    if rt == "mild":
        return "orange"
    return "green"


def _reversal_label(reversal: dict) -> str:
    if not reversal:
        return "No data"
    rt = (reversal.get("reversal_type") or "none").lower()
    if rt == "strong":
        return "Strong reversal"
    if rt == "mild":
        return "Mild reversal"
    return "No reversal"


def _excerpt(text: str, n: int = 180) -> str:
    """Strip markdown syntax and return first ``n`` chars as plain text.

    Used to render a one-line executive summary next to each collapsible card
    title so the user can scan conclusions without expanding every card.
    """
    import re as _re
    if not text:
        return ""
    s = text
    s = _re.sub(r"```.*?```", "", s, flags=_re.DOTALL)
    s = _re.sub(r"`[^`]*`", "", s)
    s = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = _re.sub(r"^#{1,6}\s+", "", s, flags=_re.MULTILINE)
    s = _re.sub(r"\*\*([^*]*)\*\*", r"\1", s)
    s = _re.sub(r"\*([^*]*)\*", r"\1", s)
    s = _re.sub(r"^>\s+", "", s, flags=_re.MULTILINE)
    s = _re.sub(r"^[-*+]\s+", "", s, flags=_re.MULTILINE)
    s = _re.sub(r"\n{2,}", " ", s)
    s = _re.sub(r"\n", " ", s)
    s = _re.sub(r"#\s*", "", s)  # strip any remaining inline # markers
    s = _re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("..." if len(s) > n else "")


def _analyst_card(label: str, key: str, state: dict) -> str:
    """Render an analyst report as a collapsible card with an exec summary."""
    body = (state or {}).get(key, "") or ""
    safe_label = str(label)
    if not body.strip():
        return (
            '<details class="collapse analyst-card">'
            '<summary>' + safe_label + ' <span class="muted">— no report</span></summary>'
            '<div class="empty">No report recorded.</div></details>'
        )
    summary = _excerpt(body, 180)
    return (
        '<details class="collapse analyst-card">'
        '<summary><span class="card-title">' + safe_label + '</span>'
        '<span class="excerpt">' + summary + '</span></summary>'
        '<div class="md-body" data-md>' + body + '</div>'
        '</details>'
    )


def _timeline_rating(ticker: str, date: str) -> str:
    state = load_state(ticker, date) or {}
    text = state.get("final_trade_decision", "") or ""
    rating, _ = parse_rating(text)
    return rating


# Register helpers with the Jinja2 environment so the template can call
# them by name. Jinja2Templates uses the same env instance as .env.
templates.env.globals.update({
    "rating_class": _rating_class,
    "parse_rating": parse_rating,
    "reversal_class": _reversal_class,
    "reversal_label": _reversal_label,
    "analyst_card": _analyst_card,
    "timeline_rating": _timeline_rating,
    "excerpt": _excerpt,
})


# ----------------------------------------------------------------------
# Context builders
# ----------------------------------------------------------------------
def _watchlist_summary() -> list[dict]:
    """Build the chips payload for the top bar."""
    out: list[dict] = []
    for ticker in load_watchlist():
        state, latest_date = latest_state(ticker)
        rating_text = "Unknown"
        if state and state.get("final_trade_decision"):
            rating_text, _ = parse_rating(state["final_trade_decision"])
        rev = detect_reversal(ticker)
        out.append({
            "ticker": ticker,
            "latest_state": latest_date,
            "latest_rating": rating_text,
            "reversal_status": rev.get("reversal_type", "none"),
            "last_run_date": latest_date,
        })
    return out


def _build_index_context(request: Request) -> dict:
    """Build the context dict for the index page."""
    params = request.query_params
    watchlist = load_watchlist()
    selected_ticker = (params.get("ticker") or "").strip().upper()
    if not selected_ticker and watchlist:
        selected_ticker = watchlist[0]
    if selected_ticker and selected_ticker not in watchlist:
        # Allow viewing a ticker even if not in watchlist, but fall back.
        pass

    available_dates = list_states_for_ticker(selected_ticker) if selected_ticker else []
    selected_date = (params.get("date") or "").strip()
    if not selected_date:
        selected_date = available_dates[0] if available_dates else None
    elif available_dates and selected_date not in available_dates:
        # User asked for a date that doesn't exist; fall back to latest.
        selected_date = available_dates[0]

    selected_state: Optional[dict] = None
    if selected_ticker and selected_date:
        selected_state = load_state(selected_ticker, selected_date)

    reversal = detect_reversal(selected_ticker) if selected_ticker else {}

    return {
        "watchlist": _watchlist_summary(),
        "selected_ticker": selected_ticker,
        "selected_state": selected_state or {},
        "selected_date": selected_date,
        "available_dates": available_dates,
        "reversal": reversal,
        "today": get_today_str(),
    }


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ctx = _build_index_context(request)
    # CRITICAL Starlette 1.6+ gotcha: pass request as the FIRST positional
    # arg, not embedded in context. Otherwise raises "unhashable type: dict".
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/api/state/{ticker}/{date}")
def api_state(ticker: str, date: str):
    state = load_state(ticker.upper(), date)
    if state is None:
        return JSONResponse({"error": "state not found",
                             "ticker": ticker.upper(), "date": date},
                            status_code=404)
    return JSONResponse(state)


@app.get("/api/watchlist")
def api_watchlist():
    return JSONResponse({"tickers": load_watchlist()})


@app.get("/api/watchlist_summary")
def api_watchlist_summary():
    return JSONResponse(_watchlist_summary())


@app.post("/api/watchlist/add")
async def api_watchlist_add(request: Request):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    if ticker:
        add_ticker(ticker)
    return JSONResponse({"tickers": load_watchlist()})


@app.post("/api/watchlist/remove")
async def api_watchlist_remove(request: Request):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    if ticker:
        remove_ticker(ticker)
    return JSONResponse({"tickers": load_watchlist()})


@app.post("/api/run")
async def api_run(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    date = str(body.get("date") or "").strip() or get_today_str()
    if not ticker:
        return JSONResponse({"error": "ticker required"}, status_code=400)

    def _job():
        try:
            runner.run_analysis(ticker, date)
        except Exception as exc:  # noqa: BLE001 -- recorded in status file
            # Status file is already updated by run_analysis; just log.
            print(f"[dashboard] run failed for {ticker} {date}: {exc}")

    background_tasks.add_task(_job)
    return JSONResponse({"status": "started", "ticker": ticker, "date": date})


@app.get("/api/run/status")
def api_run_status():
    return JSONResponse(runner.get_last_run_status())


@app.get("/api/reversal/{ticker}")
def api_reversal(ticker: str):
    return JSONResponse(detect_reversal(ticker.upper()))


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Make ``python -m dashboard.app`` work even if the user double-click the
    # file -- ensure the project root (parent of dashboard/) is on sys.path.
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in __import__("sys").path:
        __import__("sys").path.insert(0, project_root)
    uvicorn.run(app, host="0.0.0.0", port=8080)
