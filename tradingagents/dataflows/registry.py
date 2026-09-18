"""DataSourceRegistry — pluggable data source dispatch.

This module is the runtime counterpart of ``config/data_sources.yaml``.
It:

1. Loads the YAML config (single source of truth for which sources are
   enabled per (category, market)).
2. Lazily instantiates adapters on first use. Adapters are registered
   via the ``@register_adapter`` decorator (see ``adapters/__init__.py``).
3. Dispatches a capability call to the configured chain, walking from
   the highest-priority source to the lowest on recoverable errors
   (``NoMarketDataError``, ``VendorRateLimitError``,
   ``VendorNotConfiguredError``). Other exceptions propagate.

Translation layer:
- The legacy ``interface.route_to_vendor`` used coarse-grained categories
  (``core_stock_apis``, ``fundamental_data``, ``news_data``, ...) plus
  method-level vendor maps. The registry keeps the same coarse categories
  in the YAML (``market_data``, ``fundamentals``, ``sentiment``, ``news``)
  and translates them to fine-grained capabilities at dispatch time using
  ``base_adapter.CATEGORY_CAPABILITIES``. This means:

    registry.dispatch("market_data", "US", symbol="NVDA", start_date=...,
                      end_date=...)
    -> calls adapter.fetch_market_data(...)
    registry.dispatch("market_data", "US", capability="technical_indicators",
                      symbol="NVDA", ...)

  The optional ``capability`` kwarg lets a caller pick a fine-grained
  capability within a category; default is the first capability listed
  in ``CATEGORY_CAPABILITIES[category]``.

Phase 1 dispatch path:
- The legacy ``interface.route_to_vendor`` keeps working unchanged; it
  is the only caller of the registry for now (``route_to_vendor`` will
  be refactored to delegate here). Existing agent code that imports
  ``interface`` is untouched.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from .base_adapter import (
    BaseAdapter,
    CAPABILITIES,
    CAPABILITY_METHODS,
    CATEGORY_CAPABILITIES,
    CapabilityNotSupported,
    capabilities_for_category,
)
from .errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)
from .ticker_router import Market, route

logger = logging.getLogger(__name__)


# Errors that trigger chain-walking: the current source failed in a way
# that suggests trying the next one might succeed. Other errors (bugs,
# programming mistakes, schema mismatches) propagate so they surface
# loudly rather than being silently retried on a different source.
_CHAIN_WALKABLE_ERRORS = (
    NoMarketDataError,
    VendorRateLimitError,
    VendorNotConfiguredError,
)


# ---------------------------------------------------------------------------
# Adapter registration
# ---------------------------------------------------------------------------
# Adapters register themselves at import time via the @register_adapter
# decorator. The registry looks them up by name (matching the YAML
# ``source:`` field). This indirection keeps the registry free of imports
# of concrete adapter modules — adding a new adapter doesn't require
# editing registry.py, only shipping the module and importing it from
# ``adapters/__init__.py``.
_ADAPTER_CLASSES: dict[str, type[BaseAdapter]] = {}


def register_adapter(name: str) -> Callable[[type], type]:
    """Decorator: register a BaseAdapter subclass under ``name``.

    Usage::

        @register_adapter("yfinance")
        class YFinanceAdapter(BaseAdapter):
            ...
    """

    def _decorator(cls: type) -> type:
        if not issubclass(cls, BaseAdapter):
            raise TypeError(
                f"Adapter {cls!r} must inherit from BaseAdapter"
            )
        if name in _ADAPTER_CLASSES and _ADAPTER_CLASSES[name] is not cls:
            existing = _ADAPTER_CLASSES[name]
            logger.warning(
                "Adapter name '%s' re-registered: %s -> %s",
                name, existing.__name__, cls.__name__,
            )
        _ADAPTER_CLASSES[name] = cls
        # Set the name on the class so adapter instances report their
        # identity correctly to the logger and to the YAML source field.
        cls.name = name
        return cls

    return _decorator


def get_adapter_class(name: str) -> type[BaseAdapter] | None:
    """Return the registered adapter class for ``name``, or None."""
    return _ADAPTER_CLASSES.get(name)


def list_registered_adapters() -> list[str]:
    """Return a sorted list of all registered adapter names."""
    return sorted(_ADAPTER_CLASSES.keys())


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

# Default path to the YAML config. Override via the TRADINGAGENTS_DATA_SOURCES
# env var for testing or non-standard installs. Falls back to a sensible
# project-relative path when unset.
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "data_sources.yaml"


def _config_path_from_env() -> Path | None:
    """Return the YAML path from the env override, or None if unset."""
    raw = os.environ.get("TRADINGAGENTS_DATA_SOURCES")
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.exists():
        logger.warning("TRADINGAGENTS_DATA_SOURCES=%s does not exist", raw)
    return p


def load_config(path: Path | None = None) -> dict:
    """Load and validate the YAML data source config.

    Returns a dict shaped like::

        {
            "market_data": {"US": [{source, priority, enabled}, ...], ...},
            "fundamentals": {...},
            ...
        }

    Missing file -> empty dict (callers fall back to the legacy
    hard-coded chain). This keeps Phase 1 safe to roll out: if the YAML
    is missing or unreadable, the existing interface.py keeps working.
    """
    if path is None:
        path = _config_path_from_env() or _DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.warning("data_sources.yaml not found at %s; registry is empty", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Light validation: warn about unknown top-level keys.
    known = set(CATEGORY_CAPABILITIES.keys())
    unknown = set(cfg.keys()) - known
    if unknown:
        logger.warning(
            "Unknown data source categories in %s: %s (expected: %s)",
            path, sorted(unknown), sorted(known),
        )
    return cfg


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class DataSourceRegistry:
    """Pluggable data source dispatcher.

    Usage::

        reg = DataSourceRegistry()                       # loads default YAML
        reg.import_adapters()                              # discovers @register_adapter'd classes
        csv = reg.dispatch("market_data", "US",
                           symbol="NVDA",
                           start_date="2024-01-01",
                           end_date="2024-06-01")

    Dispatch contract:
    - Walks the configured chain for ``(category, market)`` in priority
      order (priority=1 first, then 2, ...). Disabled sources
      (``enabled: false``) are skipped.
    - Skips adapters that don't support the requested fine-grained
      capability (e.g. asking ``akshare`` for ``insider_transactions``
      is skipped silently — akshare doesn't support it).
    - Returns the first successful result. If every source fails with a
      chain-walkable error, raises ``NoMarketDataError`` describing what
      was tried.
    - If no source in the chain supports the capability, raises
      ``VendorNotConfiguredError``.
    """

    def __init__(self, config: dict | None = None, *, lazy: bool = True) -> None:
        # config shape: { category: { market: [{source, priority, enabled, config}, ...] } }
        self.config: dict = config if config is not None else load_config()
        # name -> BaseAdapter instance. Lazily populated on first use.
        self._instances: dict[str, BaseAdapter] = {}
        self._lazy = lazy
        if not lazy:
            self.import_adapters()

    # ------------------------------------------------------------------
    # Adapter discovery
    # ------------------------------------------------------------------
    def import_adapters(self) -> None:
        """Import the ``adapters`` subpackage so @register_adapter runs.

        Idempotent: importing the same module twice is a no-op. Safe to
        call from __init__ when ``lazy=False`` or on first dispatch.
        """
        try:
            from . import adapters  # noqa: F401 — side effect: registers adapters
        except ImportError as e:
            logger.warning(
                "Could not import adapters subpackage: %s. "
                "Registry will have no adapters available.", e,
            )

    def _get_instance(self, name: str, source_config: dict) -> BaseAdapter | None:
        """Return the cached adapter instance for ``name`` or instantiate.

        Returns None if the adapter class isn't registered (i.e. the YAML
        lists a source whose adapter hasn't shipped yet). In that case
        the dispatch chain skips it with a warning.
        """
        if name in self._instances:
            return self._instances[name]
        cls = get_adapter_class(name)
        if cls is None:
            logger.warning(
                "Adapter '%s' is configured but not registered (module not "
                "imported?). Skipping.", name,
            )
            return None
        try:
            inst = cls(config=source_config)
        except VendorNotConfiguredError as e:
            # Adapter refused to initialize (missing API key, etc.).
            # Log once and remember the miss so we don't retry-init on every
            # dispatch — the chain will skip this source until process restart.
            logger.info("Adapter '%s' not configured: %s", name, e)
            self._instances[name] = None  # type: ignore[assignment]
            return None
        except Exception as e:
            # Unexpected init failure: don't crash the whole dispatch, but
            # be loud about it so it gets noticed.
            logger.exception("Adapter '%s' failed to initialize: %s", name, e)
            self._instances[name] = None  # type: ignore[assignment]
            return None
        self._instances[name] = inst
        return inst

    # ------------------------------------------------------------------
    # Chain lookup
    # ------------------------------------------------------------------
    def _chain_for(
        self,
        category: str,
        market: Market,
    ) -> list[dict]:
        """Return the configured chain for ``(category, market)``.

        Sorted by priority ascending (1 before 2 before 3). Disabled
        entries are kept but flagged — the dispatcher skips them. An
        empty list means "no source configured for this (category, market)".
        """
        cat = self.config.get(category, {})
        chain = cat.get(market, [])
        # Defensive copy + sort by priority (stable for ties).
        return sorted(list(chain), key=lambda e: e.get("priority", 999))

    def _resolve_capability(
        self,
        category: str,
        capability: str | None,
    ) -> str:
        """Return the fine-grained capability name to dispatch on.

        If ``capability`` is given, validate it's under ``category`` and
        return it. Otherwise default to the first capability listed in
        ``CATEGORY_CAPABILITIES[category]``.
        """
        if capability:
            return capability
        caps = capabilities_for_category(category)
        if not caps:
            raise ValueError(f"No capabilities mapped for category '{category}'")
        # 'first' is well-defined because frozensets are unordered; pick a
        # stable default by sorting.
        return sorted(caps)[0]

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def dispatch(
        self,
        category: str,
        market_or_symbol: str,
        *,
        capability: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Dispatch a capability call to the configured source chain.

        Args:
            category: YAML category ('market_data', 'fundamentals',
                'sentiment', 'news').
            market_or_symbol: Either a Market key ('US' / 'A_SHARE' /
                'HK') or a ticker symbol. Symbols are routed via
                ``ticker_router.route`` to a Market key.
            capability: Optional fine-grained capability within the
                category (e.g. 'technical_indicators' under
                'market_data'). If omitted, the first capability listed
                under the category is used.
            **kwargs: Forwarded to the adapter method.

        Returns:
            The string payload from the first successful adapter.

        Raises:
            NoMarketDataError: every source in the chain failed with a
                chain-walkable error.
            VendorNotConfiguredError: no source in the chain supports the
                requested capability.
        """
        # Resolve market: callers can pass either a Market key or a ticker.
        market = market_or_symbol if market_or_symbol in ("US", "A_SHARE", "HK") else route(market_or_symbol)

        # Resolve the fine-grained capability to dispatch on.
        cap = self._resolve_capability(category, capability)
        if cap not in CAPABILITIES:
            raise ValueError(f"Unknown capability: {cap}")
        method_name = CAPABILITY_METHODS.get(cap)
        if not method_name:
            raise ValueError(f"No method mapped for capability {cap}")

        chain = self._chain_for(category, market)
        if not chain:
            raise VendorNotConfiguredError(
                f"No data sources configured for category='{category}' "
                f"market='{market}'. Add entries to config/data_sources.yaml."
            )

        # Lazy-import the adapters subpackage on first dispatch.
        if not self._instances and self._lazy:
            self.import_adapters()

        tried: list[str] = []
        last_error: Exception | None = None
        for entry in chain:
            source_name = entry.get("source")
            enabled = entry.get("enabled", True)
            if not enabled:
                logger.debug(
                    "Skipping disabled source '%s' for %s/%s",
                    source_name, category, market,
                )
                continue

            inst = self._get_instance(source_name, entry.get("config", {}) or {})
            if inst is None:
                # Adapter not registered or not configured; skip silently.
                continue

            # Skip adapters that don't declare this capability.
            if not inst.supports(cap):
                logger.debug(
                    "Adapter '%s' does not support capability '%s'; skipping",
                    source_name, cap,
                )
                continue

            method = getattr(inst, method_name, None)
            if method is None:
                logger.warning(
                    "Adapter '%s' claims to support '%s' but method '%s' "
                    "is missing. This is a bug in the adapter.",
                    source_name, cap, method_name,
                )
                continue

            tried.append(source_name)
            try:
                result = method(**kwargs)
                if result:
                    return result
                # Empty / None result from a non-raising adapter: treat as
                # no-data and walk the chain (mirrors the legacy router).
                logger.info(
                    "Adapter '%s' returned empty result for %s/%s; walking chain",
                    source_name, category, market,
                )
            except _CHAIN_WALKABLE_ERRORS as e:
                last_error = e
                logger.info(
                    "Adapter '%s' failed for %s/%s: %s; walking chain",
                    source_name, category, market, e,
                )
                continue

        if last_error is not None:
            raise NoMarketDataError(
                f"All configured sources failed for {category}/{market} "
                f"(capability: {cap}). Tried: {tried}. Last error: {last_error}"
            )
        # No adapter in the chain declared the capability.
        raise VendorNotConfiguredError(
            f"No configured source for category='{category}' market='{market}' "
            f"supports capability '{cap}'. Tried: {tried or 'none'}."
        )

    # ------------------------------------------------------------------
    # Introspection (used by the dashboard / debugging)
    # ------------------------------------------------------------------
    def describe_chain(self, category: str, market: Market) -> list[dict]:
        """Return a list of {source, priority, enabled, registered,
        supports_capabilities} for the (category, market) chain. Used by
        the dashboard's data-source status panel."""
        chain = self._chain_for(category, market)
        out = []
        for entry in chain:
            name = entry.get("source")
            cls = get_adapter_class(name) if name else None
            out.append({
                "source": name,
                "priority": entry.get("priority"),
                "enabled": entry.get("enabled", True),
                "registered": cls is not None,
                "capabilities": sorted(cls.supported_capabilities) if cls else [],
            })
        return out


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# A single registry instance per process. Lazy by default so importing
# the module doesn't trigger adapter SDK initialization (yfinance / akshare
# / tushare all do network calls at import time, which would slow startup
# and break offline tests). The first dispatch call triggers the import.
_registry: DataSourceRegistry | None = None


def get_registry() -> DataSourceRegistry:
    """Return the process-wide registry singleton.

    Loads the YAML config on first call. Subsequent calls return the
    cached instance. Tests that want a fresh registry can call
    ``DataSourceRegistry(config=...)`` directly and bypass this cache.
    """
    global _registry
    if _registry is None:
        _registry = DataSourceRegistry()
    return _registry


def reset_registry() -> None:
    """Drop the singleton. Primarily for tests."""
    global _registry
    _registry = None


__all__ = [
    "DataSourceRegistry",
    "get_registry",
    "reset_registry",
    "register_adapter",
    "get_adapter_class",
    "list_registered_adapters",
    "load_config",
]
