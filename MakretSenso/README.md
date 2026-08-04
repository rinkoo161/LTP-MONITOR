# MarketSense

NSE market intelligence & equity advisory platform. **Decision support only** —
no order placement, no broker write APIs, every claim traceable to a stored record.

Phase 1 (current): hardened NSE ingestion — all 23 corporate-disclosure RSS
feeds + JSON-API backfill, securities master with rename history, Postgres-backed
event bus, point-in-time data layer, CLI, health/metrics API.

## Requirements

- Python 3.11+ (built on 3.13)
- PostgreSQL 16 (`brew install postgresql@16 && brew services start postgresql@16`)
- `createdb marketsense && createdb marketsense_test`

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
cp .env.example .env          # optional; defaults work for local brew postgres
```

## Run

```bash
.venv/bin/ms run                     # ingest supervisor (foreground; nohup for soak)
.venv/bin/ms serve                   # health/metrics API on :8100 (ltp-monitor owns :8000)

.venv/bin/ms filings RELIANCE --days 7
.venv/bin/ms feeds                   # per-feed poller status
.venv/bin/ms budget                  # NSE request budget + breaker state
.venv/bin/ms backfill --days 90      # announcements cold-start backfill
.venv/bin/ms verify --day 2026-08-03 # coverage vs NSE's own API
.venv/bin/ms master-sync             # securities master refresh
```

Tests: `.venv/bin/pytest -q` (needs `marketsense_test` DB; see `tests/conftest.py`).

## Architecture (Phase 1 slice)

```
ms-ingest (one process, one thread — NSE politeness by construction)
  A1 poller: 23 RSS feeds, conditional GET, P0 60s/market P2 hourly
  hourly API catch-up (closes RSS rolling-window gaps)
  document fetcher: content-addressed store at ~/.marketsense/documents
       │  publishes filing.received
       ▼
  Postgres = store AND event bus (outbox + LISTEN/NOTIFY, replay by offset)
       ▲
ms-api (separate read-only process): /health /api/feeds /api/filings/{sym}
                                     /api/stats /metrics (Prometheus)
```

Key invariants — see `marketsense/db/models.py` and `tests/test_pit.py`:

- Every fact row carries `event_at` (world time) **and** `observed_at` (when we
  saw it). Analytical reads require `as_of` and filter on `observed_at` — the
  poison test enforces that a backfilled row cannot leak into a past backtest.
- Dedup is DB-constraint-backed (content hash global + feed-scoped natural key);
  RSS and API backfill share the attachment URL as the key, so the two sources
  collapse into one row.
- One `NSEClient` per process: token-bucket budget (30/min), circuit breaker on
  consecutive 401/403, curl_cffi Chrome TLS impersonation, per-request audit rows.

## Roadmap

ROADMAP.md is the cross-session source of truth. Phases: 1 ingestion (this) →
2 document intelligence → 3 fundamentals/technicals/flow → 4 fusion + backtest →
5 alerts + LTP Monitor integration + dashboard.
