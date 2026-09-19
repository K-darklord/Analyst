"""Strategist — main entry point.

Run the full Strategist pipeline:
  1. Fetch ETF basket returns (US + A-share).
  2. Compute rolling spectral features (RMT).
  3. ORCA trend detection (rally/crash/neutral).
  4. omd_finance spectral collapse detection.
  5. Combine into a regime signal with position override.

Usage
-----
    from strategist import run_strategist
    signal = run_strategist(market="US")   # or "ASHARE"
    print(signal.regime, signal.position_override)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Literal

import pandas as pd

from .config import ASHARE_ETFS, DEFAULT_CONFIG, US_ETFS, StrategistConfig
from .data_fetcher import build_returns_matrix
from .omd_detector import detect_collapse
from .orca_detector import detect_trend
from .signal_combiner import combine_signals
from .spectral_features import rolling_spectral_features

logger = logging.getLogger(__name__)

Market = Literal["US", "ASHARE"]


class Strategist:
    """Top-down market regime detector."""

    def __init__(
        self,
        market: Market = "US",
        config: StrategistConfig | None = None,
    ) -> None:
        self.market = market
        self.config = config or DEFAULT_CONFIG
        self._etfs = US_ETFS if market == "US" else ASHARE_ETFS

    def run(self, end_date: str | None = None) -> dict:
        """Run the full pipeline and return the combined signal as a dict.

        Parameters
        ----------
        end_date : ISO date string (YYYY-MM-DD). Defaults to today.

        Returns
        -------
        dict with keys:
            market, as_of, regime, position_override,
            trend (orca), tail_risk (omd), reason, spectral (latest)
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("Strategist running for %s as of %s", self.market, end_date)

        # 1. Fetch returns
        returns = build_returns_matrix(
            self._etfs,
            end_date=end_date,
            lookback_days=400,  # ~1.5y to have enough history for 1y median
        )
        if returns.shape[0] < self.config.lookback_window + 10:
            logger.warning(
                "Only %d days of returns (need %d+); results may be unreliable",
                returns.shape[0],
                self.config.lookback_window + 10,
            )

        # 2. Rolling spectral features
        spectral = rolling_spectral_features(
            returns,
            window=self.config.lookback_window,
            top_k=self.config.absorption_top_k,
        )
        if spectral.empty:
            raise RuntimeError("Spectral feature computation failed")

        # 3. ORCA trend detection
        orca = detect_trend(returns, spectral, self.config)

        # 4. omd collapse detection
        omd = detect_collapse(spectral, self.config)

        # 5. Combine
        combined = combine_signals(orca, omd, self.config)

        result = {
            "market": self.market,
            "as_of": end_date,
            "regime": combined.regime,
            "position_override": combined.position_override,
            "trend": combined.trend,
            "tail_risk": combined.tail_risk,
            "reason": combined.reason,
            "spectral_latest": {
                k: round(v, 4) for k, v in spectral.iloc[-1].to_dict().items()
            },
        }

        logger.info("Strategist result: %s", combined.reason)
        return result


def run_strategist(
    market: Market = "US",
    end_date: str | None = None,
    config: StrategistConfig | None = None,
) -> dict:
    """Convenience wrapper: run the Strategist and return the signal dict."""
    return Strategist(market=market, config=config).run(end_date=end_date)


if __name__ == "__main__":
    # CLI: python -m strategist [US|ASHARE] [YYYY-MM-DD]
    import sys

    mkt: Market = sys.argv[1] if len(sys.argv) > 1 else "US"
    date = sys.argv[2] if len(sys.argv) > 2 else None
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    signal = run_strategist(market=mkt, end_date=date)
    print(json.dumps(signal, indent=2, ensure_ascii=False, default=str))
