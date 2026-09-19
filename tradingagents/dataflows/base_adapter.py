"""Abstract base class for pluggable data source adapters.

The adapter layer is the runtime side of `config/data_sources.yaml`. Each
adapter declares which capabilities it supports and provides concrete
implementations for those capabilities. The DataSourceRegistry walks the
configured chain per (category, market) and dispatches a call to the first
adapter that both supports the capability and successfully returns data.

Design notes:
- Adapters return the SAME string payloads the legacy vendor functions
  returned (CSV for OHLCV, markdown-ish text for fundamentals, etc.). The
  agents consume those strings directly in their LLM prompts, so changing
  the return type is a Phase-later concern. The base class therefore does
  not impose a structured schema; it only standardizes HOW adapters are
  discovered and dispatched.
- Capabilities are declared as a class-level frozenset so the registry can
  filter adapters without instantiating them or sniffing methods.
- Each capability method raises `CapabilityNotSupported` if the adapter
  forgot to override it. This catches "I claimed I support X but didn't
  implement it" bugs loudly rather than silently returning None.
- Errors from underlying calls should surface as NoMarketDataError /
  VendorRateLimitError / VendorNotConfiguredError (already used by the
  legacy routing layer) so the registry can decide whether to walk the
  chain. Other exceptions propagate — they indicate real bugs.
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Any, Iterable

from .errors import NoMarketDataError, VendorError

logger = logging.getLogger(__name__)


# All capabilities known to the registry. Adding a new capability = add a
# row here, expose it on the base class, and ship at least one adapter.
# Capabilities map 1:1 to the method categories in interface.py so the
# legacy VENDOR_METHODS dispatch can be replaced row-by-row without
# changing call sites in the agents.
CAPABILITIES = frozenset(
    {
        "market_data",            # OHLCV history (formerly get_stock_data)
        "technical_indicators",   # stockstats indicators (formerly get_indicators)
        "fundamentals",           # company snapshot (formerly get_fundamentals)
        "balance_sheet",          # balance sheet statement
        "cashflow",               # cash flow statement
        "income_statement",       # income statement
        "news",                   # ticker-scoped news
        "global_news",            # macro / market-wide news
        "insider_transactions",   # insider buys/sells
        "macro_data",             # macroeconomic indicators (CN: cpi/pmi/shibor; US: FRED)
        "sentiment",              # social-media / forum sentiment (Reddit, Guba, Xueqiu)
    }
)


class CapabilityNotSupported(VendorError):
    """Raised when an adapter is asked for a capability it did not declare.

    This is a programming error (the registry should not have dispatched
    here), so it surfaces loudly rather than being swallowed by the
    chain-walking logic.
    """


class BaseAdapter(ABC):
    """Abstract base for all data source adapters.

    Subclasses MUST:
      - set `name` to a unique adapter identifier (matches the `source`
        field in data_sources.yaml)
      - set `supported_capabilities` to a frozenset subset of CAPABILITIES
      - implement every method whose capability appears in
        `supported_capabilities`

    Subclasses MAY:
      - override `initialize(self, config)` to read credentials from the
        per-source `config:` block in data_sources.yaml. The default
        implementation stores the dict on `self.config` for later use.
      - override `healthcheck(self)` to short-circuit a failing source
        before the first real call. Default returns True.
    """

    # Unique adapter identifier. MUST match the YAML `source:` field.
    name: str = "base"

    # Subset of CAPABILITIES this adapter can serve. The registry uses
    # this to filter the configured chain before dispatching.
    supported_capabilities: frozenset = frozenset()

    def __init__(self, config: dict | None = None) -> None:
        # Per-source `config:` block from data_sources.yaml. Subclasses
        # read credentials, rate limits, etc. out of here in initialize().
        self.config: dict = dict(config or {})
        self.logger = logging.getLogger(f"{__name__}.{self.name}")
        self.initialize()

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """Optional setup hook. Runs once at construction.

        Override to validate credentials, set up SDK clients, etc. Default
        is a no-op. Raise VendorNotConfiguredError here if the source
        cannot be used (missing API key, unreachable host, etc.) — the
        registry will skip it as if `enabled: false`.
        """

    def healthcheck(self) -> bool:
        """Return True if the adapter is ready to serve requests.

        Override for an explicit liveness probe (e.g. ping the API).
        Default True so the registry dispatches optimistically; failures
        surface through the normal call path.
        """
        return True

    # ------------------------------------------------------------------
    # Capability dispatch
    # ------------------------------------------------------------------
    def supports(self, capability: str) -> bool:
        """Return True if this adapter declares support for `capability`."""
        return capability in self.supported_capabilities

    def _not_supported(self, capability: str) -> "Any":  # pragma: no cover - defensive
        raise CapabilityNotSupported(
            f"Adapter '{self.name}' does not implement '{capability}'. "
            f"Declared capabilities: {sorted(self.supported_capabilities)}."
        )

    # ------------------------------------------------------------------
    # Capability methods. Each maps to a former VENDOR_METHODS entry.
    # Adapters only override the methods for capabilities they declared.
    # ------------------------------------------------------------------
    def fetch_market_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        **kwargs: Any,
    ) -> str:
        return self._not_supported("market_data")

    def fetch_technical_indicators(
        self,
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int,
        **kwargs: Any,
    ) -> str:
        return self._not_supported("technical_indicators")

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("fundamentals")

    def fetch_balance_sheet(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("balance_sheet")

    def fetch_cashflow(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("cashflow")

    def fetch_income_statement(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("income_statement")

    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("news")

    def fetch_global_news(self, **kwargs: Any) -> str:
        return self._not_supported("global_news")

    def fetch_insider_transactions(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("insider_transactions")

    def fetch_macro_data(self, indicator: str, **kwargs: Any) -> str:
        return self._not_supported("macro_data")

    def fetch_sentiment(self, symbol: str, **kwargs: Any) -> str:
        return self._not_supported("sentiment")


# Mapping from capability name to the base-class method that serves it.
# The registry uses this to call the right method without reflective
# getattr() tricks. New capabilities = add a row here + a method above.
CAPABILITY_METHODS: dict[str, str] = {
    "market_data": "fetch_market_data",
    "technical_indicators": "fetch_technical_indicators",
    "fundamentals": "fetch_fundamentals",
    "balance_sheet": "fetch_balance_sheet",
    "cashflow": "fetch_cashflow",
    "income_statement": "fetch_income_statement",
    "news": "fetch_news",
    "global_news": "fetch_global_news",
    "insider_transactions": "fetch_insider_transactions",
    "macro_data": "fetch_macro_data",
    "sentiment": "fetch_sentiment",
}


# Capabilities that map to the legacy "fundamental_data" category. The
# registry uses these groupings to translate between the YAML's
# coarse-grained categories (market_data / fundamentals / sentiment /
# news) and the fine-grained capability names that adapters expose.
CATEGORY_CAPABILITIES: dict[str, frozenset[str]] = {
    "market_data": frozenset({"market_data", "technical_indicators"}),
    "fundamentals": frozenset(
        {"fundamentals", "balance_sheet", "cashflow", "income_statement", "insider_transactions"}
    ),
    "sentiment": frozenset({"sentiment"}),
    "news": frozenset({"news", "global_news"}),
    "macro_data": frozenset({"macro_data"}),
}


def capabilities_for_category(category: str) -> frozenset[str]:
    """Return the set of fine-grained capabilities under a YAML category."""
    return CATEGORY_CAPABILITIES.get(category, frozenset())


__all__ = [
    "BaseAdapter",
    "CAPABILITIES",
    "CAPABILITY_METHODS",
    "CATEGORY_CAPABILITIES",
    "CapabilityNotSupported",
    "capabilities_for_category",
]
