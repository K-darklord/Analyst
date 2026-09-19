"""Xueqiu (雪球) adapter — A-share/HK social discussion sentiment.

Fetches per-stock discussion posts from xueqiu.com by scraping the stock
page's discussion timeline. Xueqiu applies Aliyun WAF protection that
blocks plain HTTP requests and standard Playwright/Chromium automation;
this adapter uses ``undetected-chromedriver`` (a patched Chrome) loading
the user's existing Chrome profile (which holds the Xueqiu login + WAF
cookies).  No cookie extraction or decryption is needed.

Configuration (one of):
  - ``XUEQIU_CHROME_PROFILE`` env var: path to a Chrome profile dir that is
    logged in to xueqiu.com (e.g. ``~/Library/Application Support/Google/Chrome/Profile 1``).
  - Or ``chrome_profile`` in the adapter's YAML config.
  - If unset, falls back to the system Chrome Default profile.

The adapter copies the profile to a temp dir before launching (to avoid
locking the user's running Chrome), then scrapes ``article`` elements
using selectors: ``.user-name`` (author), ``.content`` (post body),
``.timeline__item__info`` (time + device).

If the profile is not configured or undetected-chromedriver is unavailable,
the adapter raises ``NoMarketDataError`` so the registry walks to the next
sentiment source (eastmoney_guba / capital-flow proxy).

Capability: sentiment
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any

from tradingagents.dataflows.base_adapter import BaseAdapter
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.registry import register_adapter
from tradingagents.dataflows.ticker_router import is_ashare, is_hk, strip_suffix, exchange_tag

logger = logging.getLogger(__name__)

# Default Chrome profile locations by OS
_DEFAULT_PROFILE_CANDIDATES = [
    os.path.expanduser("~/Library/Application Support/Google/Chrome/Default"),       # macOS
    os.path.expanduser("~/.config/google-chrome/Default"),                            # Linux
    os.path.expanduser("~/AppData/Local/Google/Chrome/User Data/Default"),            # Windows
]


def _resolve_chrome_profile(config: dict) -> str:
    """Return the Chrome profile directory to use, or '' if none found."""
    profile = config.get("chrome_profile") or os.environ.get("XUEQIU_CHROME_PROFILE", "")
    if profile:
        return profile
    for candidate in _DEFAULT_PROFILE_CANDIDATES:
        if os.path.isdir(candidate):
            return candidate
    return ""


@register_adapter("xueqiu")
class XueqiuAdapter(BaseAdapter):
    """Fetches stock discussion posts from Xueqiu (雪球)."""

    name = "xueqiu"

    supported_capabilities = frozenset({"sentiment"})

    def initialize(self) -> None:
        self._chrome_profile = _resolve_chrome_profile(self.config)

    def _xueqiu_symbol(self, symbol: str) -> str:
        """Convert a ticker to Xueqiu's symbol format (e.g. SH688521 / SZ000001 / HK00700)."""
        code = strip_suffix(symbol)
        tag = exchange_tag(symbol)
        if is_ashare(symbol):
            return f"{tag}{code}"
        if is_hk(symbol):
            return f"HK{code.zfill(5)}"
        return symbol

    # ------------------------------------------------------------------
    # Sentiment (discussion posts)
    # ------------------------------------------------------------------
    def fetch_news(self, symbol: str, **kwargs: Any) -> str:
        return self.fetch_sentiment(symbol, **kwargs)

    def fetch_sentiment(self, symbol: str, **kwargs: Any) -> str:
        """Fetch recent Xueqiu discussion posts for a stock.

        Uses undetected-chromedriver with the user's Chrome profile to load
        the stock page (bypasses Aliyun WAF) and scrapes ``article`` elements.

        Args:
            symbol: A-share or HK ticker.
            limit: max posts (default 20).
        """
        if not (is_ashare(symbol) or is_hk(symbol)):
            raise NoMarketDataError(
                symbol, symbol,
                "xueqiu only supports A-share (.SH/.SZ/.BJ) and HK (.HK) tickers",
            )

        if not self._chrome_profile:
            raise NoMarketDataError(
                symbol, symbol,
                "no Chrome profile found — set XUEQIU_CHROME_PROFILE to a "
                "Chrome profile logged in to xueqiu.com",
            )

        try:
            import time
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
        except ImportError as e:
            raise NoMarketDataError(
                symbol, symbol, f"undetected-chromedriver not installed: {e}",
            ) from e

        xq_symbol = self._xueqiu_symbol(symbol)
        limit = int(kwargs.get("limit", 20))
        page_url = f"https://xueqiu.com/S/{xq_symbol}"

        # Copy the profile to a temp dir so we don't lock the user's Chrome.
        tmp_profile = tempfile.mkdtemp(prefix="xq_chrome_")
        driver = None
        try:
            shutil.copytree(self._chrome_profile, tmp_profile, dirs_exist_ok=True)

            opts = uc.ChromeOptions()
            opts.add_argument("--headless")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument(f"--user-data-dir={tmp_profile}")
            opts.add_argument("--profile-directory=.")

            driver = uc.Chrome(options=opts, version_main=None)
            driver.get(page_url)
            # Wait for the discussion timeline to render.
            time.sleep(10)

            articles = driver.find_elements(By.CSS_SELECTOR, "article")
            posts = []
            for el in articles[:limit]:
                try:
                    user = el.find_element(By.CSS_SELECTOR, ".user-name").text
                except Exception:
                    user = ""
                try:
                    info = el.find_element(By.CSS_SELECTOR, ".timeline__item__info").text
                except Exception:
                    info = ""
                try:
                    content = el.find_element(By.CSS_SELECTOR, ".content").text
                except Exception:
                    content = ""
                content = (content or "").strip()[:200]
                if content:
                    posts.append({"user": user, "info": info, "content": content})

            if not posts:
                raise NoMarketDataError(
                    symbol, xq_symbol, "no discussion posts found on page",
                )

        except NoMarketDataError:
            raise
        except Exception as e:
            raise NoMarketDataError(
                symbol, xq_symbol, f"xueqiu scrape via undetected-chromedriver failed: {e}",
            ) from e
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            shutil.rmtree(tmp_profile, ignore_errors=True)

        lines = [
            f"[{p['info']}] {p['content']}  —{p['user']}"
            for p in posts
        ]
        header = (
            f"## 雪球 discussion posts for {symbol} (symbol={xq_symbol})\n"
            f"# Source: xueqiu.com (scraped from page)\n"
            f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# Post count: {len(posts)}\n\n"
        )
        return header + "\n".join(lines)
