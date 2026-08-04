"""A1 — the feed poller.

One pass = for each feed due by its schedule: conditional GET → parse →
insert new filings (+ pending Document rows for attachments) → publish
filing.received — all in ONE transaction per feed, so an event exists iff
its filing committed. Dedup is enforced by DB constraints, not by
application memory: a restart cannot forget what it has seen.

Politeness properties:
  * a 304 costs one budget token and no parsing;
  * budget refusal (BudgetExceeded via NSEUnavailable) marks the cycle
    'skipped' and moves on — the next schedule tick retries;
  * feeds are polled serially inside one thread: 23 feeds can never
    stampede NSE in parallel by construction (§2.5's "one shared pool").
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketsense.agents.a1_ingestion.feeds import FEEDS, POLL_INTERVAL, FeedSpec
from marketsense.agents.a1_ingestion.parse import parse_feed
from marketsense.bus import topics
from marketsense.bus.outbox import notify, publish
from marketsense.core.clock import calendar
from marketsense.core.logging import get_logger
from marketsense.db.models import Document, FeedState, Filing
from marketsense.net.nse_client import NSEClient, NSEUnavailable
from marketsense.universe.securities_master import resolve

log = get_logger("a1")

# Attachments we bother storing. .xml covers XBRL results (Phase 3 input).
_ATTACHMENT_EXTS = (".pdf", ".xml", ".zip", ".xls", ".xlsx", ".csv")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class FeedPoller:
    def __init__(self, client: NSEClient, session_factory) -> None:
        self.client = client
        self.session_factory = session_factory
        self._next_due: dict[str, float] = {f.name: 0.0 for f in FEEDS}

    # ------------------------------------------------------------------
    def due_feeds(self, now_mono: float | None = None) -> list[FeedSpec]:
        now_mono = time.monotonic() if now_mono is None else now_mono
        return [f for f in FEEDS if self._next_due[f.name] <= now_mono]

    def _reschedule(self, spec: FeedSpec) -> None:
        market, off = POLL_INTERVAL[spec.priority]
        interval = market if calendar.is_market_open() else off
        self._next_due[spec.name] = time.monotonic() + interval

    # ------------------------------------------------------------------
    def poll_feed(self, spec: FeedSpec) -> dict:
        """Poll one feed. Returns stats; never raises for routine trouble."""
        stats = {"feed": spec.name, "status": "ok", "seen": 0, "new": 0}
        try:
            res = self.client.get(spec.url, conditional=True)
        except NSEUnavailable as e:
            stats["status"] = f"skipped: {e}"
            self._touch_state(spec, status="skipped", error=True)
            self._reschedule(spec)
            return stats

        if res.not_modified:
            stats["status"] = "304"
            self._touch_state(spec, status="304")
            self._reschedule(spec)
            return stats

        entries = parse_feed(spec.name, res.content)
        stats["seen"] = len(entries)

        with self.session_factory() as db:
            for entry in entries:
                if self._insert_entry(db, entry):
                    stats["new"] += 1
            self._update_state(db, spec, res, stats)
            if stats["new"]:
                notify(db)
            db.commit()

        self._reschedule(spec)
        if stats["new"]:
            log.info("feed_polled", **stats)
        return stats

    # ------------------------------------------------------------------
    def _insert_entry(self, db: Session, entry: dict) -> bool:
        """Insert one filing if unseen. Returns True if new."""
        # Dedup layer 1: content hash (global).
        exists = db.scalar(
            select(Filing.id).where(Filing.content_hash == entry["content_hash"])
        )
        if exists:
            return False
        # Dedup layer 2: feed-scoped natural key (attachment URL).
        if entry["dedup_key"]:
            exists = db.scalar(
                select(Filing.id).where(
                    Filing.feed == entry["feed"], Filing.dedup_key == entry["dedup_key"]
                )
            )
            if exists:
                return False

        # Symbol attach: filename token first, company-name resolve second.
        symbol = entry["symbol_hint"]
        security = None
        if symbol:
            security = resolve(db, symbol)
        if security is None and entry["company_title"]:
            security = _resolve_by_name(db, entry["company_title"])
        if security is not None and symbol is None:
            symbol = security.symbol

        filing = Filing(
            feed=entry["feed"],
            dedup_key=entry["dedup_key"],
            content_hash=entry["content_hash"],
            symbol=symbol,
            security_id=security.id if security else None,
            isin=security.isin if security else None,
            subject=entry["subject"],
            description=entry["description"],
            link=entry["link"],
            attachment_url=_attachment_url(entry["link"]),
            raw={"company_title": entry["company_title"], "fields": entry["fields"]},
            source="rss",
            event_at=entry["event_at"],
        )
        db.add(filing)
        db.flush()

        if filing.attachment_url:
            db.add(Document(filing_id=filing.id, url=filing.attachment_url))

        publish(
            db,
            topics.FILING_RECEIVED,
            {
                "filing_id": filing.id,
                "feed": filing.feed,
                "symbol": filing.symbol,
                "security_id": filing.security_id,
                "subject": (filing.subject or "")[:300],
                "event_at": filing.event_at.isoformat() if filing.event_at else None,
                "has_attachment": bool(filing.attachment_url),
            },
        )
        return True

    # ------------------------------------------------------------------
    def _update_state(self, db: Session, spec: FeedSpec, res, stats: dict) -> None:
        state = db.get(FeedState, spec.name) or FeedState(
            feed=spec.name, priority=spec.priority,
            entries_seen=0, entries_new=0, consecutive_errors=0,
        )
        state.etag = res.etag
        state.last_modified = res.last_modified
        state.last_polled_at = _now_utc()
        state.last_status = "ok"
        state.entries_seen += stats["seen"]
        state.entries_new += stats["new"]
        state.consecutive_errors = 0
        if stats["new"]:
            state.last_changed_at = _now_utc()
        db.merge(state)

    def _touch_state(self, spec: FeedSpec, status: str, error: bool = False) -> None:
        with self.session_factory() as db:
            state = db.get(FeedState, spec.name) or FeedState(
                feed=spec.name, priority=spec.priority,
                entries_seen=0, entries_new=0, consecutive_errors=0,
            )
            state.last_polled_at = _now_utc()
            state.last_status = status
            if error:
                state.consecutive_errors += 1
            db.merge(state)
            db.commit()

    # ------------------------------------------------------------------
    def run_pass(self) -> dict:
        """Poll everything due. The supervisor calls this in a loop."""
        results = [self.poll_feed(spec) for spec in self.due_feeds()]
        return {
            "polled": len(results),
            "new": sum(r["new"] for r in results),
            "skipped": sum(1 for r in results if str(r["status"]).startswith("skipped")),
        }


def _attachment_url(link: str | None) -> str | None:
    if link and link.lower().endswith(_ATTACHMENT_EXTS):
        return link
    return None


def _resolve_by_name(db: Session, name: str):
    """Company-name → security. RSS titles use NSE's own registered names,
    so an exact (case-insensitive) match on current or former name covers
    nearly everything; anything unresolved stays symbol-less rather than
    fuzzy-matched to the wrong company."""
    from sqlalchemy import func

    from marketsense.db.models import Security, SecurityAlias

    n = " ".join(name.split())
    if not n:
        return None
    # corporate_actions titles append detail after the name
    # ("SWAN CORP LIMITED - Ex-Date: 28-Aug-2026") — try full, then prefix.
    candidates = [n]
    if " - " in n:
        candidates.append(n.split(" - ", 1)[0].strip())
    for cand in candidates:
        sec = db.scalar(
            select(Security).where(func.upper(Security.company_name) == cand.upper())
        )
        if sec:
            return sec
        alias = db.scalar(
            select(SecurityAlias).where(
                func.upper(SecurityAlias.alias) == cand.upper(),
                SecurityAlias.alias_type == "old_name",
            )
        )
        if alias:
            return db.get(Security, alias.security_id)
    return None
