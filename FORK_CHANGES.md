# Fork Changes — K-darklord/TradingAgents

This file is the **fork-vs-upstream change ledger**. It lists every file this
fork adds or modifies relative to `TauricResearch/TradingAgents` upstream,
explains the architectural separation that keeps merges tractable, and gives
the merge recipe for pulling in future upstream releases.

Upstream remote: `origin` → `https://github.com/TauricResearch/TradingAgents.git`
Fork remote:     `myfork` → `https://github.com/K-darklord/TradingAgents.git`
Base tag:        `v0.5.0` (merged via PR #1364)

---

## Architectural separation — the registry indirection layer

The fork's design rule is: **our extensions live behind an indirection that
the upstream does not depend on**. Concretely:

```
            ┌─────────────────────────────────────────────┐
   agents → │ tradingagents/dataflows/interface.py        │ ← upstream file, fork-patched
            │   route_to_vendor(method, *args, **kwargs)  │
            │     ├─ FORK: try registry.dispatch(...)      │ ← our addition
            │     └─ legacy: VENDOR_METHODS chain          │ ← upstream code
            └────────────────────┬────────────────────────┘
                                 │ falls back to
            ┌────────────────────▼────────────────────────┐
   registry │ tradingagents/dataflows/registry.py          │ ← fork-only
   layer    │ tradingagents/dataflows/base_adapter.py      │ ← fork-only
            │ tradingagents/dataflows/ticker_router.py     │ ← fork-only
            │ tradingagents/dataflows/normalizer.py        │ ← fork-only
            │ tradingagents/dataflows/adapters/*           │ ← fork-only subpackage
            └─────────────────────────────────────────────┘
                                 │ reads
            ┌────────────────────▼────────────────────────┐
   config   │ config/data_sources.yaml                    │ ← fork-only
            └─────────────────────────────────────────────┘
```

**Why this keeps merges clean:**

1. The only upstream file we patch is `interface.py`. The patch is
   a clearly-marked delegation block (search for `FORK EXTENSION` in that
   file). When upstream updates `interface.py`, the merge conflict is
   confined to that block; resolution = keep both the upstream additions
   and our delegation block.

2. All other fork additions live in **new files** the upstream doesn't
   have. New files don't conflict on `git merge` unless upstream ships a
   file with the exact same path. Risk assessment per file is below.

3. The dashboard (`dashboard/`) is an entirely separate top-level package
   the upstream doesn't ship. It imports `tradingagents.*` but doesn't
   modify any upstream module. Upstream changes to `tradingagents/` don't
   affect the dashboard's contracts (we read state from saved JSON files,
   not from live tradingagents objects, except in `runner.py` which only
   calls `TradingAgentsGraph.propagate(...)` — a stable public API).

---

## Fork-only files (no merge conflict risk)

These files don't exist in upstream. They are safe across merges unless
upstream ships a same-named file (rare; check `git diff upstream/main
--name-only` after each fetch).

### `config/` — fork config directory
- `config/data_sources.yaml` — declarative data source registry config

### `dashboard/` — Bloomberg-style local HTTP dashboard
- `dashboard/app.py` — FastAPI app, all routes (run, run-all/stream SSE,
  portfolio, watchlist, reversal, print)
- `dashboard/runner.py` — TradingAgentsGraph wrapper with progress callbacks
- `dashboard/state_reader.py` — state file loader, portfolio, watchlist
- `dashboard/templates/index.html` — main dashboard view
- `dashboard/templates/print.html` — A4 print-optimized report view
- `dashboard/static/style.css` — Bloomberg dark theme + print CSS
- `dashboard/static/key_signals.js` — regex extraction (price targets,
  fundamentals, sentiment, news) from agent reports
- `dashboard/static/diff_highlights.js` — page-level change detection
  (UPDATED badges, pulse animation) across refreshes
- `dashboard/static/run_all_progress.js` — SSE-driven Run All progress panel
- `dashboard/scripts/daily_run.py` — cron entry, iterates watchlist

### `tradingagents/dataflows/` — fork additions inside upstream package
- `tradingagents/dataflows/registry.py` — `DataSourceRegistry` singleton
- `tradingagents/dataflows/base_adapter.py` — `BaseAdapter` abstract class
- `tradingagents/dataflows/ticker_router.py` — `.SH/.SZ/.BJ → A_SHARE`,
  `.HK → HK`, else → `US`
- `tradingagents/dataflows/normalizer.py` — column-name normalization +
  CSV/markdown output formatters
- `tradingagents/dataflows/adapters/__init__.py` — adapter auto-register
- `tradingagents/dataflows/adapters/yfinance_adapter.py` — wraps existing
  `y_finance.py` (upstream file, unmodified)
- `tradingagents/dataflows/adapters/akshare_adapter.py` — wraps existing
  `akshare_backend.py` (fork addition pre-dating this restructure)
- `tradingagents/dataflows/adapters/tushare_adapter.py` — tushare pro_api
  client for A-share + HK market data and fundamentals

---

## Modified upstream files (merge-conflict-prone)

### `tradingagents/dataflows/interface.py`
**What we changed:** added a registry delegation path at the top of
`route_to_vendor()`. If the YAML config has an enabled+registered source for
the `(category, market)` pair, dispatch via the registry first; on
`NoMarketDataError` / `VendorNotConfiguredError`, fall back to the existing
upstream `VENDOR_METHODS` chain unchanged.

**Where to find our additions:** search for `FORK EXTENSION` in this file.
All fork additions live between `# --- Phase 1: registry delegation ---`
comment blocks.

**Merge resolution when upstream updates this file:**
- Keep upstream's changes to `VENDOR_METHODS`, `get_category_for_method`,
  `get_vendor`, and the existing `route_to_vendor` body (after our
  delegation block).
- Keep our `REGISTRY_METHOD_MAP`, `_registry_has_enabled_source`,
  `_dispatch_via_registry`, `_coerce_method_kwargs` helpers and the
  delegation probe at the top of `route_to_vendor`.
- If upstream adds a new method to `VENDOR_METHODS`, also add a row to
  `REGISTRY_METHOD_MAP` if a registry adapter should serve it.

### `pyproject.toml`
**What we changed:** added `[project.optional-dependencies].fork` extra
with `tushare` and `pyyaml` (the upstream `dependencies` list is
preserved verbatim).

**Merge resolution:** when upstream adds new core deps, keep their
additions; keep our `[fork]` extra block separate at the bottom of the
`[project.optional-dependencies]` table.

### `README.md`
**What we changed:** inserted a `## Fork Extensions` section immediately
after the upstream title header, before the upstream `## News` section.
The fork section is fenced with `<!-- FORK SECTION START -->` and
`<!-- FORK SECTION END -->` HTML comments so the diff is bounded.

**Merge resolution:** keep our fork section block; integrate upstream
changes to the rest of the file normally.

### `CHANGELOG.md`
**What we changed:** added a `## [Fork]` section at the very top, above
upstream's `## [0.5.0]` entry. Fork entries are dated and labelled
`[Fork]` to distinguish from upstream version tags.

**Merge resolution:** keep our fork entries at the top; upstream additions
go below as new version sections.

### `requirements.txt`
**What we changed:** added fork-specific deps below the upstream `.` line
with a `# fork deps` comment header.

---

## Merge recipe — pulling a new upstream release

```bash
# 1. Fetch upstream
git fetch origin

# 2. Create a merge branch off our working branch
git checkout -b merge-upstream-<version>

# 3. Merge the upstream tag/branch
git merge origin/main --no-ff
# (or: git merge v0.6.0 --no-ff for a specific tag)

# 4. If conflicts arise, they will be in these files (in priority order):
#    - tradingagents/dataflows/interface.py    ← our FORK EXTENSION block
#    - README.md                              ← our FORK SECTION block
#    - CHANGELOG.md                            ← our [Fork] top section
#    - pyproject.toml                          ← our [fork] optional-deps
#    Resolve each by keeping both sides per the per-file notes above.

# 5. After resolving, verify the registry delegation still works:
cd /path/to/TradingAgents
/opt/anaconda3/envs/fundteam/bin/python -c "
from tradingagents.dataflows.interface import route_to_vendor, REGISTRY_METHOD_MAP
print('REGISTRY_METHOD_MAP keys:', sorted(REGISTRY_METHOD_MAP.keys()))
out = route_to_vendor('get_stock_data', 'NVDA', '2024-01-01', '2024-06-01')
print('NVDA rows:', len(out) if isinstance(out, str) else out)
"

# 6. Re-run the dashboard smoke test
nohup /opt/anaconda3/envs/fundteam/bin/python -m uvicorn dashboard.app:app \
    --host 0.0.0.0 --port 8080 > /tmp/dashboard.log 2>&1 &
curl -s --noproxy '*' -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/

# 7. If clean, push the merge to myfork
git push myfork merge-upstream-<version:main
```

### When upstream adds a new dataflows file with a name we used

If upstream ships e.g. `tradingagents/dataflows/registry.py` themselves
(unlikely but possible), the merge will report an add/add conflict on that
file. Resolution options:
- **Preferred:** rename our file to `tradingagents/dataflows/fork_registry.py`
  and update the import in `interface.py`. Keep our semantic; let upstream
  own the canonical `registry.py` name.
- **Alternative:** inspect upstream's `registry.py`; if it serves the same
  purpose, deprecate ours and adopt upstream's.

### When upstream refactors `dataflows/` entirely

If upstream moves e.g. `y_finance.py` to `dataflows/vendors/yfinance.py`:
1. Our `adapters/yfinance_adapter.py` imports break — update the import
   path inside the adapter only.
2. Our `interface.py` patch's imports from `y_finance` break — update
   those imports. The delegation block itself is vendor-agnostic.

The adapter layer is designed to absorb this: each adapter is a thin
delegate, so a vendor file rename = one import line change per adapter.

---

## Fork-specific conda env

The fork uses the `fundteam` conda env (py3.11). Upstream deps are
unchanged; the fork adds:

- `tushare` — A-share + HK market data and fundamentals
- `pyyaml` — for parsing `config/data_sources.yaml`
- `akshare` — A-share fallback (already present in the env)

Install fork extras:
```bash
/opt/anaconda3/envs/fundteam/bin/pip install "tradingagents[fork]"
```

---

## Fork changelog (short form — full detail in CHANGELOG.md)

- **2026-09-18 [Fork Phase 1]** Data source / execution layer separation.
  New `DataSourceRegistry` + `BaseAdapter` + per-market YAML config. yfinance
  + akshare adapters wrap existing vendor functions; registry dispatch path
  added to `interface.py` with graceful fallback to the legacy chain.
- **2026-09-18 [Fork Phase 4]** Run All real-time progress system. SSE
  endpoint `/api/run-all/stream` streams per-ticker events
  (`hello`/`all_start`/`ticker_start`/`log`/`ticker_done`/`all_done`).
  Frontend `run_all_progress.js` renders a live progress panel with per-ticker
  cards and an event log; auto-refresh on `all_done` activates
  `diff_highlights.js` UPDATED badges.
- **2026-09-18 [Fork Phase 2]** Tushare adapter (2000 points). A-share daily
  + daily_basic (PE/PB/ROE/market cap) + income/balance/cashflow statements.
  HK `hk_daily` wired (rate-limited at 1/hour at 2000 points; chain falls
  back to yfinance). `NO_PROXY=*` bypass for the dev proxy.
- **2026-09-18 [Fork earlier]** Dashboard (FastAPI + Bloomberg dark theme),
  watchlist, portfolio, reversal detection, PDF print view, key signals
  extraction, diff highlights, daily cron job, akshare A-share backend.
