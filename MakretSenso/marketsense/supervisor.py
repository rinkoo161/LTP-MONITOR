"""ms-ingest — the Phase 1 process: A1 poller + document fetcher.

DELIBERATELY single-threaded. All NSE requests (feed polls, document
downloads, list syncs) happen sequentially in one loop, which makes
"never let N agents hammer NSE in parallel" a structural property rather
than a discipline. At 23 feeds and a 30/min budget the loop is idle most
of the time; concurrency would buy nothing but risk.

Each component failure is recorded in agent_runs and does not stop the
loop — a broken feed parser must not stop document draining (§3: agents
survive one another's failure).
"""
from __future__ import annotations

import signal
import time
from datetime import datetime, timezone

from marketsense.agents.a1_ingestion.documents import DocumentFetcher
from marketsense.agents.a1_ingestion.poller import FeedPoller
from marketsense.core.logging import get_logger, setup_logging
from marketsense.db.engine import session
from marketsense.db.models import AgentRun
from marketsense.runtime import nse_client
from marketsense.universe.securities_master import sync_change_histories, sync_equity_lists

log = get_logger("supervisor")

LOOP_SLEEP = 5.0
DOC_DRAIN_EVERY = 30.0
MASTER_SYNC_EVERY = 24 * 3600.0
# RSS rolling windows hold ~20 items; a burst between polls can scroll a
# filing out of the window before we see it. The hourly API catch-up over
# the last 2 days closes that gap structurally — it is why "zero missed
# filings" is achievable at a 60s poll cadence.
CATCHUP_EVERY = 3600.0


def _record_run(agent: str, fn) -> None:
    """Run one component, bookkeeping its outcome. Never raises."""
    started = datetime.now(timezone.utc)
    try:
        stats = fn() or {}
        status, error = "ok", None
    except Exception as e:  # noqa: BLE001 — the loop must survive anything
        stats, status, error = {}, "error", f"{type(e).__name__}: {e}"
        log.error("component_error", agent=agent, error=error)
    with session() as db:
        db.add(AgentRun(agent=agent, started_at=started,
                        finished_at=datetime.now(timezone.utc),
                        status=status, stats=stats, error=error))
        db.commit()


class IngestSupervisor:
    def __init__(self) -> None:
        client = nse_client()
        self.poller = FeedPoller(client, session)
        self.docs = DocumentFetcher(client, session)
        self._client = client
        self._stop = False
        self._last_doc_drain = 0.0
        self._last_master_sync = 0.0
        self._last_catchup = 0.0

    def stop(self, *_args) -> None:
        log.info("stop_requested")
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log.info("ingest_started")

        while not self._stop:
            now = time.monotonic()

            # Daily securities-master refresh (first pass runs it immediately
            # if the table is empty; cheap no-op otherwise).
            if now - self._last_master_sync > MASTER_SYNC_EVERY:
                self._last_master_sync = now
                _record_run("securities_master", self._sync_master)

            _record_run("a1_poller", self.poller.run_pass)

            if now - self._last_doc_drain > DOC_DRAIN_EVERY:
                self._last_doc_drain = now
                _record_run("a1_docs", self.docs.drain)

            if now - self._last_catchup > CATCHUP_EVERY:
                self._last_catchup = now
                _record_run("a1_catchup", self._catchup)

            time.sleep(LOOP_SLEEP)

        log.info("ingest_stopped")

    def _sync_master(self) -> dict:
        with session() as db:
            stats = sync_equity_lists(db, self._client)
            stats.update(sync_change_histories(db, self._client))
            return stats

    def _catchup(self) -> dict:
        from marketsense.agents.a1_ingestion.backfill import backfill_announcements

        return backfill_announcements(session, self._client, days=2)


def main() -> None:
    setup_logging("INFO")
    IngestSupervisor().run()


if __name__ == "__main__":
    main()
