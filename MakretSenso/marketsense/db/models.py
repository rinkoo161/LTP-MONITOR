"""Phase 1 schema.

The one structural rule, enforced here and checked by tests/test_pit.py:
every fact table carries BOTH

    event_at    — when the fact happened in the world (exchange timestamp,
                  broadcast time, filing date). May be NULL only when the
                  source genuinely gives no timestamp.
    observed_at — when THIS SYSTEM first saw it. Never NULL, set by us.

Backtests and any "as of" query filter on observed_at, never event_at —
that is what makes look-ahead leakage structurally impossible rather than
a convention someone forgets. See db/pit.py.

Scores/signals tables arrive in later phases but will follow the same
rule plus model_version (append-only).
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _observed_at() -> Mapped[datetime]:
    """The system-observation timestamp. server_default so even raw SQL
    inserts cannot produce a row without one."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# ============================================================ securities

class Security(Base):
    """One row per listed instrument (main board + SME), keyed by ISIN
    where available. Renames land in SecurityAlias, not new rows."""

    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    company_name: Mapped[str] = mapped_column(Text)
    series: Mapped[str | None] = mapped_column(String(8))       # EQ, BE, SM, ST…
    is_sme: Mapped[bool] = mapped_column(Boolean, default=False)
    listing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    face_value: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|suspended|delisted
    extra: Mapped[dict | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = _observed_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("symbol", "series", name="uq_symbol_series"),)


class SecurityAlias(Base):
    """Old symbols / old names so historical filings resolve. A rename is
    an alias row + an update to Security.symbol, never a second Security."""

    __tablename__ = "security_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    alias: Mapped[str] = mapped_column(String(64), index=True)
    alias_type: Mapped[str] = mapped_column(String(16))  # old_symbol | old_name | variant
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # rename date
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (UniqueConstraint("alias", "alias_type", "security_id",
                                       name="uq_alias"),)


# =============================================================== filings

class Filing(Base):
    """One corporate disclosure from any feed. Dedup is two-layered:
    dedup_key (feed-scoped natural id when the feed provides one) and
    content_hash (always computable). Either matching = duplicate."""

    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed: Mapped[str] = mapped_column(String(48), index=True)      # e.g. "announcements"
    dedup_key: Mapped[str | None] = mapped_column(String(256))     # NSE's own id when present
    content_hash: Mapped[str] = mapped_column(String(64))          # sha256 of canonical content
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)  # as the feed gave it
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    isin: Mapped[str | None] = mapped_column(String(12))
    subject: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)                # full source payload
    source: Mapped[str] = mapped_column(String(16), default="rss")  # rss | api_backfill
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        # content_hash is globally unique; dedup_key unique within a feed
        UniqueConstraint("content_hash", name="uq_filing_content_hash"),
        Index("uq_filing_dedup_key", "feed", "dedup_key", unique=True,
              postgresql_where=text("dedup_key IS NOT NULL")),
        Index("ix_filings_feed_event_at", "feed", "event_at"),
    )


class Document(Base):
    """A downloaded attachment (PDF, XBRL, …), content-addressed."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    local_path: Mapped[str | None] = mapped_column(Text)   # under settings().pdf_dir
    bytes: Mapped[int | None]
    content_type: Mapped[str | None] = mapped_column(String(128))
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | fetched | failed | skipped
    fetch_error: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = _observed_at()


class FeedState(Base):
    """Poller bookkeeping per feed — schedule, validators, last outcome."""

    __tablename__ = "feed_state"

    feed: Mapped[str] = mapped_column(String(48), primary_key=True)
    priority: Mapped[str] = mapped_column(String(4))               # P0 | P1 | P2
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))    # ok|304|skipped|error:…
    entries_seen: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_new: Mapped[int] = mapped_column(BigInteger, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)


# ============================================================ event bus

class Outbox(Base):
    """The event bus. id is the replay offset. Events are appended in the
    same transaction as the facts they describe — that is the point."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # offset
    topic: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ConsumerOffset(Base):
    """At-least-once: a consumer's high-water mark, advanced after handling."""

    __tablename__ = "consumer_offsets"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_acked_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeadLetter(Base):
    """Events a consumer failed on MAX_ATTEMPTS times. Kept, never dropped."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    consumer: Mapped[str] = mapped_column(String(64), index=True)
    outbox_id: Mapped[int] = mapped_column(BigInteger)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ============================================================ ops tables

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent: Mapped[str] = mapped_column(String(48), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    # running | ok | error | skipped
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class HttpAudit(Base):
    """Every NSE request — §2.5's full audit log."""

    __tablename__ = "http_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    budget_tokens: Mapped[float | None]
    breaker_open: Mapped[bool | None]
    observed_at: Mapped[datetime] = _observed_at()


class Holiday(Base):
    """NSE trading holidays, refreshed from the holiday-master API so the
    static seed in core/clock.py is a bootstrap, not the truth."""

    __tablename__ = "holidays"

    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    segment: Mapped[str] = mapped_column(String(8), primary_key=True, default="CM")
    observed_at: Mapped[datetime] = _observed_at()
