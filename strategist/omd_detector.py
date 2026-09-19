"""omd_finance-style spectral collapse detector.

omd_finance (arXiv:2607.19005) shows that the "effective dimension" of the
cross-sectional correlation matrix collapses sharply before endogenous crises
(2008).  The collapse means the market is converging onto a single dominant
factor — a hallmark of stress and forced de-risking.

This detector tracks effective_rank over time and flags "collapsing" when
the current rank drops below a fraction of its 1-year median for several
consecutive days.

It also monitors the absorption_ratio (a complementary concentration metric)
and lambda_mp_ratio (how far the largest eigenvalue sits above the
Marchenko-Pastur noise bound) for a richer risk picture.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import StrategistConfig


@dataclass
class OMDSignal:
    """Output of the omd_finance collapse detector."""

    state: str  # "stable" | "collapsing" | "elevated"
    collapse_score: float  # 0 (no risk) .. 1 (extreme stress)
    effective_rank: float
    effective_rank_ratio: float  # current / 1y median
    absorption_ratio: float
    lambda_mp_ratio: float
    reason: str


def detect_collapse(
    spectral: pd.DataFrame,
    config: StrategistConfig,
) -> OMDSignal:
    """Detect spectral collapse from a history of spectral features.

    Parameters
    ----------
    spectral : DataFrame from rolling_spectral_features(), indexed by date.
    config : StrategistConfig with collapse thresholds.

    Returns
    -------
    OMDSignal
    """
    if len(spectral) < config.collapse_confirm_days:
        return OMDSignal(
            state="stable",
            collapse_score=0.0,
            effective_rank=float(spectral["effective_rank"].iloc[-1]),
            effective_rank_ratio=1.0,
            absorption_ratio=float(spectral["absorption_ratio"].iloc[-1]),
            lambda_mp_ratio=float(spectral["lambda_mp_ratio"].iloc[-1]),
            reason="insufficient history",
        )

    er = spectral["effective_rank"]
    ar = spectral["absorption_ratio"]
    lm = spectral["lambda_mp_ratio"]

    # 1-year median of effective rank (use at most 252 trading days).
    median_window = min(252, len(er) - 1)
    er_median = er.iloc[-median_window - 1 : -1].median()
    if er_median == 0 or np.isnan(er_median):
        er_ratio = 1.0
    else:
        er_ratio = float(er.iloc[-1] / er_median)

    # Collapse: current effective rank << median for N consecutive days.
    recent = er.iloc[-config.collapse_confirm_days :]
    collapse_days = int((recent < er_median * config.collapse_rank_ratio).sum())
    is_collapsing = collapse_days >= config.collapse_confirm_days

    # Composite collapse score in [0, 1].
    # Combine: rank ratio (lower = worse), absorption (higher = worse),
    # lambda_mp ratio (higher = more concentrated on market factor).
    rank_component = float(max(0.0, 1.0 - er_ratio))
    ar_component = float(max(0.0, (ar.iloc[-1] - 0.5) / 0.5))
    lm_component = float(max(0.0, min(1.0, (lm.iloc[-1] - 1.0) / 2.0)))
    collapse_score = float(np.clip(0.5 * rank_component + 0.3 * ar_component + 0.2 * lm_component, 0, 1))

    if is_collapsing:
        state = "collapsing"
        reason = (
            f"effective_rank={er.iloc[-1]:.1f} dropped to {er_ratio:.0%} of 1y median "
            f"for {collapse_days}/{config.collapse_confirm_days} days; "
            f"absorption={ar.iloc[-1]:.2f}, lambda_mp={lm.iloc[-1]:.2f}"
        )
    elif collapse_score > 0.5:
        state = "elevated"
        reason = (
            f"stress signals rising (score={collapse_score:.2f}): "
            f"rank_ratio={er_ratio:.0%}, absorption={ar.iloc[-1]:.2f}"
        )
    else:
        state = "stable"
        reason = (
            f"effective_rank={er.iloc[-1]:.1f} ({er_ratio:.0%} of median), "
            f"absorption={ar.iloc[-1]:.2f}, score={collapse_score:.2f}"
        )

    return OMDSignal(
        state=state,
        collapse_score=collapse_score,
        effective_rank=float(er.iloc[-1]),
        effective_rank_ratio=er_ratio,
        absorption_ratio=float(ar.iloc[-1]),
        lambda_mp_ratio=float(lm.iloc[-1]),
        reason=reason,
    )
