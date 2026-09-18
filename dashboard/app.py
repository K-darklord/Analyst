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

import asyncio
import json
import queue
import threading

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from dashboard import runner
from dashboard.state_reader import (
    PORTFOLIO_PALETTE,
    add_holding,
    add_ticker,
    compute_portfolio_summary,
    detect_reversal,
    get_today_str,
    list_states_for_ticker,
    load_last_run_status,
    load_portfolio,
    load_state,
    load_watchlist,
    latest_state,
    parse_rating,
    remove_holding,
    remove_ticker,
    save_last_run_status,
    update_holding,
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
# SVG chart helpers (hand-coded, no Chart.js dependency)
# ----------------------------------------------------------------------
import html as _html
import math as _math


def _esc(s) -> str:
    """HTML-escape a value for safe inclusion in SVG markup."""
    return _html.escape(str(s), quote=True)


def _rating_level_int(text: str):
    """Map a rating text to an integer level (-2..+2) using parse_rating.

    Returns None when no level can be derived (the rating is "Unknown").
    """
    _, level = parse_rating(text or "")
    return level


def render_allocation_pie(allocation: list, diameter: int = 140) -> str:
    """Hand-coded SVG donut/pie chart for portfolio allocation.

    ``allocation`` is the list produced by :func:`compute_portfolio_summary`
    (each item has ``ticker``, ``weight_pct``, ``color``).
    Returns an inline SVG string. Single holding -> full circle. Empty -> a
    placeholder ring.
    """
    if not allocation:
        return (
            f'<svg class="pie" viewBox="0 0 {diameter} {diameter}" '
            f'width="{diameter}" height="{diameter}" role="img" '
            f'aria-label="no holdings">'
            f'<circle cx="{diameter/2:.1f}" cy="{diameter/2:.1f}" '
            f'r="{diameter/2 - 8:.1f}" fill="none" stroke="#2a3142" '
            f'stroke-width="6" stroke-dasharray="3 4"/>'
            f'<text x="{diameter/2:.1f}" y="{diameter/2:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'fill="#5c6478" font-size="9" font-family="monospace">'
            f'NO DATA</text></svg>'
        )

    cx = diameter / 2
    cy = diameter / 2
    r = diameter / 2 - 4
    parts: list[str] = [
        f'<svg class="pie" viewBox="0 0 {diameter} {diameter}" '
        f'width="{diameter}" height="{diameter}" role="img" '
        f'aria-label="portfolio allocation">'
    ]

    if len(allocation) == 1:
        a = allocation[0]
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
            f'fill="{_esc(a.get("color", "#ffa726"))}"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" fill="#0a0e1a" '
            f'font-family="monospace" font-size="11" font-weight="700">'
            f'{a["weight_pct"]:.0f}%</text>'
        )
        parts.append("</svg>")
        return "".join(parts)

    start_angle = -90.0  # start at 12 o'clock
    for a in allocation:
        sweep = (a["weight_pct"] / 100.0) * 360.0
        if sweep <= 0:
            continue
        end_angle = start_angle + sweep

        def polar(angle_deg):
            rad = _math.radians(angle_deg)
            return (cx + r * _math.cos(rad), cy + r * _math.sin(rad))

        x1, y1 = polar(start_angle)
        x2, y2 = polar(end_angle)
        large_arc = 1 if sweep > 180 else 0
        color = _esc(a.get("color", "#ffa726"))
        path = (
            f'M {cx:.2f} {cy:.2f} L {x1:.2f} {y1:.2f} '
            f'A {r:.2f} {r:.2f} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z'
        )
        parts.append(
            f'<path d="{path}" fill="{color}" stroke="#0a0e1a" '
            f'stroke-width="1"/>'
        )
        if a["weight_pct"] >= 5:
            mid_angle = start_angle + sweep / 2
            lx = cx + (r * 0.62) * _math.cos(_math.radians(mid_angle))
            ly = cy + (r * 0.62) * _math.sin(_math.radians(mid_angle))
            parts.append(
                f'<text x="{lx:.2f}" y="{ly:.2f}" text-anchor="middle" '
                f'dominant-baseline="middle" fill="#0a0e1a" '
                f'font-family="monospace" font-size="8" font-weight="700">'
                f'{_esc(a["ticker"])[:6]}</text>'
            )
        start_angle = end_angle
    parts.append("</svg>")
    return "".join(parts)


def render_rating_history_chart(
    ticker: str,
    dates: list,
    width: int = 1000,
    height: int = 200,
) -> str:
    """Hand-coded SVG line chart of a ticker's rating over time.

    X axis = ``dates`` (YYYY-MM-DD), Y axis = rating level (-2..+2) via
    :func:`parse_rating`. Reads each state file through
    :func:`load_state` (mirrors the existing ``timeline_rating`` helper but
    also returns the numeric level for plotting).
    """
    pad_l, pad_r, pad_t, pad_b = 44, 20, 20, 36
    plot_w = max(width - pad_l - pad_r, 10)
    plot_h = max(height - pad_t - pad_b, 10)

    levels = []  # list of (date, level or None, rating_text)
    for d in dates:
        st = load_state(ticker, d) or {}
        txt = st.get("final_trade_decision", "") or ""
        rt, lv = parse_rating(txt)
        levels.append((d, lv, rt))

    y_min, y_max = -2, 2
    def y_for(level):
        if level is None:
            level = 0
        t = (level - y_min) / (y_max - y_min)  # 0..1
        # invert: high level -> top (small y)
        return pad_t + (1 - t) * plot_h

    n = len(dates)
    def x_for(i):
        if n <= 1:
            return pad_l + plot_w / 2
        return pad_l + (i / (n - 1)) * plot_w

    parts = [
        f'<svg class="rating-chart" viewBox="0 0 {width} {height}" '
        f'width="100%" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="rating history for {_esc(ticker)}">'
    ]
    # Background
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="#151a2e" rx="2"/>'
    )

    # Gridlines + Y labels
    y_labels = {2: "SB", 1: "B", 0: "H", -1: "S", -2: "SS"}
    for lv in range(y_min, y_max + 1):
        y = y_for(lv)
        color = "#2a3142"
        text_color = "#5c6478"
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.2f}" x2="{width - pad_r}" '
            f'y2="{y:.2f}" stroke="{color}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{y + 3:.2f}" text-anchor="end" '
            f'fill="{text_color}" font-family="monospace" '
            f'font-size="9">{y_labels[lv]}</text>'
        )

    # Zero line slightly stronger
    zy = y_for(0)
    parts.append(
        f'<line x1="{pad_l}" y1="{zy:.2f}" x2="{width - pad_r}" '
        f'y2="{zy:.2f}" stroke="#3a4258" stroke-width="1" '
        f'stroke-dasharray="2 3"/>'
    )

    # X labels (dates)
    label_every = max(1, (n // 8) or 1)
    for i, d in enumerate(dates):
        if i % label_every != 0 and i != n - 1:
            continue
        x = x_for(i)
        deg = -30 if n > 6 else 0
        anchor = "end" if deg else "middle"
        parts.append(
            f'<text x="{x:.2f}" y="{height - pad_b + 14}" '
            f'text-anchor="{anchor}" transform="rotate({deg} {x:.2f} '
            f'{height - pad_b + 14})" fill="#5c6478" '
            f'font-family="monospace" font-size="9">{_esc(d)}</text>'
        )

    # Build polyline points (skip None-level points for the line, but still
    # draw a dot for known points).
    pts: list[str] = []
    dots: list[str] = []
    for i, (d, lv, rt) in enumerate(levels):
        if lv is None:
            if pts:
                # break the line at unknown points
                pts.append("__BREAK__")
            continue
        x = x_for(i)
        y = y_for(lv)
        pts.append(f"{x:.2f},{y:.2f}")
        color = "#ffa726"
        dots.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"/>'
        )

    # Draw each contiguous segment as a polyline.
    segments: list[list[str]] = []
    cur: list[str] = []
    for p in pts:
        if p == "__BREAK__":
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(p)
    if cur:
        segments.append(cur)
    for seg in segments:
        if len(seg) < 2:
            # single point -> draw a small dot only (handled below)
            continue
        points_str = " ".join(seg)
        parts.append(
            f'<polyline points="{points_str}" fill="none" '
            f'stroke="#ffa726" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round"/>'
        )

    parts.extend(dots)
    parts.append("</svg>")
    return "".join(parts)


# Expose the SVG helpers to templates.
templates.env.globals.update({
    "render_allocation_pie": render_allocation_pie,
    "render_rating_history_chart": render_rating_history_chart,
    "portfolio_palette": PORTFOLIO_PALETTE,
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

    portfolio = compute_portfolio_summary()

    return {
        "watchlist": _watchlist_summary(),
        "selected_ticker": selected_ticker,
        "selected_state": selected_state or {},
        "selected_date": selected_date,
        "available_dates": available_dates,
        "reversal": reversal,
        "today": get_today_str(),
        "portfolio": portfolio,
        "holding_count": len(portfolio.get("holdings", [])),
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


@app.get("/print", response_class=HTMLResponse)
def print_view(request: Request, ticker: str = ""):
    """Print-optimized single-stock report page (PDF-ready).

    Loads the latest state for ``ticker`` via :func:`latest_state` and renders
    ``print.html`` with all sections expanded (no ``<details>``) so the page
    prints cleanly to A4. If no ticker is supplied or no state is found, an
    error view is rendered instead.
    """
    tu = (ticker or "").strip().upper()
    error_message: Optional[str] = None
    state: dict = {}
    state_date: Optional[str] = None
    rating_text = "Unknown"
    rating_level: Optional[int] = None

    if not tu:
        error_message = "No ticker specified. Use /print?ticker=XXX"
    else:
        st, d = latest_state(tu)
        if not st:
            error_message = (
                f"No state file found for ticker '{tu}'. "
                f"Run an analysis first."
            )
        else:
            state = st
            state_date = d
            decision = st.get("final_trade_decision", "") or ""
            if decision:
                rating_text, rating_level = parse_rating(decision)

    return templates.TemplateResponse(
        request,
        "print.html",
        {
            "ticker": tu,
            "state": state,
            "state_date": state_date,
            "rating_text": rating_text,
            "rating_level": rating_level,
            "rating_class": _rating_class(rating_text),
            "error_message": error_message,
        },
    )


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


# ----------------------------------------------------------------------
# Portfolio endpoints
# ----------------------------------------------------------------------
@app.get("/api/portfolio")
def api_portfolio():
    return JSONResponse(compute_portfolio_summary())


@app.post("/api/portfolio/add")
async def api_portfolio_add(request: Request):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    try:
        shares = float(body.get("shares", 0) or 0)
    except (TypeError, ValueError):
        shares = 0.0
    try:
        entry_price = float(body.get("entry_price", 0) or 0)
    except (TypeError, ValueError):
        entry_price = 0.0
    entry_date = str(body.get("entry_date", "") or "")
    notes = str(body.get("notes", "") or "")
    add_holding(ticker, shares, entry_price, entry_date, notes)
    return JSONResponse(compute_portfolio_summary())


@app.post("/api/portfolio/remove")
async def api_portfolio_remove(request: Request):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    if ticker:
        remove_holding(ticker)
    return JSONResponse(compute_portfolio_summary())


@app.post("/api/portfolio/update")
async def api_portfolio_update(request: Request):
    body = await request.json()
    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker:
        return JSONResponse({"error": "ticker required"}, status_code=400)
    update_holding(
        ticker,
        shares=body.get("shares"),
        entry_price=body.get("entry_price"),
        entry_date=body.get("entry_date"),
        notes=body.get("notes"),
    )
    return JSONResponse(compute_portfolio_summary())


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


def _now_iso() -> str:
    """ISO-8601 timestamp with second precision, for status file fields."""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# Run-All SSE streaming endpoint (Phase 4)
# ----------------------------------------------------------------------
# Live per-ticker progress events streamed to the frontend as Server-Sent
# Events. The frontend uses ``EventSource`` to consume this endpoint and
# updates the progress panel + per-ticker cards in real time, replacing the
# legacy 1-second ``/api/run/status`` polling loop when Run All is invoked.
#
# Event shape (one JSON object per SSE ``data:`` line):
#   {"event": "all_start", "tickers": [...], "total": N, "date": "..."}
#   {"event": "ticker_start", "ticker": "NVDA", "index": 0, "total": N, ...}
#   {"event": "log", "ticker": "NVDA", "message": "Running propagate(...)"}
#   {"event": "ticker_done", "ticker": "NVDA", "status": "success", ...}
#   {"event": "all_done", "total": N, "completed": X, "failed": Y, ...}
#
# The endpoint spawns the run-all work in a daemon thread and bridges
# thread-side callback events into an asyncio.Queue via
# ``loop.call_soon_threadsafe``. The SSE response awaits the queue and
# writes events as they arrive. On ``all_done`` (or any fatal error), the
# stream is closed and the EventSource reconnect logic is suppressed via
# a final ``retry: 999999999`` comment so the browser doesn't auto-reconnect.
def _sse_format(event_type: str, payload: dict) -> bytes:
    """Format a single SSE message as bytes (per the SSE wire format)."""
    # SSE allows an event name line + a data line + a blank line terminator.
    # We pack the payload as JSON so the frontend can parse one object per
    # message regardless of which event type fired.
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\n data: {data}\n\n".encode("utf-8")


@app.post("/api/run-all/stream")
async def api_run_all_stream():
    """Stream run-all progress as Server-Sent Events.

    Returns a ``StreamingResponse`` with ``media_type="text/event-stream"``.
    The response stays open until the run-all job finishes (or fails).
    """
    tickers = load_watchlist()
    if not tickers:
        return JSONResponse({"error": "watchlist is empty"}, status_code=400)

    date = get_today_str()

    # Thread-safe queue + running flag. The worker thread puts (event, payload)
    # tuples; the async side drains the queue and writes SSE bytes.
    event_q: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()
    loop = asyncio.get_running_loop()

    def _callback(event_type: str, payload: dict) -> None:
        # Called from the worker thread — must be thread-safe. We use
        # call_soon_threadsafe to schedule the queue put on the loop thread,
        # but queue.put itself is already thread-safe so a direct put is fine.
        event_q.put((event_type, payload))

    def _worker():
        try:
            runner.run_all_with_progress(tickers, date, progress_callback=_callback)
        except Exception as exc:  # noqa: BLE001 -- surfaced via all_done
            # Runner already emits all_done with the error; if it blew up
            # before reaching that emit, surface it here so the stream ends.
            event_q.put(("all_done", {
                "total": len(tickers),
                "completed": 0,
                "failed": len(tickers),
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": _now_iso(),
            }))
        # Sentinel: signal the async side that no more events are coming.
        event_q.put(None)

    thread = threading.Thread(target=_worker, daemon=True, name="run-all-stream")
    thread.start()

    async def _event_stream():
        # Send an initial hello event so the EventSource.onopen fires
        # immediately and the frontend can render the panel before the
        # first real event arrives.
        yield _sse_format("hello", {"tickers": tickers, "total": len(tickers), "date": date})
        while True:
            try:
                # Block on the queue from the async side. queue.Queue.get
                # is sync and would block the event loop, so use a
                # thread-pool offload via run_in_executor.
                item = await asyncio.get_event_loop().run_in_executor(
                    None, event_q.get,
                )
            except Exception:
                break
            if item is None:
                # Worker finished. Tell the browser to stop reconnecting
                # (SSE spec: a huge retry interval effectively disables it).
                yield b"retry: 999999999\n\n"
                return
            event_type, payload = item
            yield _sse_format(event_type, payload)
            if event_type == "all_done":
                yield b"retry: 999999999\n\n"
                return

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )


@app.post("/api/run-all")
async def api_run_all(background_tasks: BackgroundTasks):
    """Run analysis sequentially for every ticker in the watchlist.

    Stores an aggregate run-all status to ``last_run_status.json`` so that
    ``/api/run/status`` can report progress (total/completed/current_ticker).
    Each ``runner.run_analysis`` call overwrites the file with per-ticker
    fields; this endpoint re-saves the aggregate fields after each ticker
    completes so the poller sees consistent run-all progress.
    """
    tickers = load_watchlist()
    if not tickers:
        return JSONResponse(
            {"error": "watchlist is empty"}, status_code=400
        )

    date = get_today_str()

    # Seed the aggregate status before the background task starts so that a
    # fast poller never sees a stale single-ticker status from a prior run.
    save_last_run_status({
        "mode": "run_all",
        "status": "running",
        "total": len(tickers),
        "completed": 0,
        "current_ticker": tickers[0],
        "tickers": tickers,
        "date": date,
        "started_at": _now_iso(),
        "finished_at": None,
        "error": None,
    })

    def _job():
        total = len(tickers)
        last_error = None
        for i, ticker in enumerate(tickers):
            # Mark the current ticker before invoking run_analysis (which
            # will overwrite the file with its own per-ticker status).
            pre = load_last_run_status() or {}
            pre.update({
                "mode": "run_all",
                "status": "running",
                "total": total,
                "completed": i,
                "current_ticker": ticker,
                "tickers": tickers,
                "date": date,
            })
            save_last_run_status(pre)
            try:
                runner.run_analysis(ticker, date)
            except Exception as exc:  # noqa: BLE001 -- recorded, continue
                last_error = f"{ticker}: {type(exc).__name__}: {exc}"
                err_status = load_last_run_status() or {}
                err_status.update({
                    "mode": "run_all",
                    "status": "running",
                    "total": total,
                    "completed": i,
                    "current_ticker": ticker,
                    "error": last_error,
                })
                save_last_run_status(err_status)
                continue
            # run_analysis just overwrote the file with per-ticker fields;
            # restore the aggregate run-all fields and bump completed count.
            post = load_last_run_status() or {}
            post.update({
                "mode": "run_all",
                "status": "running",
                "total": total,
                "completed": i + 1,
                "current_ticker": tickers[i + 1] if i + 1 < total else None,
                "tickers": tickers,
                "date": date,
            })
            save_last_run_status(post)

        final = load_last_run_status() or {}
        final.update({
            "mode": "run_all",
            "status": "success" if not last_error else "failed",
            "completed": len(tickers),
            "current_ticker": None,
            "tickers": tickers,
            "finished_at": _now_iso(),
            "error": last_error,
        })
        save_last_run_status(final)

    background_tasks.add_task(_job)
    return JSONResponse({
        "status": "started",
        "tickers": tickers,
        "count": len(tickers),
        "date": date,
    })


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
