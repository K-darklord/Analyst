"""Shared Data Catalog adapter -- delegate fetches to FundTeam's Data Catalog.

Instead of scraping guba / reddit / stocktwits / xueqiu locally, the Analyst
asks FundTeam's shared ``/api/data/fetch`` endpoint, which walks the
declarative source chain in ``data_catalog.yaml``. The local adapters stay
registered as lower-priority fallback, so a FundTeam outage degrades to the
existing in-repo behaviour.

Transport mirrors ``agents/utils/decision_log_hook.py``: a plain
``urllib.request`` POST carrying the shared service token in the
``X-Internal-Token`` header. No import-time dependency on FundTeam, and no
network call until the first dispatch.

Capabilities served: market_data / fundamentals / news / sentiment. The
finer-grained statement (balance_sheet / cashflow / income_statement),
insider_transactions, global_news and macro capabilities stay on the local
adapters until the shared catalog grows those data types.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime
from typing import Any

from ..base_adapter import BaseAdapter
from ..errors import NoMarketDataError
from ..registry import register_adapter

logger = logging.getLogger(__name__)

# Fine-grained capability -> catalog data_type.
_CAP_TO_DATATYPE = {
    "market_data": "market_data",
    "fundamentals": "fundamentals",
    "news": "news",
    "sentiment": "sentiment",
}


def _days_between(start_date: str, end_date: str) -> int:
    """Inclusive day count between two ISO dates, or 0 when underivable."""
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
        return max((e - s).days + 1, 1)
    except Exception:
        return 0


@register_adapter("shared_catalog")
class SharedCatalogAdapter(BaseAdapter):
    """Fetches data from FundTeam's shared Data Catalog over HTTP."""

    name = "shared_catalog"

    supported_capabilities = frozenset(_CAP_TO_DATATYPE.keys())

    def initialize(self) -> None:
        # Remote endpoint + shared service token, mirroring decision_log_hook.
        self._base_url = os.environ.get("FUNDTEAM_URL", "http://127.0.0.1:8080")
        self._token = os.environ.get("DECISION_LOG_TOKEN", "")
        self._timeout = float(self.config.get("timeout", 5.0))
        if not self._token:
            self.logger.info(
                "DECISION_LOG_TOKEN not set; shared_catalog will be skipped "
                "on any authenticated FundTeam deployment (falls back to the "
                "local chain)."
            )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        req = urllib.request.Request(
            f"{self._base_url}/api/data/fetch",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 -- network/HTTP/JSON -> chain walk
            raise NoMarketDataError(
                payload.get("symbol") or payload.get("data_type", "?"),
                payload.get("symbol") or "",
                f"shared catalog fetch failed: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # Capability methods
    # ------------------------------------------------------------------
    def _fetch(self, data_type: str, symbol: str, **kwargs: Any) -> str:
        result = self._post({"data_type": data_type, "symbol": symbol, **kwargs})
        if not result.get("ok"):
            raise NoMarketDataError(
                symbol, symbol,
                result.get("error") or "shared catalog returned ok=false",
            )
        return self._stringify(result)

    def fetch_market_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        **kwargs: Any,
    ) -> str:
        days = _days_between(start_date, end_date) or int(kwargs.get("days", 30))
        return self._fetch("market_data", symbol, days=days)

    def fetch_fundamentals(self, symbol: str, **kwargs: Any) -> str:
        return self._fetch("fundamentals", symbol)

    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        days = _days_between(
            kwargs.get("start_date", ""), kwargs.get("end_date", "")
        ) or int(kwargs.get("days", 30))
        return self._fetch("news", symbol, days=days)

    def fetch_sentiment(self, symbol: str, **kwargs: Any) -> str:
        days = int(kwargs.get("days", 30) or 30)
        return self._fetch("sentiment", symbol, days=days)

    # ------------------------------------------------------------------
    # Formatting (catalog result dict -> agent-facing text block)
    # ------------------------------------------------------------------
    def _meta_header(self, result: dict) -> str:
        parts = [
            f"# Shared Data Catalog | data_type={result.get('data_type')}",
            f"# source={result.get('source') or 'unknown'} "
            f"| evidence={result.get('evidence_grade') or 'unknown'}",
        ]
        asof = result.get("as_of")
        if asof:
            parts[-1] += f" | as_of={asof}"
        return "\n".join(parts) + "\n"

    def _stringify(self, result: dict) -> str:
        data = result.get("data")
        meta = self._meta_header(result)
        if isinstance(data, str):
            return meta + data + "\n"
        if isinstance(data, dict):
            if "dates" in data or "close" in data:
                return meta + self._ohlcv(data) + "\n"
            if "items" in data:
                return meta + self._items(data) + "\n"
            return meta + self._kvv(data) + "\n"
        return meta + json.dumps(data, ensure_ascii=False, default=str) + "\n"

    @staticmethod
    def _ohlcv(data: dict) -> str:
        dates = data.get("dates") or []
        close = data.get("close") or []
        volume = data.get("volume") or []
        lines = ["date,close,volume"]
        for i in range(min(len(dates), len(close))):
            vol = volume[i] if i < len(volume) else ""
            lines.append(f"{dates[i]},{close[i]},{vol}")
        return "\n".join(lines)

    @staticmethod
    def _items(data: dict) -> str:
        lines = []
        for it in data.get("items") or []:
            if not isinstance(it, dict):
                lines.append(f"- {it}")
                continue
            date = it.get("date", "")
            title = it.get("title") or it.get("body") or ""
            extra = " | ".join(
                x for x in (it.get("source", ""), it.get("sentiment", "")) if x
            )
            head = f"[{date}] {title}" if date else title
            if extra:
                head += f"  ({extra})"
            lines.append(f"- {head}")
            body = it.get("body")
            if body and title and body != title:
                lines.append(f"  {body}")
        return "\n".join(lines)

    @staticmethod
    def _kvv(data: dict) -> str:
        lines = []
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{key}: {json.dumps(val, ensure_ascii=False, default=str)}")
            else:
                lines.append(f"{key}: {val}")
        return "\n".join(lines)
