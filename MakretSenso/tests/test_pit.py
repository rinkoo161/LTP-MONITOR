"""The poison test (§10 / de-risk R2): a row observed in the future must
be invisible to a past as_of, byte-for-byte. If this file fails, the
build fails — do not weaken it to make something else convenient."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from marketsense.db.models import Filing
from marketsense.db.pit import PITViolation, pit_filings

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _add_filing(db, *, symbol, event_at, observed_at, subject):
    db.add(Filing(feed="announcements", content_hash=f"h-{subject}"[:64].ljust(64, "0"),
                  symbol=symbol, subject=subject, source="rss", event_at=event_at))
    db.flush()
    # observed_at has a server_default; poison rows need explicit override
    db.execute(text("UPDATE filings SET observed_at = :o WHERE subject = :s"),
               {"o": observed_at, "s": subject})


def test_future_observed_row_is_invisible_to_past_as_of(db_factory):
    with db_factory() as db:
        _add_filing(db, symbol="TCS", subject="seen-early",
                    event_at=NOW - timedelta(days=1),
                    observed_at=NOW - timedelta(days=1))
        db.commit()

    with db_factory() as db:
        baseline = [(f.subject, f.content_hash) for f in
                    pit_filings(db, as_of=NOW, symbol="TCS")]

    # POISON: a filing dated in the PAST but observed in the FUTURE —
    # exactly the shape of a backfilled row that would leak into a backtest.
    with db_factory() as db:
        _add_filing(db, symbol="TCS", subject="seen-late",
                    event_at=NOW - timedelta(days=2),          # printed date: past
                    observed_at=NOW + timedelta(days=30))      # we saw it: future
        db.commit()

    with db_factory() as db:
        after = [(f.subject, f.content_hash) for f in
                 pit_filings(db, as_of=NOW, symbol="TCS")]

    assert after == baseline, (
        "PIT LEAK: a future-observed filing changed a past-as_of read"
    )

    # …and it IS visible once as_of passes its observation time.
    with db_factory() as db:
        later = [f.subject for f in
                 pit_filings(db, as_of=NOW + timedelta(days=31), symbol="TCS")]
    assert "seen-late" in later


def test_naive_as_of_rejected(db_factory):
    with db_factory() as db:
        with pytest.raises(PITViolation):
            pit_filings(db, as_of=datetime(2026, 8, 4, 12, 0), symbol="TCS")


def test_every_fact_table_has_observed_at():
    """Structural lint: any table storing facts must carry observed_at.
    Adding a new fact table without it should fail HERE, at review time."""
    from marketsense.db.models import Base

    exempt = {"alembic_version", "feed_state", "consumer_offsets", "outbox",
              "dead_letters", "agent_runs"}  # ops/bus state, not facts
    for table in Base.metadata.sorted_tables:
        if table.name in exempt:
            continue
        assert "observed_at" in table.columns, (
            f"fact table '{table.name}' lacks observed_at — PIT integrity broken"
        )
