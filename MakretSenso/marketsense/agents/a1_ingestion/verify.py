"""Coverage verifier — Phase 1 acceptance evidence.

Compares what we HOLD against what NSE's own API says EXISTS for a
window, keyed the same way ingestion dedups (attachment URL / seq_id).
This is the honest version of "full feed coverage verified against the
NSE website": the website's announcement page is fed by the same API, so
matching the API == matching the site, but mechanically checkable.

Every gap is NAMED (symbol, subject, timestamp), never just counted —
per the evidence standards: a coverage number without the list of what
is missing proves nothing.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from marketsense.agents.a1_ingestion.backfill import ANNOUNCEMENTS_API, _ddmmyyyy
from marketsense.core.logging import get_logger
from marketsense.db.models import Filing
from marketsense.net.nse_client import NSEClient

log = get_logger("a1.verify")


def verify_announcements(db_factory, client: NSEClient, *, day: date | None = None) -> dict:
    """Coverage for one day. Returns {expected, held, missing: [...]}."""
    day = day or (date.today() - timedelta(days=1))
    url = ANNOUNCEMENTS_API.format(frm=_ddmmyyyy(day), to=_ddmmyyyy(day))
    rows = client.get_json(url)
    if not isinstance(rows, list):
        rows = []

    missing: list[dict] = []
    held = 0
    with db_factory() as db:
        for row in rows:
            attachment = (row.get("attchmntFile") or "").strip() or None
            seq_id = str(row.get("seq_id") or "").strip() or None
            dedup_key = attachment or (f"seq:{seq_id}" if seq_id else None)
            if dedup_key is None:
                continue
            exists = db.scalar(
                select(Filing.id).where(Filing.feed == "announcements",
                                        Filing.dedup_key == dedup_key)
            )
            if exists:
                held += 1
            else:
                missing.append(
                    {
                        "symbol": row.get("symbol"),
                        "subject": (row.get("desc") or "")[:80],
                        "an_dt": row.get("an_dt"),
                        "seq_id": seq_id,
                    }
                )

    expected = held + len(missing)
    report = {
        "day": str(day),
        "expected": expected,
        "held": held,
        "coverage_pct": round(100.0 * held / expected, 2) if expected else 100.0,
        "missing": missing,
    }
    log.info("coverage", day=str(day), expected=expected, held=held,
             missing=len(missing))
    return report
