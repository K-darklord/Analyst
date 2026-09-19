"""Configuration for the Strategist component.

Defines the ETF baskets used for spectral analysis, rolling window parameters,
and regime detection thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# ETF baskets — used to build the correlation matrix for spectral analysis.
# ORCA uses 24 US ETFs; we extend to both US and A-share so the Strategist can
# cover both markets. Each entry is (ticker, vendor, name).
# ---------------------------------------------------------------------------

US_ETFS: list[tuple[str, str, str]] = [
    # broad market
    ("SPY", "yfinance", "S&P 500"),
    ("QQQ", "yfinance", "Nasdaq 100"),
    ("DIA", "yfinance", "Dow 30"),
    ("IWM", "yfinance", "Russell 2000"),
    # sectors
    ("XLK", "yfinance", "Technology"),
    ("XLF", "yfinance", "Financials"),
    ("XLE", "yfinance", "Energy"),
    ("XLV", "yfinance", "Healthcare"),
    ("XLI", "yfinance", "Industrials"),
    ("XLY", "yfinance", "Consumer Discretionary"),
    ("XLP", "yfinance", "Consumer Staples"),
    ("XLU", "yfinance", "Utilities"),
    ("XLRE", "yfinance", "Real Estate"),
    ("XLC", "yfinance", "Communication"),
    # factors
    ("MTUM", "yfinance", "Momentum"),
    ("QUAL", "yfinance", "Quality"),
    ("SIZE", "yfinance", "Size"),
    ("USMV", "yfinance", "Min Vol"),
]

# A-share: 申万一级行业指数 via tushare sw_daily (31 sectors).
# Using sector indices instead of ETFs because:
#   1. tushare sw_daily is stable (we have 10000 points); eastmoney ETF API is WAF-blocked
#   2. 31 sectors give good cross-sectional diversity for the correlation matrix
#   3. Sector-level data is exactly what the Strategist needs for regime detection
ASHARE_ETFS: list[tuple[str, str, str]] = [
    ("801010.SI", "tushare_sw", "农林牧渔"),
    ("801020.SI", "tushare_sw", "采掘"),
    ("801030.SI", "tushare_sw", "化工"),
    ("801040.SI", "tushare_sw", "钢铁"),
    ("801050.SI", "tushare_sw", "有色金属"),
    ("801080.SI", "tushare_sw", "电子"),
    ("801110.SI", "tushare_sw", "家用电器"),
    ("801120.SI", "tushare_sw", "食品饮料"),
    ("801130.SI", "tushare_sw", "纺织服装"),
    ("801140.SI", "tushare_sw", "轻工制造"),
    ("801150.SI", "tushare_sw", "医药生物"),
    ("801160.SI", "tushare_sw", "公用事业"),
    ("801170.SI", "tushare_sw", "交通运输"),
    ("801180.SI", "tushare_sw", "房地产"),
    ("801200.SI", "tushare_sw", "商贸零售"),
    ("801210.SI", "tushare_sw", "社会服务"),
    ("801230.SI", "tushare_sw", "综合"),
    ("801710.SI", "tushare_sw", "建筑材料"),
    ("801720.SI", "tushare_sw", "建筑装饰"),
    ("801730.SI", "tushare_sw", "电力设备"),
    ("801740.SI", "tushare_sw", "国防军工"),
    ("801750.SI", "tushare_sw", "计算机"),
    ("801760.SI", "tushare_sw", "传媒"),
    ("801770.SI", "tushare_sw", "通信"),
    ("801780.SI", "tushare_sw", "银行"),
    ("801790.SI", "tushare_sw", "非银金融"),
    ("801880.SI", "tushare_sw", "汽车"),
    ("801890.SI", "tushare_sw", "机械设备"),
    ("801950.SI", "tushare_sw", "煤炭"),
    ("801960.SI", "tushare_sw", "石油石化"),
    ("801980.SI", "tushare_sw", "美容护理"),
]


@dataclass
class StrategistConfig:
    """Rolling-window and regime-detection parameters."""

    # Lookback window (trading days) for the correlation matrix.
    # ORCA uses 60-120 days; we default to 90.
    lookback_window: int = 90

    # Forecast horizon in trading days. ORCA targets 10 days.
    forecast_horizon: int = 10

    # --- omd_finance spectral-collapse thresholds ---
    # Effective-rank collapse: if current effective rank drops below this
    # fraction of its 1-year median, flag "collapsing".
    collapse_rank_ratio: float = 0.60
    # Number of consecutive days the rank must be below threshold to confirm.
    collapse_confirm_days: int = 5

    # --- ORCA trend thresholds (rule-based v1; ML model comes later) ---
    # Probability (0..1) thresholds for regime classification.
    rally_prob_threshold: float = 0.55
    crash_prob_threshold: float = 0.55
    # Absorption ratio (fraction of variance explained by top-k eigenvectors).
    # High absorption => concentrated risk.
    absorption_top_k: int = 3
    absorption_high_threshold: float = 0.75

    # --- position override ---
    # Multipliers applied to Analyst-driven position sizes based on regime.
    position_multipliers: dict[str, float] = field(default_factory=lambda: {
        "bull": 1.2,
        "rally": 1.0,
        "neutral": 0.8,
        "correction": 0.5,
        "crisis": 0.0,       # full risk-off
        "divergence_top": 0.3,  # trend up but structure collapsing
    })


DEFAULT_CONFIG = StrategistConfig()
