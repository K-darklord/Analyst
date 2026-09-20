"""FORK EXTENSION — Decision log hook for the FundTeam orchestration layer.

Posts every Analyst / Researcher output to FundTeam's /api/decisions/log so
the backtester can later backfill realised PnL and score each agent.

This module is best-effort: any failure (network, FundTeam down, malformed
payload) is swallowed and logged to stderr so the agent pipeline never
blocks on the logging side-path.

Mirrors strategist/signal_combiner.py#_log_to_fundteam so all three agents
(Analyst, Researcher, Strategist) share the same logging contract.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Any, Optional


def log_to_fundteam(
    agent_name: str,
    signal_type: str,
    signal_content: dict[str, Any],
    market: Optional[str] = None,
    ticker: Optional[str] = None,
    target_price: Optional[float] = None,
    notes: Optional[str] = None,
    repair_plan: Optional[dict] = None,
    repair_status: Optional[dict] = None,
    timeout: float = 2.0,
) -> None:
    """POST a decision record to FundTeam /api/decisions/log.

    Failures are non-fatal: they print to stderr and return None.

    Phase B1/B4 extension: ``repair_plan`` (emitted by auditor when
    verdict != approve) and ``repair_status`` (written by engineer after
    executing the fix) are passed through to the endpoint, which stores
    them in ``decisions.repair_plan_json`` / ``decisions.repair_status_json``
    so the Engineer dashboard can surface them via
    ``/api/engineer/repair-plans``.
    """
    base = os.environ.get("FUNDTEAM_URL", "http://127.0.0.1:8080")
    payload = {
        "agent_name": agent_name,
        "signal_type": signal_type,
        "market": market,
        "ticker": ticker,
        "signal_content": signal_content,
        "target_price": target_price,
        "notes": notes,
    }
    # Phase B1/B4: only include repair_plan/repair_status when provided
    # so older callers (and the dashboard endpoint's schema check) are
    # not affected. The endpoint at /api/decisions/log reads body.get(...)
    # and forwards to decision_log.log_decision(repair_plan=, repair_status=).
    if repair_plan is not None:
        payload["repair_plan"] = repair_plan
    if repair_status is not None:
        payload["repair_status"] = repair_status
    try:
        req = urllib.request.Request(
            f"{base}/api/decisions/log",
            data=json.dumps(payload, default=str).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[decision_log_hook] {agent_name}/{signal_type} log failed "
            f"(non-fatal): {exc}",
            file=sys.stderr,
        )


# --- Rating parsing (mirrors dashboard/state_reader.parse_rating) ------------

_RATING_LINE_RE = re.compile(
    r"\*\*(?:Rating|Recommendation|Action)\*\*\s*:?\s*(.+)",
    re.IGNORECASE,
)
_RATING_PHRASES = [
    "strong buy", "buy", "overweight", "accumulate",
    "strong sell", "sell", "underweight", "reduce",
    "hold", "neutral", "equal weight",
]


def parse_rating_text(text: str) -> str:
    """Extract the rating phrase ('Buy', 'Hold', etc.) from markdown."""
    if not text:
        return "Unknown"
    for m in _RATING_LINE_RE.finditer(text):
        raw = m.group(1).strip().strip("*").strip().upper()
        for phrase in _RATING_PHRASES:
            if phrase in raw:
                return phrase.title()
        return raw.title() if raw else "Unknown"
    return "Unknown"


def _state_ticker(state: dict) -> Optional[str]:
    """Resolve the ticker this decision is about from LangGraph state."""
    for key in ("ticker", "symbol_of_interest", "symbol"):
        val = state.get(key)
        if val:
            return str(val)
    return None


def _state_market(ticker: Optional[str]) -> Optional[str]:
    """Infer market from ticker suffix (.SH/.SZ -> ASHARE, .HK -> HK, else US)."""
    if not ticker:
        return None
    up = ticker.upper()
    if up.endswith((".SH", ".SZ")):
        return "ASHARE"
    if up.endswith(".HK") or up.startswith(("0", "1", "2", "3", "9")) and up.isdigit():
        return "HK"
    return "US"


# --- Public convenience hooks ----------------------------------------------

def log_analyst_decision(state: dict, final_trade_decision: str) -> None:
    """Hook for the Portfolio Manager (Analyst) final decision."""
    ticker = _state_ticker(state)
    market = state.get("market") or _state_market(ticker)
    rating = parse_rating_text(final_trade_decision)
    log_to_fundteam(
        agent_name="analyst",
        signal_type="trade_decision",
        market=market,
        ticker=ticker,
        signal_content={
            "rating": rating,
            "decision_text": final_trade_decision[:4000],
        },
        notes=f"rating={rating}",
    )


def log_researcher_decision(state: dict, investment_plan: str) -> None:
    """Hook for the Research Manager investment plan."""
    ticker = _state_ticker(state)
    market = state.get("market") or _state_market(ticker)
    rating = parse_rating_text(investment_plan)
    log_to_fundteam(
        agent_name="researcher",
        signal_type="investment_plan",
        market=market,
        ticker=ticker,
        signal_content={
            "recommendation": rating,
            "plan_text": investment_plan[:4000],
        },
        notes=f"recommendation={rating}",
    )
