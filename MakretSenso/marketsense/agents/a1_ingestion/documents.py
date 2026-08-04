"""Attachment downloader — drains Document rows with fetch_status='pending'.

Content-addressed storage: files land at
    {data_dir}/documents/{sha256[:2]}/{sha256}.{ext}
so re-filed identical PDFs cost one copy, and a Document row's sha256 is
the join key A2 will cache classifications by (§A2 "cache by document
hash").

Budget-aware: downloads share the same NSEClient budget as the pollers,
and each drain cycle is capped, so a burst of filings degrades to a
queue, never to a hammering of NSE. Pending docs survive restarts —
the queue is the DB, not memory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from marketsense.core.config import settings
from marketsense.core.logging import get_logger
from marketsense.db.models import Document
from marketsense.net.nse_client import NSEClient, NSEUnavailable

log = get_logger("a1.docs")

MAX_PER_CYCLE = 10
MAX_BYTES = 50 * 1024 * 1024  # refuse >50MB; annual reports get big but not this big


def _ext(url: str) -> str:
    ext = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    return ext if ext and len(ext) <= 5 else "bin"


class DocumentFetcher:
    def __init__(self, client: NSEClient, session_factory) -> None:
        self.client = client
        self.session_factory = session_factory

    def drain(self, limit: int = MAX_PER_CYCLE) -> dict:
        stats = {"fetched": 0, "failed": 0, "skipped_budget": 0}
        with self.session_factory() as db:
            pending = list(
                db.scalars(
                    select(Document)
                    .where(Document.fetch_status == "pending")
                    .order_by(Document.id)
                    .limit(limit)
                )
            )
            ids = [d.id for d in pending]
        if not ids:
            return stats

        for doc_id in ids:
            with self.session_factory() as db:
                doc = db.get(Document, doc_id)
                if doc is None or doc.fetch_status != "pending":
                    continue
                try:
                    res = self.client.get(doc.url, timeout=60.0)
                except NSEUnavailable as e:
                    # Budget/breaker — leave pending, stop the cycle: the
                    # rest would refuse too.
                    log.info("doc_fetch_deferred", doc_id=doc_id, reason=str(e))
                    stats["skipped_budget"] += 1
                    db.commit()
                    break
                except Exception as e:
                    doc.fetch_status = "failed"
                    doc.fetch_error = str(e)[:500]
                    stats["failed"] += 1
                    db.commit()
                    continue

                if len(res.content) > MAX_BYTES:
                    doc.fetch_status = "failed"
                    doc.fetch_error = f"too large: {len(res.content)} bytes"
                    stats["failed"] += 1
                    db.commit()
                    continue

                sha = hashlib.sha256(res.content).hexdigest()
                out_dir = settings().pdf_dir / sha[:2]
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"{sha}.{_ext(doc.url)}"
                if not path.exists():
                    path.write_bytes(res.content)

                doc.sha256 = sha
                doc.local_path = str(path)
                doc.bytes = len(res.content)
                doc.fetch_status = "fetched"
                stats["fetched"] += 1
                db.commit()

        if stats["fetched"] or stats["failed"]:
            log.info("docs_drained", **stats)
        return stats
