"""Postgres-as-event-bus.

publish() appends to the outbox INSIDE the caller's transaction — an event
exists if and only if the facts it describes committed. After commit the
caller (or a SQLAlchemy after_commit hook) fires NOTIFY as a wake-up; the
notify is best-effort because delivery does not depend on it: consumers
poll their offset on a timer as well, so a missed NOTIFY costs latency,
never an event.

Consumer semantics (at-least-once):
  * read events with id > last_acked_id for my (consumer, topic)
  * handle each; on success advance the offset
  * on failure retry up to MAX_ATTEMPTS in-process, then write a
    dead_letters row and advance PAST the poison event — one bad filing
    must not wedge the stream (§3: agents survive one another's failure)
  * replay = reset the offset row; events are never deleted in Phase 1
"""
from __future__ import annotations

import json
import time
from typing import Callable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from marketsense.bus.topics import NOTIFY_CHANNEL
from marketsense.core.logging import get_logger
from marketsense.db.models import ConsumerOffset, DeadLetter, Outbox

log = get_logger("bus")

MAX_ATTEMPTS = 3


def publish(db: Session, topic: str, payload: dict) -> Outbox:
    """Append an event in the CURRENT transaction. Caller commits."""
    evt = Outbox(topic=topic, payload=payload)
    db.add(evt)
    db.flush()  # assign the offset id now so callers can log it
    return evt


def notify(db: Session) -> None:
    """Best-effort wake-up after commit. Safe to skip; polling covers it."""
    try:
        db.execute(text(f"NOTIFY {NOTIFY_CHANNEL}"))
    except Exception as e:
        log.warning("notify_failed", error=str(e))


class Consumer:
    """Poll-based at-least-once consumer with dead-lettering.

    handler(event: Outbox) -> None. Raise to signal failure.
    """

    def __init__(
        self,
        name: str,
        topic: str,
        handler: Callable[[Outbox], None],
        session_factory: Callable[[], Session],
        batch_size: int = 100,
    ) -> None:
        self.name = name
        self.topic = topic
        self.handler = handler
        self.session_factory = session_factory
        self.batch_size = batch_size

    def _offset_row(self, db: Session) -> ConsumerOffset:
        row = db.get(ConsumerOffset, (self.name, self.topic))
        if row is None:
            row = ConsumerOffset(consumer=self.name, topic=self.topic, last_acked_id=0)
            db.add(row)
            db.flush()
        return row

    def drain(self) -> int:
        """Handle everything pending. Returns number of events processed
        (acked or dead-lettered). Each event is its own transaction, so a
        crash mid-batch re-delivers only the unacked tail."""
        processed = 0
        while True:
            with self.session_factory() as db:
                offset = self._offset_row(db)
                events = list(
                    db.scalars(
                        select(Outbox)
                        .where(Outbox.topic == self.topic, Outbox.id > offset.last_acked_id)
                        .order_by(Outbox.id)
                        .limit(self.batch_size)
                    )
                )
                db.commit()
            if not events:
                return processed

            for evt in events:
                self._handle_one(evt)
                processed += 1

    def _handle_one(self, evt: Outbox) -> None:
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.handler(evt)
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("handler_error", consumer=self.name, outbox_id=evt.id,
                            attempt=attempt, error=last_error)
                time.sleep(min(0.2 * 2**attempt, 2.0))
        else:
            with self.session_factory() as db:
                db.add(DeadLetter(consumer=self.name, outbox_id=evt.id, topic=evt.topic,
                                  payload=evt.payload, error=last_error,
                                  attempts=MAX_ATTEMPTS))
                db.commit()
            log.error("dead_lettered", consumer=self.name, outbox_id=evt.id)

        # ack (advance past the event whether handled or dead-lettered)
        with self.session_factory() as db:
            offset = self._offset_row(db)
            if evt.id > offset.last_acked_id:
                offset.last_acked_id = evt.id
            db.commit()

    def reset(self, to_offset: int = 0) -> None:
        """Replay: rewind the high-water mark."""
        with self.session_factory() as db:
            offset = self._offset_row(db)
            offset.last_acked_id = to_offset
            db.commit()
        log.info("offset_reset", consumer=self.name, topic=self.topic, to=to_offset)


def payload_json(evt: Outbox) -> dict:
    p = evt.payload
    return p if isinstance(p, dict) else json.loads(p)
