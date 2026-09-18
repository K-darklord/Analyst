"""Adapter package — auto-registers all shipped adapters with the registry.

Importing this package triggers @register_adapter decorators in each
adapter module. The DataSourceRegistry calls ``from . import adapters``
on first dispatch (lazy). New adapters = add a module here + import it
below; no edit to registry.py required.
"""

# Import order matters only for log readability; adapters don't depend on
# each other at registration time. Heavy SDK initialization (yfinance /
# akshare / tushare) is deferred to adapter __init__ or first call.
from . import yfinance_adapter     # noqa: F401
from . import akshare_adapter      # noqa: F401
from . import tushare_adapter      # noqa: F401  (Phase 2)

# Adapters not yet shipped:
# from . import eastmoney_guba
# from . import eastmoney_news

__all__ = ["yfinance_adapter", "akshare_adapter"]
