"""Background runner that calls the TradingAgents graph.

Wraps :class:`tradingagents.graph.trading_graph.TradingAgentsGraph` and
records per-run status to ``~/.tradingagents/dashboard/last_run_status.json``
so the dashboard frontend can poll for completion.
"""
from __future__ import annotations

from datetime import datetime

from dashboard.state_reader import (
    save_last_run_status,
    load_last_run_status,
)

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


def run_analysis(ticker: str, date: str) -> dict:
    """Run TradingAgents ``propagate`` for ``ticker`` on ``date``.

    Records ``running`` -> ``success``/``failed`` status to
    ``last_run_status.json`` so the dashboard can show progress. Returns the
    final-state dict.
    """
    status = {
        "ticker": ticker,
        "date": date,
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "running",
        "error": None,
    }
    save_last_run_status(status)
    try:
        TradingAgentsGraph = _get_graph_class()
        ta = TradingAgentsGraph()
        result = ta.propagate(ticker, date)
        # propagate returns ``(final_state, signal)`` per the docstring.
        if isinstance(result, tuple) and len(result) >= 1:
            final_state = result[0]
        else:
            final_state = result
        status["finished_at"] = _now_iso()
        status["status"] = "success"
        save_last_run_status(status)
        return final_state if isinstance(final_state, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- surface the message to UI
        status["finished_at"] = _now_iso()
        status["status"] = "failed"
        status["error"] = f"{type(exc).__name__}: {exc}"
        save_last_run_status(status)
        raise


def get_last_run_status() -> dict:
    return load_last_run_status()
