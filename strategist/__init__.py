"""Strategist — top-down market regime detection.

Combines ORCA-style trend forecasting with omd_finance-style spectral collapse
detection to produce a market regime signal that acts as a position-size
override on top of the Analyst's per-stock ratings.

This module lives OUTSIDE the upstream TradingAgents package so that upstream
updates can be merged without conflict. It is part of the FundTeam layer.
"""

from .strategist import Strategist, run_strategist

__all__ = ["Strategist", "run_strategist"]
