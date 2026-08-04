"""The single NSE gateway. One instance per ingest process, no exceptions.

Hardening, in order of what actually matters (proven by the LTP Monitor
codebase and NSE's observed behaviour):

1. TLS fingerprint. NSE's Akamai checks the TLS ClientHello, not the
   headers — plain requests/httpx is blocked even with perfect browser
   headers. curl_cffi with Chrome impersonation is REQUIRED, not an
   optimisation. There is deliberately no requests fallback here: a
   fallback that always gets 403'd is worse than a loud ImportError.

2. Cookie bootstrap. JSON APIs under nseindia.com need cookies set by the
   homepage. Bootstrapped lazily, refreshed on 401/403 once per request,
   and aged out after SESSION_MAX_AGE.

3. Budget + breaker (net/budget.py) around every request. The archives
   host (nsearchives.nseindia.com) serves static files and does not need
   cookies, but shares the same budget — one polite footprint total.

4. Conditional GET. RSS feeds honour ETag/Last-Modified; an unchanged
   feed costs a 304 and ~200 bytes. Cache state lives in memory and is
   re-primed after restart (one full fetch per feed — acceptable).

5. Audit. Every request emits an audit record via the injected callback:
   url, status, latency, retry count, budget snapshot. The DB writer is
   injected so this module stays import-light and testable.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from curl_cffi import requests as creq

from marketsense.core.logging import get_logger
from marketsense.net.budget import BudgetExceeded, CircuitOpen, RequestBudget

log = get_logger("nse_client")

NSE_WWW = "https://www.nseindia.com"
NSE_ARCHIVES = "https://nsearchives.nseindia.com"

SESSION_MAX_AGE = 300.0  # seconds; NSE cookies age out fast
MAX_RETRIES = 3

IMPERSONATE = "chrome"  # curl_cffi picks its newest supported Chrome


class NSEUnavailable(Exception):
    """All retries exhausted or breaker open; caller should skip the cycle."""


@dataclass
class FetchResult:
    status: int
    content: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool  # True on 304 — content is empty, use your cache
    url: str
    elapsed_ms: int


class NSEClient:
    """Shared, thread-safe NSE HTTP gateway with budget, breaker, and audit."""

    def __init__(
        self,
        budget: RequestBudget,
        audit: Callable[[dict], None] | None = None,
    ) -> None:
        self.budget = budget
        self._audit = audit or (lambda rec: None)
        self._lock = threading.Lock()
        self._session: creq.Session | None = None
        self._session_ts = 0.0
        # url -> (etag, last_modified) for conditional GET
        self._validators: dict[str, tuple[str | None, str | None]] = {}

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------
    def _bootstrap_session(self) -> creq.Session:
        s = creq.Session(impersonate=IMPERSONATE)
        s.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": NSE_WWW + "/",
            }
        )
        # Homepage sets the cookies the JSON APIs check.
        r = s.get(NSE_WWW, timeout=15)
        log.info("session_bootstrap", status=r.status_code, cookies=len(s.cookies))
        return s

    def _get_session(self, force: bool = False) -> creq.Session:
        with self._lock:
            stale = (
                self._session is None
                or force
                or time.monotonic() - self._session_ts > SESSION_MAX_AGE
            )
            if stale:
                # Bootstrap consumes budget too — it is a real request.
                self.budget.acquire()
                self._session = self._bootstrap_session()
                self._session_ts = time.monotonic()
            return self._session

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        conditional: bool = False,
        timeout: float = 20.0,
    ) -> FetchResult:
        """GET with budget, breaker, retry, and (optionally) conditional GET.

        Raises NSEUnavailable when the cycle should be skipped. Callers
        must treat that as routine, not exceptional — it IS the polite
        behaviour working.
        """
        last_err: str = ""
        for attempt in range(MAX_RETRIES):
            try:
                self.budget.acquire()
            except (BudgetExceeded, CircuitOpen) as e:
                raise NSEUnavailable(str(e)) from e

            headers = {}
            if conditional:
                etag, lastmod = self._validators.get(url, (None, None))
                if etag:
                    headers["If-None-Match"] = etag
                if lastmod:
                    headers["If-Modified-Since"] = lastmod

            needs_cookies = url.startswith(NSE_WWW)
            t0 = time.monotonic()
            try:
                if needs_cookies:
                    s = self._get_session(force=attempt > 0)
                    r = s.get(url, headers=headers, timeout=timeout)
                else:
                    # archives host: static files, no cookie dance
                    r = creq.get(
                        url, headers=headers, timeout=timeout, impersonate=IMPERSONATE
                    )
            except Exception as e:
                self.budget.record_transport_failure()
                last_err = f"transport: {e}"
                self._emit_audit(url, None, t0, attempt, last_err)
                self._sleep_backoff(attempt)
                continue

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            self._emit_audit(url, r.status_code, t0, attempt, "")

            if r.status_code in (401, 403):
                self.budget.record_auth_failure()
                last_err = f"HTTP {r.status_code}"
                log.warning("auth_rejected", url=url, status=r.status_code, attempt=attempt)
                self._sleep_backoff(attempt)
                continue

            if r.status_code == 304:
                self.budget.record_success()
                return FetchResult(304, b"", *self._validators.get(url, (None, None)),
                                   not_modified=True, url=url, elapsed_ms=elapsed_ms)

            if r.status_code == 200:
                self.budget.record_success()
                etag = r.headers.get("etag")
                lastmod = r.headers.get("last-modified")
                if conditional and (etag or lastmod):
                    self._validators[url] = (etag, lastmod)
                return FetchResult(200, r.content, etag, lastmod,
                                   not_modified=False, url=url, elapsed_ms=elapsed_ms)

            # 429/5xx/anything else: back off and retry
            self.budget.record_transport_failure()
            last_err = f"HTTP {r.status_code}"
            self._sleep_backoff(attempt)

        raise NSEUnavailable(f"{url} failed after {MAX_RETRIES} attempts (last: {last_err})")

    def get_json(self, url: str, *, timeout: float = 20.0):
        import json

        res = self.get(url, timeout=timeout)
        return json.loads(res.content)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # jittered exponential: ~1.5s, ~3s, ~6s
        time.sleep((1.5 * 2**attempt) * (0.7 + 0.6 * random.random()))

    def _emit_audit(
        self, url: str, status: int | None, t0: float, attempt: int, error: str
    ) -> None:
        try:
            self._audit(
                {
                    "url": url,
                    "status": status,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "attempt": attempt,
                    "error": error or None,
                    "budget": self.budget.snapshot(),
                }
            )
        except Exception as e:  # the audit sink must never break a fetch
            log.warning("audit_sink_error", error=str(e))
