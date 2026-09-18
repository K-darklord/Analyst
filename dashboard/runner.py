"""Background runner that calls the TradingAgents graph.

Wraps :class:`tradingagents.graph.trading_graph.TradingAgentsGraph` and
records per-run status to ``~/.tradingagents/dashboard/last_run_status.json``
so the dashboard frontend can poll for completion.

Phase 4 additions:
  - ``run_analysis`` accepts an optional ``progress_callback`` that
    receives ``(event_type, payload)`` tuples so callers (the SSE
    endpoint) can stream progress in real time.
  - ``run_all_with_progress`` runs the whole watchlist and emits
    per-ticker + all-done events through the callback.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Optional

from dashboard.state_reader import (
    save_last_run_status,
    load_last_run_status,
)

# Type alias for the progress callback. Callable[[str, dict], None] would be
# stricter but mypy isn't enforced here; keep it permissive for callers that
# want to ignore the payload.
ProgressCallback = Callable[[str, dict], None]


# Import lazily so importing this module never triggers the heavy langgraph
# stack -- that lets the dashboard's read-only endpoints stay fast even when
# the LLM client isn't configured.
_GRAPH_CLASS = None


def _get_graph_class():
    """Import the TradingAgentsGraph class on first use."""
    global _GRAPH_CLASS
    if _GRAPH_CLASS is None:
        # The TradingAgents package is installed editable in the fundteam
        # env. The verified import path is
        # ``tradingagents.graph.trading_graph`` (not
        # ``tradingagents.tradingagentsgraph``).
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        _GRAPH_CLASS = TradingAgentsGraph
    return _GRAPH_CLASS


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_save_status(status: dict) -> None:
    """Best-effort status file write. Never raises.

    The SSE progress stream carries the same information in real time, so a
    status-file write failure (sandboxed env, read-only mount, permissions)
    must not abort the run. We log once and move on.
    """
    try:
        save_last_run_status(status)
    except Exception as exc:  # noqa: BLE001 -- intentional non-fatal
        import sys
        if not getattr(_safe_save_status, "_warned", False):
            print(f"[runner] save_last_run_status failed (non-fatal): {exc}",
                  file=sys.stderr)
            _safe_save_status._warned = True




def _emit(callback: ProgressCallback | None, event: str, payload: dict) -> None:
    """Safe event emit: never let a callback exception crash the run."""
    if callback is None:
        return
    try:
        callback(event, payload)
    except Exception:  # noqa: BLE001 -- callbacks must not crash the run
        pass


def run_analysis(
    ticker: str,
    date: str,
    *,
    progress_callback: ProgressCallback | None = None,
    index: int | None = None,
    total: int | None = None,
) -> dict:
    """Run TradingAgents ``propagate`` for ``ticker`` on ``date``.

    Records ``running`` -> ``success``/``failed`` status to
    ``last_run_status.json`` so the dashboard can show progress. Returns the
    final-state dict.

    If ``progress_callback`` is supplied, the following events are emitted:
      - ``("ticker_start", {ticker, date, index, total, started_at})``
      - ``("log", {ticker, message})`` — emitted at major sub-steps
        (graph instantiation, propagate start, propagate done)
      - ``("ticker_done", {ticker, date, index, total, status,
        error, finished_at, duration_sec})``
    """
    started_at = _now_iso()
    started_ts = time.time()
    status = {
        "ticker": ticker,
        "date": date,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "error": None,
    }
    _safe_save_status(status)
    _emit(progress_callback, "ticker_start", {
        "ticker": ticker,
        "date": date,
        "index": index,
        "total": total,
        "started_at": started_at,
    })

    try:
        _emit(progress_callback, "log", {
            "ticker": ticker,
            "message": "Initializing TradingAgentsGraph ...",
        })
        TradingAgentsGraph = _get_graph_class()
        ta = TradingAgentsGraph()
        _emit(progress_callback, "log", {
            "ticker": ticker,
            "message": f"Running propagate({ticker}, {date}) ...",
        })
        result = ta.propagate(ticker, date)
        # propagate returns ``(final_state, signal)`` per the docstring.
        if isinstance(result, tuple) and len(result) >= 1:
            final_state = result[0]
        else:
            final_state = result
        finished_at = _now_iso()
        duration = round(time.time() - started_ts, 1)
        status["finished_at"] = finished_at
        status["status"] = "success"
        _safe_save_status(status)
        _emit(progress_callback, "ticker_done", {
            "ticker": ticker,
            "date": date,
            "index": index,
            "total": total,
            "status": "success",
            "error": None,
            "finished_at": finished_at,
            "duration_sec": duration,
        })
        return final_state if isinstance(final_state, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- surface the message to UI
        finished_at = _now_iso()
        duration = round(time.time() - started_ts, 1)
        status["finished_at"] = finished_at
        status["status"] = "failed"
        status["error"] = f"{type(exc).__name__}: {exc}"
        _safe_save_status(status)
        _emit(progress_callback, "ticker_done", {
            "ticker": ticker,
            "date": date,
            "index": index,
            "total": total,
            "status": "failed",
            "error": str(exc),
            "finished_at": finished_at,
            "duration_sec": duration,
        })
        raise


def run_all_with_progress(
    tickers: list[str],
    date: str,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run analysis for every ticker in ``tickers`` on ``date``.

    Emits ``all_start`` / per-ticker events (via run_analysis) /
    ``all_done`` through ``progress_callback``. Also persists aggregate
    run-all status to ``last_run_status.json`` so the legacy polling
    endpoint (``/api/run/status``) keeps working alongside the SSE stream.
    """
    total = len(tickers)
    started_at = _now_iso()
    started_ts = time.time()

    # Seed the aggregate status before the loop starts so a fast poller
    # never sees a stale single-ticker status from a prior run.
    _safe_save_status({
        "mode": "run_all",
        "status": "running",
        "total": total,
        "completed": 0,
        "current_ticker": tickers[0] if tickers else None,
        "tickers": tickers,
        "date": date,
        "started_at": started_at,
        "finished_at": None,
        "error": None,
    })
    _emit(progress_callback, "all_start", {
        "tickers": tickers,
        "total": total,
        "date": date,
        "started_at": started_at,
    })

    last_error = None
    completed = 0
    failed = 0
    for i, ticker in enumerate(tickers):
        # Mark the current ticker before invoking run_analysis (which will
        # overwrite the file with its own per-ticker status).
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
        _safe_save_status(pre)
        try:
            run_analysis(
                ticker,
                date,
                progress_callback=progress_callback,
                index=i,
                total=total,
            )
            completed += 1
        except Exception as exc:  # noqa: BLE001 -- recorded, continue
            failed += 1
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
            _safe_save_status(err_status)
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
        _safe_save_status(post)

    finished_at = _now_iso()
    duration = round(time.time() - started_ts, 1)
    final = load_last_run_status() or {}
    final.update({
        "mode": "run_all",
        "status": "success" if not last_error else "failed",
        "completed": len(tickers),
        "current_ticker": None,
        "tickers": tickers,
        "finished_at": finished_at,
        "error": last_error,
    })
    _safe_save_status(final)
    _emit(progress_callback, "all_done", {
        "total": total,
        "completed": completed,
        "failed": failed,
        "duration_sec": duration,
        "finished_at": finished_at,
        "error": last_error,
    })
    return final


def get_last_run_status() -> dict:
    return load_last_run_status()
