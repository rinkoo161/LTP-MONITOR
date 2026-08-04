"""RSS entry → normalised filing dict. Pure functions, fixture-testable."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

import feedparser

from marketsense.core.clock import IST

# BHARTIARTL_05082026000335_Report.pdf → BHARTIARTL
# Symbols may carry & and - (M&M, BAJAJ-AUTO).
_SYMBOL_IN_URL = re.compile(r"/corporate/([A-Z0-9&\-]{1,20})_\d{8,}")

_PUB_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y")


def parse_pub(ts: str | None) -> datetime | None:
    """NSE RSS pubDate is naive IST ('05-Aug-2026 00:03:49')."""
    if not ts:
        return None
    ts = ts.strip()
    for fmt in _PUB_FORMATS:
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    # feedparser may have normalised to RFC822 — let it try
    try:
        parsed = feedparser.datetimes._parse_date(ts)  # noqa: SLF001
        if parsed:
            return datetime(*parsed[:6], tzinfo=IST)
    except Exception:
        pass
    return None


def symbol_from_link(link: str | None) -> str | None:
    if not link:
        return None
    m = _SYMBOL_IN_URL.search(link)
    return m.group(1) if m else None


# Several feeds (brsr, annual_reports, encumbrance, some related_party)
# publish an EMPTY pubDate — verified live 2026-08-05. Their attachment
# filenames carry the timestamp instead, in two shapes:
#   BRSR_..._20260804_233837946.xml      yyyymmdd _ HHMMSS(fff)
#   AR_..._10030945_04082026232241.pdf   ddmmyyyy HHMMSS
_LINK_TS_YMD = re.compile(r"_(20\d{6})_(\d{6})")
_LINK_TS_DMY = re.compile(r"_(\d{8})(\d{6})(?:\D|$)")


def event_at_from_link(link: str | None) -> datetime | None:
    if not link:
        return None
    name = link.rsplit("/", 1)[-1]
    m = _LINK_TS_YMD.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=IST)
        except ValueError:
            pass
    m = _LINK_TS_DMY.search(name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%d%m%Y%H%M%S").replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def split_summary_fields(summary: str | None) -> dict[str, str]:
    """'A: x |SUBJECT: y' → {'A': 'x', 'SUBJECT': 'y'}. Free text that
    precedes the first KEY: stays under '_text'."""
    out: dict[str, str] = {}
    if not summary:
        return out
    free: list[str] = []
    for part in summary.split("|"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, _, val = part.partition(":")
            key = key.strip().upper()
            # keys are short ALL-CAPS labels; anything else is free text
            if key and len(key) <= 40 and re.fullmatch(r"[A-Z0-9 /&()\-.]+", key):
                out[key] = val.strip()
                continue
        free.append(part)
    if free:
        out["_text"] = " | ".join(free)
    return out


def content_hash(feed: str, title: str, link: str, pub: str, summary: str) -> str:
    canon = "\x1f".join((feed, title or "", link or "", pub or "", summary or ""))
    return hashlib.sha256(canon.encode("utf-8", errors="replace")).hexdigest()


def parse_feed(feed_name: str, content: bytes) -> list[dict]:
    """Parse one RSS payload into normalised entry dicts (newest first,
    as the feed orders them)."""
    parsed = feedparser.parse(content)
    entries = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        pub_raw = (e.get("published") or "").strip()
        summary = (e.get("summary") or "").strip()
        fields = split_summary_fields(summary)
        entries.append(
            {
                "feed": feed_name,
                "company_title": title,
                "link": link or None,
                "symbol_hint": symbol_from_link(link),
                "event_at": parse_pub(pub_raw) or event_at_from_link(link),
                "subject": fields.get("SUBJECT") or fields.get("_text") or title,
                "description": summary or None,
                "fields": fields,
                "content_hash": content_hash(feed_name, title, link, pub_raw, summary),
                # link is unique per attachment on NSE; a rolling feed can
                # re-serve the same item across polls — that's the dedup.
                "dedup_key": link or None,
            }
        )
    return entries
