"""redaction.py — never let a credential reach a log line.

2026-08-02. The macro fetchers print raw provider error bodies, and
provider errors frequently echo the request URL — which carries
`apikey=...` in the query string. That is how a live key reached the
activity log.

Two independent mechanisms, because either alone has a hole:

  1. PATTERN redaction catches key-shaped strings even when the value is
     not one we know about (a key pasted into a config we do not read, a
     key belonging to a provider added later).
  2. VALUE redaction catches keys whose shape we cannot predict, by
     masking the literal values of any config entry whose NAME contains
     KEY / TOKEN / SECRET / PASSWORD.

Pattern-only would miss a short opaque key; value-only would miss a key
that never passed through config. Both run on every string.

Deliberately conservative about ordering: value redaction runs FIRST, so
a real key is masked by its own value before any pattern has a chance to
partially match and leave a fragment behind.
"""
import re

# Query-string credentials — the specific leak that motivated this.
_QS = re.compile(r"((?:api[_-]?key|apikey|token|secret|password|auth)=)"
                 r"[^&\s\"']+", re.I)
# Provider-shaped keys seen in this project. Each is anchored on its own
# recognisable prefix rather than a generic "long alphanumeric run",
# which would mangle order ids and security ids.
_PATTERNS = [
    re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}"),      # Anthropic
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS
    # Alpha Vantage / Twelve Data keys are 16-32 char uppercase-alnum
    # blobs with no prefix, so they are caught by VALUE redaction rather
    # than guessed at here — a generic rule would eat legitimate ids.
]
MASK = "***REDACTED***"

_SENSITIVE_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)
_MIN_VALUE_LEN = 6      # below this, masking would hit ordinary words


def sensitive_values(cfg):
    """Literal values worth masking, from any suspiciously-named entry."""
    out = []
    for k, v in (cfg or {}).items():
        if not isinstance(v, str) or len(v) < _MIN_VALUE_LEN:
            continue
        if _SENSITIVE_NAME.search(str(k)):
            out.append(v)
    # Longest first: masking a short value that is a substring of a
    # longer one would leave the longer one's tail exposed.
    return sorted(set(out), key=len, reverse=True)


def redact(text, cfg=None, limit=None):
    """Mask credentials in `text`. Never raises — this runs on a log path.

    `limit` truncates AFTER redaction, so truncation can never cut a
    secret in half and leave the first N characters visible.
    """
    try:
        s = str(text)
    except Exception:
        return MASK
    try:
        for v in sensitive_values(cfg):
            if v and v in s:
                s = s.replace(v, MASK)
        s = _QS.sub(r"\1" + MASK, s)
        for p in _PATTERNS:
            s = p.sub(MASK, s)
    except Exception:
        # A redaction bug must not become a leak. If anything goes wrong
        # here, emit nothing rather than the unredacted original.
        return MASK
    if limit and len(s) > limit:
        s = s[:limit] + "..."
    return s
