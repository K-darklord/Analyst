"""ORCA-style trend detector (v1: rule-based).

ORCA (arXiv:2604.17251) trains a Random Forest on 127 spectral + 79
traditional price features to predict 10-day-ahead rally/crash probabilities.

This v1 implementation approximates ORCA's signal with a transparent
rule-based model that combines:

  - Price momentum (short-horizon z-score of the equal-weight ETF basket)
  - Spectral dispersion (effective_rank trend — rising => healthy breadth)
  - Risk concentration (absorption_ratio — falling => diversification)
  - lambda_mp_ratio stability

Rally probability rises when momentum is positive AND spectral structure is
stable/expanding.  Crash probability rises when momentum is negative AND
absorption is high (concentration) — the classic "everyone heading for the
exit at once" pattern.

A future v2 will replace this with the trained RF model from ORCA.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import StrategistConfig


@dataclass
class ORCASignal:
    """Output of the ORCA trend detector."""

    rally_prob: float  # 0..1
    crash_prob: float  # 0..1
    regime: str  # "rally" | "crash" | "neutral"
    momentum_z: float
    effective_rank_trend: float  # slope of effective_rank over 20d (z-scored)
    absorption_z: float


def _zscore(x: pd.Series, window: int = 60) -> float:
    """Latest z-score of x over `window` (using trailing mean/std)."""
    if len(x) < 2:
        return 0.0
    w = x.iloc[-window:]
    mu, sigma = w.mean(), w.std(ddof=0)
    if sigma == 0 or np.isnan(sigma):
        return 0.0
    return float((x.iloc[-1] - mu) / sigma)


def detect_trend(
    returns: pd.DataFrame,
    spectral: pd.DataFrame,
    config: StrategistConfig,
) -> ORCASignal:
    """Detect short-term trend direction from returns + spectral history.

    Parameters
    ----------
    returns : full log-returns frame (ETF basket).
    spectral : rolling spectral features from rolling_spectral_features().
    config : StrategistConfig.
    """
    # --- 1. Momentum: equal-weight basket return over the forecast horizon ---
    basket = returns.mean(axis=1).dropna()
    if len(basket) < config.forecast_horizon + 5:
        return ORCASignal(0.5, 0.5, "neutral", 0.0, 0.0, 0.0)

    # Cumulative return over the forecast horizon (annualised-ish).
    recent_ret = basket.iloc[-config.forecast_horizon :].sum()
    # Z-score against 120-day history.
    roll_ret = basket.rolling(config.forecast_horizon).sum().dropna()
    mu, sigma = roll_ret.mean(), roll_ret.std(ddof=0)
    if sigma == 0 or np.isnan(sigma):
        momentum_z = 0.0
    else:
        momentum_z = float((recent_ret - mu) / sigma)

    # --- 2. Effective-rank trend (spectral breadth) ---
    er = spectral["effective_rank"].dropna()
    if len(er) >= 20:
        # Linear slope over 20 days, z-scored.
        slope = float(np.polyfit(range(len(er.iloc[-20:])), er.iloc[-20:].values, 1)[0])
        er_trend_z = _zscore(er.diff(20).dropna(), window=60) if len(er) > 80 else slope / max(er.std(), 1e-6)
    else:
        er_trend_z = 0.0

    # --- 3. Absorption ratio z-score (concentration) ---
    ar = spectral["absorption_ratio"].dropna()
    absorption_z = _zscore(ar, window=60) if len(ar) >= 2 else 0.0

    # --- 4. Probability model (rule-based v1) ---
    # Rally: positive momentum + expanding rank + falling absorption
    rally_score = (
        0.5 * np.tanh(momentum_z)
        + 0.25 * np.tanh(er_trend_z)
        - 0.25 * np.tanh(absorption_z)
    )
    # Crash: negative momentum + high absorption
    crash_score = (
        -0.5 * np.tanh(momentum_z)
        + 0.30 * np.tanh(absorption_z)
        + 0.20 * np.tanh(-er_trend_z)
    )

    # Map scores to [0, 1] probabilities via logistic.
    rally_prob = float(1.0 / (1.0 + np.exp(-3.0 * rally_score)))
    crash_prob = float(1.0 / (1.0 + np.exp(-3.0 * crash_score)))

    # Normalise so rally + crash <= 1 (residual = neutral).
    total = rally_prob + crash_prob
    if total > 1.0:
        rally_prob /= total
        crash_prob /= total

    if rally_prob >= config.rally_prob_threshold and rally_prob > crash_prob:
        regime = "rally"
    elif crash_prob >= config.crash_prob_threshold and crash_prob > rally_prob:
        regime = "crash"
    else:
        regime = "neutral"

    return ORCASignal(
        rally_prob=rally_prob,
        crash_prob=crash_prob,
        regime=regime,
        momentum_z=momentum_z,
        effective_rank_trend=float(er_trend_z),
        absorption_z=absorption_z,
    )
