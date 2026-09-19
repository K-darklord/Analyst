"""Combine ORCA trend + omd_finance structure into a single regime signal.

The combiner implements the signal matrix from STATUS_AND_ROADMAP.md:

  | ORCA trend | omd structure | combined regime    | position multiplier |
  |------------|---------------|--------------------|--------------------|
  | rally      | stable        | bull               | 1.2                |
  | rally      | collapsing    | divergence_top     | 0.3 (reduce)       |
  | crash      | stable        | correction         | 0.5                |
  | crash      | collapsing    | crisis             | 0.0 (risk-off)     |
  | neutral    | stable        | neutral            | 0.8                |
  | neutral    | collapsing    | pre_crisis         | 0.4                |

The position multiplier is applied to Analyst-driven position sizes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import StrategistConfig
from .omd_detector import OMDSignal
from .orca_detector import ORCASignal


@dataclass
class CombinedSignal:
    """Final Strategist output consumed by the Manager."""

    regime: str
    position_override: float
    trend: dict
    tail_risk: dict
    reason: str


# Mapping: (orca_regime, omd_state) -> combined regime
_REGIME_MAP: dict[tuple[str, str], str] = {
    ("rally", "stable"): "bull",
    ("rally", "elevated"): "rally",
    ("rally", "collapsing"): "divergence_top",
    ("crash", "stable"): "correction",
    ("crash", "elevated"): "correction",
    ("crash", "collapsing"): "crisis",
    ("neutral", "stable"): "neutral",
    ("neutral", "elevated"): "neutral",
    ("neutral", "collapsing"): "pre_crisis",
}


def combine_signals(
    orca: ORCASignal,
    omd: OMDSignal,
    config: StrategistConfig,
) -> CombinedSignal:
    """Combine ORCA trend and omd structure signals.

    Parameters
    ----------
    orca : output of orca_detector.detect_trend()
    omd : output of omd_detector.detect_collapse()
    config : StrategistConfig

    Returns
    -------
    CombinedSignal with regime, position multiplier, and sub-signals.
    """
    regime = _REGIME_MAP.get((orca.regime, omd.state), "neutral")

    position_override = config.position_multipliers.get(regime, 0.8)

    trend = {
        "regime": orca.regime,
        "rally_prob": round(orca.rally_prob, 4),
        "crash_prob": round(orca.crash_prob, 4),
        "momentum_z": round(orca.momentum_z, 3),
    }
    tail_risk = {
        "state": omd.state,
        "collapse_score": round(omd.collapse_score, 4),
        "effective_rank": round(omd.effective_rank, 2),
        "effective_rank_ratio": round(omd.effective_rank_ratio, 3),
        "absorption_ratio": round(omd.absorption_ratio, 3),
    }

    reason = (
        f"ORCA={orca.regime}(rally={orca.rally_prob:.2f}/crash={orca.crash_prob:.2f}) "
        f"+ OMD={omd.state}(score={omd.collapse_score:.2f}) => {regime} "
        f"(position x{position_override})"
    )

    return CombinedSignal(
        regime=regime,
        position_override=float(position_override),
        trend=trend,
        tail_risk=tail_risk,
        reason=reason,
    )
