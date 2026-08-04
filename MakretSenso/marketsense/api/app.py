"""Phase 1 API — agent health, feed status, filings query, Prometheus metrics.

Read-only over the DB. The ingest supervisor is a separate process; this
app carries no NSEClient and can never spend NSE budget. That is also
what makes it safe for ltp-monitor to poll (§6 read-only integration):
a dashboard refresh loop cannot translate into exchange requests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from marketsense import __version__
from marketsense.db.engine import session
from marketsense.db.models import AgentRun, FeedState, Filing, Outbox
from marketsense.db.pit import pit_filings

app = FastAPI(title="MarketSense", version=__version__)

STALE_AFTER = {"P0": 300, "P1": 1800, "P2": 7200}  # sec since last poll = unhealthy


@app.get("/health")
def health():
    """Overall + per-feed health. 'degraded' names its causes."""
    now = datetime.now(timezone.utc)
    problems: list[str] = []
    with session() as db:
        states = list(db.scalars(select(FeedState)))
        last_run = db.scalar(
            select(func.max(AgentRun.finished_at)).where(AgentRun.agent == "a1_poller")
        )
    if not states:
        problems.append("no feed_state rows — supervisor has never run")
    for s in states:
        if s.last_polled_at is None:
            problems.append(f"{s.feed}: never polled")
            continue
        age = (now - s.last_polled_at).total_seconds()
        if age > STALE_AFTER.get(s.priority, 3600):
            problems.append(f"{s.feed}: last poll {int(age)}s ago")
        if s.consecutive_errors >= 3:
            problems.append(f"{s.feed}: {s.consecutive_errors} consecutive errors")
    return {
        "status": "degraded" if problems else "ok",
        "version": __version__,
        "supervisor_last_pass": last_run.isoformat() if last_run else None,
        "problems": problems,
    }


@app.get("/api/feeds")
def feeds():
    with session() as db:
        return [
            {
                "feed": s.feed,
                "priority": s.priority,
                "last_polled_at": s.last_polled_at,
                "last_changed_at": s.last_changed_at,
                "last_status": s.last_status,
                "entries_seen": s.entries_seen,
                "entries_new": s.entries_new,
                "consecutive_errors": s.consecutive_errors,
            }
            for s in db.scalars(select(FeedState).order_by(FeedState.feed))
        ]


@app.get("/api/filings/{symbol}")
def filings(symbol: str, days: int = Query(7, le=365), feed: str | None = None):
    now = datetime.now(timezone.utc)
    with session() as db:
        rows = pit_filings(db, as_of=now, symbol=symbol, feed=feed,
                           since=now - timedelta(days=days))
        if not rows:
            raise HTTPException(404, f"no filings for {symbol.upper()} in {days}d")
        return [
            {
                "id": f.id,
                "feed": f.feed,
                "symbol": f.symbol,
                "subject": f.subject,
                "event_at": f.event_at,
                "observed_at": f.observed_at,
                "link": f.link,
                "source": f.source,
            }
            for f in rows
        ]


@app.get("/api/stats")
def stats():
    with session() as db:
        return {
            "filings": db.scalar(select(func.count()).select_from(Filing)),
            "filings_by_source": dict(db.execute(
                text("select source, count(*) from filings group by 1")).all()),
            "resolved_pct": db.scalar(text(
                "select round(100.0 * count(security_id) / greatest(count(*),1), 1) "
                "from filings")),
            "events": db.scalar(select(func.count()).select_from(Outbox)),
            "documents": dict(db.execute(
                text("select fetch_status, count(*) from documents group by 1")).all()),
        }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Prometheus exposition, computed from the DB at scrape time."""
    now = datetime.now(timezone.utc)
    lines: list[str] = []

    def emit(name: str, value, labels: str = "") -> None:
        lines.append(f"marketsense_{name}{{{labels}}} {value}"
                     if labels else f"marketsense_{name} {value}")

    with session() as db:
        emit("filings_total", db.scalar(select(func.count()).select_from(Filing)))
        emit("outbox_events_total", db.scalar(select(func.count()).select_from(Outbox)))
        for s in db.scalars(select(FeedState)):
            lbl = f'feed="{s.feed}",priority="{s.priority}"'
            if s.last_polled_at:
                emit("feed_poll_age_seconds",
                     int((now - s.last_polled_at).total_seconds()), lbl)
            emit("feed_entries_new_total", s.entries_new, lbl)
            emit("feed_consecutive_errors", s.consecutive_errors, lbl)
        for status, n in db.execute(text(
            "select coalesce(status::text,'transport_error'), count(*) from http_audit "
            "where observed_at > now() - interval '1 hour' group by 1"
        )).all():
            emit("http_requests_last_hour", n, f'status="{status}"')
    return "\n".join(lines) + "\n"
