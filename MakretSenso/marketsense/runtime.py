"""Process-level singletons: THE NSEClient (one per process, §2.5) wired
to the budget from settings and an audit sink writing http_audit rows."""
from __future__ import annotations

from functools import lru_cache

from marketsense.core.config import settings
from marketsense.core.logging import get_logger
from marketsense.db.engine import session
from marketsense.db.models import HttpAudit
from marketsense.net.budget import RequestBudget
from marketsense.net.nse_client import NSEClient

log = get_logger("runtime")


def _audit_sink(rec: dict) -> None:
    try:
        with session() as db:
            b = rec.get("budget", {})
            db.add(
                HttpAudit(
                    url=rec["url"],
                    status=rec.get("status"),
                    elapsed_ms=rec.get("elapsed_ms"),
                    attempt=rec.get("attempt", 0),
                    error=rec.get("error"),
                    budget_tokens=b.get("tokens_available"),
                    breaker_open=b.get("breaker_open"),
                )
            )
            db.commit()
    except Exception as e:
        # The audit sink must never break a fetch; log and move on.
        log.warning("audit_write_failed", error=str(e))


@lru_cache(maxsize=1)
def nse_client() -> NSEClient:
    s = settings()
    budget = RequestBudget(
        per_minute=s.nse_budget_per_min,
        breaker_threshold=s.nse_breaker_threshold,
        breaker_cooldown=float(s.nse_breaker_cooldown),
    )
    return NSEClient(budget=budget, audit=_audit_sink)
