"""DB-backed tests: dedup invariants, the RSS↔backfill bridge, consumer
semantics (dead-letter, offset advance, replay)."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from marketsense.agents.a1_ingestion.parse import parse_feed
from marketsense.agents.a1_ingestion.poller import FeedPoller
from marketsense.bus.outbox import MAX_ATTEMPTS, Consumer, publish
from marketsense.db.models import DeadLetter, Filing, Outbox
from tests.test_parse import RSS_FIXTURE


def _insert_all(db_factory, entries):
    poller = FeedPoller(client=None, session_factory=db_factory)  # no HTTP used
    new = 0
    with db_factory() as db:
        for e in entries:
            if poller._insert_entry(db, e):
                new += 1
        db.commit()
    return new


def test_reinserting_same_entries_is_noop(db_factory):
    entries = parse_feed("announcements", RSS_FIXTURE)
    assert _insert_all(db_factory, entries) == 2
    assert _insert_all(db_factory, entries) == 0  # idempotent
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Filing)) == 2
        # one filing.received per unique filing, not per poll
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2


def test_backfill_dedups_against_rss(db_factory):
    """The bridge: API row whose attchmntFile equals an RSS link must not
    create a second filing."""
    from marketsense.agents.a1_ingestion.backfill import _insert_api_row

    entries = parse_feed("announcements", RSS_FIXTURE)
    _insert_all(db_factory, entries)
    api_row = {
        "attchmntFile": entries[0]["link"],
        "seq_id": "999",
        "symbol": "BHARTIARTL",
        "desc": "General Updates",
        "an_dt": "05-Aug-2026 00:03:49",
    }
    with db_factory() as db:
        assert _insert_api_row(db, api_row) is False
        db.commit()
        assert db.scalar(select(func.count()).select_from(Filing)) == 2


def test_backfill_inserts_new_api_rows(db_factory):
    from marketsense.agents.a1_ingestion.backfill import _insert_api_row

    row = {"attchmntFile": "https://nsearchives.nseindia.com/corporate/X_1.pdf",
           "seq_id": "1", "symbol": "TCS", "desc": "Results", "an_dt": "01-Aug-2026 10:00:00"}
    with db_factory() as db:
        assert _insert_api_row(db, row) is True
        assert _insert_api_row(db, row) is False  # same call twice
        db.commit()


def test_consumer_drain_ack_and_replay(db_factory):
    seen: list[int] = []
    with db_factory() as db:
        for i in range(5):
            publish(db, "filing.received", {"n": i})
        db.commit()

    c = Consumer("t1", "filing.received", lambda e: seen.append(e.payload["n"]),
                 db_factory)
    assert c.drain() == 5
    assert seen == [0, 1, 2, 3, 4]
    assert c.drain() == 0  # nothing left

    c.reset(0)
    assert c.drain() == 5  # replay redelivers everything
    assert seen == [0, 1, 2, 3, 4] * 2


def test_poison_event_dead_letters_and_stream_continues(db_factory):
    """One bad event must not wedge the stream (§3)."""
    seen: list[int] = []

    def handler(evt):
        if evt.payload["n"] == 1:
            raise ValueError("poison")
        seen.append(evt.payload["n"])

    with db_factory() as db:
        for i in range(3):
            publish(db, "filing.received", {"n": i})
        db.commit()

    c = Consumer("t2", "filing.received", handler, db_factory)
    assert c.drain() == 3
    assert seen == [0, 2]  # 1 skipped
    with db_factory() as db:
        dl = list(db.scalars(select(DeadLetter)))
        assert len(dl) == 1
        assert dl[0].attempts == MAX_ATTEMPTS
        assert dl[0].payload["n"] == 1


def test_two_consumers_independent_offsets(db_factory):
    a_seen, b_seen = [], []
    with db_factory() as db:
        for i in range(3):
            publish(db, "filing.received", {"n": i})
        db.commit()
    a = Consumer("a", "filing.received", lambda e: a_seen.append(e.payload["n"]), db_factory)
    b = Consumer("b", "filing.received", lambda e: b_seen.append(e.payload["n"]), db_factory)
    a.drain()
    b.drain()
    assert a_seen == b_seen == [0, 1, 2]


def test_db_constraint_backstop_on_duplicate_hash(db_factory):
    """Even if application dedup is bypassed, the DB refuses duplicates."""
    from sqlalchemy.exc import IntegrityError

    with db_factory() as db:
        db.add(Filing(feed="announcements", content_hash="x" * 64, source="rss"))
        db.commit()
    with db_factory() as db:
        db.add(Filing(feed="announcements", content_hash="x" * 64, source="rss"))
        with pytest.raises(IntegrityError):
            db.commit()
