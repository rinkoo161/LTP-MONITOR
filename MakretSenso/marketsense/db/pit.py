"""Point-in-time read guard.

§10: "Point-in-time integrity … enforce this structurally in the data
access layer, not by convention."

The mechanism: analytical reads go through `pit_filings()` (and, in later
phases, pit_* helpers for scores/financials), which REQUIRE an `as_of`
and filter on `observed_at <= as_of`. observed_at is when THIS system
first saw the fact — so a backtest literally cannot see a filing before
the moment we ingested it, whatever its printed date says.

There is no "give me everything" analytical entry point. Live agents pass
as_of=now (a no-op filter); backtests pass the simulated clock. The
poison test in tests/test_pit.py inserts a future-observed row and
asserts a past-as_of read cannot change — that test failing is a build
failure, per the de-risk plan.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketsense.db.models import Filing


class PITViolation(Exception):
    pass


def _require_aware(as_of: datetime) -> datetime:
    if as_of.tzinfo is None:
        raise PITViolation("as_of must be timezone-aware; naive datetimes are ambiguous")
    return as_of


def pit_filings(
    db: Session,
    *,
    as_of: datetime,
    symbol: str | None = None,
    feed: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[Filing]:
    """Filings visible at `as_of` — the ONLY analytical filing query."""
    as_of = _require_aware(as_of)
    q = select(Filing).where(Filing.observed_at <= as_of)
    if symbol:
        q = q.where(Filing.symbol == symbol.upper())
    if feed:
        q = q.where(Filing.feed == feed)
    if since:
        q = q.where(Filing.event_at >= _require_aware(since))
    q = q.order_by(Filing.event_at.desc().nulls_last()).limit(limit)
    return list(db.scalars(q))
