"""Cold-start backfill from NSE's corporate-filings JSON APIs.

The announcements API (verified live 2026-08-05) is richer than RSS:
    seq_id     — NSE's own id
    symbol     — actual trading symbol (RSS only has the company name)
    sm_isin    — ISIN
    an_dt      — broadcast timestamp ("04-Aug-2026 23:55:40", IST)
    desc       — NSE's own category label ("General Updates", …)
    attchmntFile — the SAME archives URL the RSS <link> carries.

That last fact is the dedup bridge: both paths store the attachment URL
as dedup_key, so a filing seen live via RSS and again via backfill
collapses into one row regardless of which arrived first. API-only rows
without an attachment fall back to seq_id as the natural key.

Honesty note (§10 point-in-time): backfilled rows get observed_at = NOW,
because now is when this system saw them. The broadcast time lands in
event_at. A backtest over a backfilled window must treat visibility as
reconstructed-from-event_at, never pretend we observed it live — that is
exactly the pit_quality: reconstructed label from the de-risk plan.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from marketsense.agents.a1_ingestion.parse import parse_pub
from marketsense.bus import topics
from marketsense.bus.outbox import notify, publish
from marketsense.core.logging import get_logger
from marketsense.db.models import Document, FeedState, Filing
from marketsense.net.nse_client import NSE_WWW, NSEClient, NSEUnavailable
from marketsense.universe.securities_master import resolve

log = get_logger("a1.backfill")

ANNOUNCEMENTS_API = (
    NSE_WWW + "/api/corporate-announcements?index=equities"
    "&from_date={frm}&to_date={to}"
)
CHUNK_DAYS = 2  # ~600-1,200 announcements per call; keeps payloads sane


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def backfill_announcements(
    db_factory,
    client: NSEClient,
    *,
    days: int = 90,
    end: date | None = None,
    symbol: str | None = None,
) -> dict:
    """Walk backwards `days` from `end` in CHUNK_DAYS windows. Idempotent —
    re-running skips everything already stored."""
    end = end or date.today()
    start = end - timedelta(days=days)
    stats = {"windows": 0, "rows": 0, "new": 0, "skipped_windows": 0}

    cursor = end
    while cursor > start:
        frm = max(start, cursor - timedelta(days=CHUNK_DAYS - 1))
        url = ANNOUNCEMENTS_API.format(frm=_ddmmyyyy(frm), to=_ddmmyyyy(cursor))
        if symbol:
            url += f"&symbol={symbol}"
        try:
            rows = client.get_json(url)
        except NSEUnavailable as e:
            log.warning("backfill_window_skipped", frm=str(frm), to=str(cursor),
                        error=str(e))
            stats["skipped_windows"] += 1
            cursor = frm - timedelta(days=1)
            continue
        if not isinstance(rows, list):
            rows = []
        stats["windows"] += 1
        stats["rows"] += len(rows)

        with db_factory() as db:
            new_here = 0
            for row in rows:
                if _insert_api_row(db, row):
                    new_here += 1
            if new_here:
                state = db.get(FeedState, "announcements")
                if state:
                    state.entries_new += new_here
                notify(db)
            db.commit()
            stats["new"] += new_here

        log.info("backfill_window", frm=str(frm), to=str(cursor),
                 rows=len(rows), new=new_here)
        cursor = frm - timedelta(days=1)

    return stats


def _insert_api_row(db, row: dict) -> bool:
    attachment = (row.get("attchmntFile") or "").strip() or None
    seq_id = str(row.get("seq_id") or "").strip() or None
    dedup_key = attachment or (f"seq:{seq_id}" if seq_id else None)
    if dedup_key is None:
        return False  # nothing stable to key on; refuse silently-undupable rows

    exists = db.scalar(
        select(Filing.id).where(Filing.feed == "announcements",
                                Filing.dedup_key == dedup_key)
    )
    if exists:
        return False

    import hashlib

    symbol = (row.get("symbol") or "").strip().upper() or None
    isin = (row.get("sm_isin") or "").strip() or None
    subject = (row.get("desc") or "").strip() or None
    text = (row.get("attchmntText") or "").strip() or None
    event_at = parse_pub(row.get("an_dt"))
    content_hash = hashlib.sha256(
        ("api|announcements|" + dedup_key).encode()
    ).hexdigest()

    if db.scalar(select(Filing.id).where(Filing.content_hash == content_hash)):
        return False

    security = resolve(db, symbol) if symbol else None

    filing = Filing(
        feed="announcements",
        dedup_key=dedup_key,
        content_hash=content_hash,
        symbol=symbol,
        security_id=security.id if security else None,
        isin=isin or (security.isin if security else None),
        subject=subject,
        description=text,
        link=attachment,
        attachment_url=attachment,
        raw=row,
        source="api_backfill",
        event_at=event_at,
    )
    db.add(filing)
    db.flush()
    if attachment:
        db.add(Document(filing_id=filing.id, url=attachment))

    publish(
        db,
        topics.FILING_RECEIVED,
        {
            "filing_id": filing.id,
            "feed": "announcements",
            "symbol": symbol,
            "security_id": filing.security_id,
            "subject": (subject or "")[:300],
            "event_at": event_at.isoformat() if event_at else None,
            "has_attachment": bool(attachment),
            "backfill": True,
        },
    )
    return True
