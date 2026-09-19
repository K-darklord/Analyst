import sys
import logging
import json

from .strategist import run_strategist

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
mkt = sys.argv[1] if len(sys.argv) > 1 else "US"
date = sys.argv[2] if len(sys.argv) > 2 else None
signal = run_strategist(market=mkt, end_date=date)
print(json.dumps(signal, indent=2, ensure_ascii=False, default=str))
