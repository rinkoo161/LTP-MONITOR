"""rate_limit.py — one shared cooldown registry for broker REST endpoints.

v58.49 (roadmap B6). Before this, two independent cooldowns guarded the
SAME Dhan quote/LTP surface:

    broker_adapter._cache["ltp_all_fail_until"]   60s on 429, 10s otherwise
    app._quote_rate_limited_until                300s on 429,  30s otherwise

Independent cooldowns on a shared resource defeat the purpose: one path
backs off politely while the other keeps hammering the endpoint that
just refused it, and the server sees no reduction in pressure. The
2026-07-29 log showed exactly this — `prev_close` backing off while
futures polling continued to 429 on the same endpoint minutes later.

Keyed by a coarse RESOURCE name rather than a URL, because that is what
the broker actually rate-limits. All quote-ish reads share one budget,
so a 429 from any of them slows all of them.

Deliberately process-wide module state rather than an injected object:
every caller is in the same process, and threading a limiter through
broker_adapter, app and three agents would touch far more code than the
problem justifies. `reset()` exists so tests can clear it by name
instead of poking globals — the leak that made
test_authoritative_prev_close fail when the first cooldown was added.
"""
import threading
import time

_lock = threading.Lock()
_until = {}      # resource -> epoch when the cooldown expires
_reason = {}     # resource -> why it was set, for logging

# Default backoffs. A 429 is an explicit "you are asking too often" and
# earns a long pause; a transient network error is not, and a long pause
# there would turn one dropped packet into minutes of blindness.
BACKOFF_429 = 300
BACKOFF_OTHER = 30
# 2026-07-30 — a 401 is not transient. An expired Dhan token will not fix
# itself, so retrying every 30s until someone notices generates hours of
# identical log lines (observed overnight at 01:55) while every retry
# still costs a request. Backed off hard, and the caller is expected to
# alert rather than silently absorb it: the ONLY fix is a human pasting
# a new token.
BACKOFF_AUTH = 1800


def is_limited(resource="quote"):
    """True while `resource` is cooling down."""
    with _lock:
        return time.time() < _until.get(resource, 0)


def remaining(resource="quote"):
    """Seconds left on the cooldown, 0 if not limited."""
    with _lock:
        return max(0.0, _until.get(resource, 0) - time.time())


def note_failure(exc_or_msg, resource="quote", on_429=None, otherwise=None):
    """Record a failure and start (or extend) the cooldown.

    Returns (is_429, seconds_set). Never shortens an existing cooldown —
    two callers failing in quick succession should not let the second
    one's shorter backoff undo the first one's longer pause.
    """
    text = str(exc_or_msg)
    is_429 = "429" in text or "Too Many Requests" in text
    is_auth = ("401" in text or "Unauthorized" in text
               or "token has expired" in text or "token expired" in text)
    if is_auth:
        secs = BACKOFF_AUTH
    elif is_429:
        secs = on_429 if on_429 is not None else BACKOFF_429
    else:
        secs = otherwise if otherwise is not None else BACKOFF_OTHER
    with _lock:
        target = time.time() + secs
        if target > _until.get(resource, 0):
            _until[resource] = target
            kind = "AUTH" if is_auth else ("429" if is_429 else "error")
            _reason[resource] = f"{kind}: {text[:120]}"
    return is_429, secs


def is_auth_failure(resource="quote"):
    """True when the active cooldown was caused by an auth failure.

    Separated from is_limited() because the two need different UI: a
    rate limit resolves itself and only warrants a quiet note, while an
    expired token needs a human and should be shouted about.
    """
    return (why(resource) or "").startswith("AUTH")


def why(resource="quote"):
    with _lock:
        return _reason.get(resource)


def reset(resource=None):
    """Clear one resource's cooldown, or all of them.

    Exists so tests clear this by name rather than reaching into module
    globals — the exact hazard that made an existing suite fail when the
    first per-path cooldown was introduced in v58.34.
    """
    with _lock:
        if resource is None:
            _until.clear()
            _reason.clear()
        else:
            _until.pop(resource, None)
            _reason.pop(resource, None)


def snapshot():
    """All active cooldowns, for display or diagnostics."""
    now = time.time()
    with _lock:
        return {r: {"remaining": round(t - now, 1), "why": _reason.get(r)}
                for r, t in _until.items() if t > now}
