#!/usr/bin/env python3
"""filings.py — NSE corporate announcements for a symbol, with a
materiality tier.

Phase 1 of the stock-options work, and READ-ONLY: nothing here feeds a
strategy, a risk gate or an order. It exists so a watchlist symbol shows
what the company actually told the exchange, and so the noise is visibly
separated from the things that move a price.

WHY A TIER AND NOT A SENTIMENT SCORE. This project already has a news
sentiment pipeline, and v58.51 established that its classifier had never
been checked against real candle outcomes. Guessing a direction from a
headline is exactly that unvalidated move. A materiality TIER makes a
weaker, checkable claim — "this kind of filing is the kind that can move
a price" — and says nothing about which way.

THE TIERS ARE GROUNDED IN WHAT IS ACTUALLY FILED, not invented. Measured
across 875 ADANIENSOL announcements on 2026-08-05, 73 distinct
categories. The frequent ones:

    125  Updates                             MEDIUM
    104  Analysts/Institutional Investor Meet MEDIUM
     80  Press Release                       MEDIUM
     57  Disclosure under SEBI Takeover Regs     LOW  (routine SAST)
     47  Outcome of Board Meeting             HIGH
     45  Acquisition                          HIGH
     39  Shareholders meeting                MEDIUM
     37  Financial Result Updates             HIGH
     32  Trading Window                          LOW
     25  News Verification                       LOW
     21  Credit Rating                        HIGH
     20  Certificate under SEBI (Depositories)   LOW
     13  Copy of Newspaper Publication           LOW

Tier split after tightening: 255 high (29%), 407 medium (47%), 213 low
(24%). Before tightening it was 34% high, because bare 'takeover' matched
57 routine SAST shareholding disclosures and bare 'loss of' matched 'loss
of share certificate' — both measured, not guessed at.

A category the list does not know is MEDIUM, never LOW. Silently
demoting an unrecognised filing to noise is how a genuinely material
announcement would disappear from the page.
"""
import re
import time

# Matched against the announcement's `desc` (its exchange category),
# lowercased, first hit wins. Ordered most-specific first: "schedule of
# analysts meet" is an appointment, "analysts meet updates" is content.
_HIGH = (
    r"financial result|outcome of board meeting|board meeting outcome|"
    r"acquisition|amalgamation|merger|demerger|scheme of arrangement|"
    r"credit rating|open offer|delisting|"
    r"change in management|change in director|resignation|"
    r"fund rais|preferential issue|qip|rights issue|bonus|split|"
    r"dividend|buyback|"
    r"order.{0,12}(receipt|won|bagged)|award of (contract|order)|"
    r"insolvency|winding up|penalt|adjudicat|show cause|"
    r"suspension|default|"
    r"fire|accident|shutdown|force majeure"
)
_LOW = (
    r"trading window|newspaper publication|"
    r"certificate under sebi|compliance certificate|"
    r"news verification|clarification.{0,20}news|"
    r"schedule of|intimation of (meeting|schedule)|"
    r"record date|book closure|"
    r"reg\.? ?(74|39|7\(3\))|share transfer agent|"
    r"loss of share certificate|duplicate share|"
    # 2026-08-05 — measured, not assumed. Bare 'takeover' fired on 57
    # of 875 ADANIENSOL filings, ALL routine SEBI SAST shareholding
    # disclosures rather than actual takeovers, and bare 'loss of'
    # fired on 'loss of share certificate'. Together they inflated
    # HIGH to 34% of all filings, which makes a materiality tier
    # worthless. Real takeover events still reach HIGH via 'open offer'.
    r"takeover regulation|substantial acquisition of shares|"
    r"sast|shareholding pattern"
)

TIERS = ("high", "medium", "low")
_CACHE = {}
CACHE_TTL = 300          # NSE is rate-sensitive; 5 min is plenty for filings
NSE_URL = "https://www.nseindia.com/api/corporate-announcements"


def materiality(desc, text=""):
    """('high'|'medium'|'low', why) for one announcement.

    `why` names the rule that fired, so a wrong tier can be traced to a
    pattern rather than argued about.
    """
    blob = f"{desc or ''} {text or ''}".lower()
    m = re.search(_HIGH, blob)
    if m:
        return "high", f"matched '{m.group(0)[:40]}'"
    m = re.search(_LOW, blob)
    if m:
        return "low", f"routine/administrative ('{m.group(0)[:34]}')"
    # UNRECOGNISED IS MEDIUM, NEVER LOW. Demoting an unknown category to
    # noise is how a material filing vanishes from the page.
    return "medium", "category not in the high/low lists"


def _parse_dt(row):
    """Announcement time as an epoch, or 0. NSE ships several shapes."""
    for key in ("an_dt", "sort_date", "dt", "exchdisstime"):
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
            try:
                return time.mktime(time.strptime(raw[:19], fmt))
            except ValueError:
                continue
    return 0


def fetch(symbol, limit=40, force=False):
    """Recent announcements for `symbol`, newest first, tiered.

    Returns [] on any failure rather than raising: this is a display
    panel, and an NSE outage must not take a page down. The reason is
    carried on the result so the UI can say WHY it is empty instead of
    implying the company filed nothing.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"symbol": sym, "filings": [], "error": "no symbol"}
    hit = _CACHE.get(sym)
    if hit and not force and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        import nse_client
        sess = nse_client.NSEClient()._get_session()
        r = sess.get(f"{NSE_URL}?index=equities&symbol={sym}", timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"NSE returned {r.status_code}")
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError("unexpected payload shape")
    except Exception as e:
        # Do NOT cache a failure: the next view should retry.
        return {"symbol": sym, "filings": [],
                "error": f"NSE announcements unavailable: {str(e)[:120]}"}
    out = []
    for row in rows:
        desc = (row.get("desc") or "").strip()
        text = (row.get("attchmntText") or "").strip()
        tier, why = materiality(desc, text)
        out.append({
            "symbol": row.get("symbol") or sym,
            "ts": _parse_dt(row),
            "when": (row.get("an_dt") or "").strip(),
            "category": desc,
            "text": text[:400],
            "attachment": (row.get("attchmntFile") or "").strip() or None,
            "tier": tier,
            "why": why,
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    res = {"symbol": sym, "filings": out[:limit], "error": None,
           "counts": {t: sum(1 for x in out if x["tier"] == t) for t in TIERS},
           "total": len(out)}
    _CACHE[sym] = (time.time(), res)
    return res
