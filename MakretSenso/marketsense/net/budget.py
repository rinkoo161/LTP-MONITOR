"""Request budget + circuit breaker for the NSE gateway.

Semantics chosen deliberately:

* The token bucket REFUSES when empty — it does not queue. A queued
  request fired late is how polite pollers turn into thundering herds
  after a stall. Callers (feed pollers) treat a refusal as "skip this
  cycle"; the feed is polled again on its next schedule anyway.

* The circuit breaker opens on N consecutive auth-shaped failures
  (401/403), because with Akamai those mean "this session is burned",
  and continuing to hammer converts a soft block into an IP-level one.
  While open, every acquire() is refused except a single half-open probe
  after the cooldown.

This is the process-wide registry pattern from ltp-monitor's
rate_limit.py — independent per-caller cooldowns on one host was a real
bug there; here the budget object is shared by construction.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Per-minute request budget exhausted; skip this cycle."""


class CircuitOpen(Exception):
    """NSE is refusing us; stop asking until the cooldown elapses."""


@dataclass
class RequestBudget:
    per_minute: int = 30
    breaker_threshold: int = 2
    breaker_cooldown: float = 180.0
    clock: callable = time.monotonic  # injectable for tests

    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _consecutive_auth_failures: int = field(init=False, default=0)
    _breaker_opened_at: float | None = field(init=False, default=None)
    _half_open_probe_out: bool = field(init=False, default=False)
    # counters for the health page
    granted: int = field(init=False, default=0)
    refused_budget: int = field(init=False, default=0)
    refused_breaker: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._tokens = float(self.per_minute)
        self._last_refill = self.clock()

    # -- token bucket ---------------------------------------------------
    def _refill(self) -> None:
        now = self.clock()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(
            float(self.per_minute), self._tokens + elapsed * (self.per_minute / 60.0)
        )

    def acquire(self) -> None:
        """Take one token or raise. Never blocks."""
        with self._lock:
            if self._breaker_opened_at is not None:
                elapsed = self.clock() - self._breaker_opened_at
                if elapsed < self.breaker_cooldown or self._half_open_probe_out:
                    self.refused_breaker += 1
                    raise CircuitOpen(
                        f"breaker open ({elapsed:.0f}s of {self.breaker_cooldown:.0f}s cooldown)"
                    )
                # cooldown elapsed: allow exactly one probe through
                self._half_open_probe_out = True

            self._refill()
            if self._tokens < 1.0:
                self.refused_budget += 1
                raise BudgetExceeded(
                    f"budget {self.per_minute}/min exhausted; refill in "
                    f"{(1.0 - self._tokens) * 60.0 / self.per_minute:.1f}s"
                )
            self._tokens -= 1.0
            self.granted += 1

    # -- breaker feedback ----------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            self._consecutive_auth_failures = 0
            self._breaker_opened_at = None
            self._half_open_probe_out = False

    def record_auth_failure(self) -> None:
        """401/403 — the session is burned. Counts toward opening the breaker."""
        with self._lock:
            self._half_open_probe_out = False
            self._consecutive_auth_failures += 1
            if self._consecutive_auth_failures >= self.breaker_threshold:
                # (re)open — a failed half-open probe restarts the cooldown
                self._breaker_opened_at = self.clock()

    def record_transport_failure(self) -> None:
        """Timeout / connection error — bad, but not auth-shaped. The
        breaker is specifically an anti-ban device, so transport noise
        does not open it; backoff in the client handles it."""
        with self._lock:
            self._half_open_probe_out = False

    # -- introspection --------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            self._refill()
            return {
                "per_minute": self.per_minute,
                "tokens_available": round(self._tokens, 2),
                "breaker_open": self._breaker_opened_at is not None,
                "consecutive_auth_failures": self._consecutive_auth_failures,
                "granted": self.granted,
                "refused_budget": self.refused_budget,
                "refused_breaker": self.refused_breaker,
            }
