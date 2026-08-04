"""Securities master — symbol ↔ ISIN ↔ name for every NSE-listed equity.

Sources (all static CSV on the archives host — the robust layer, not the
fragile JSON APIs):

    EQUITY_L.csv       main-board equities (symbol, name, series, listing
                       date, ISIN, face value)
    SME_EQUITY_L.csv   SME/Emerge board — NSE has moved this file before,
                       so several candidate paths are tried in order and
                       a miss is logged loudly rather than swallowed.
    symbolchange.csv   old symbol → new symbol history → SecurityAlias
    namechange.csv     old name → new name history   → SecurityAlias

Identity rule: ISIN is the durable key. A row whose ISIN we already hold
updates that security in place (symbol renames included); a row with a
new ISIN inserts. Renames therefore never create duplicate securities,
and the alias table keeps the old symbol resolvable for historical
filings.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketsense.core.logging import get_logger
from marketsense.db.models import Security, SecurityAlias
from marketsense.net.nse_client import NSE_ARCHIVES, NSEClient, NSEUnavailable

log = get_logger("securities_master")

EQUITY_L = NSE_ARCHIVES + "/content/equities/EQUITY_L.csv"
# NSE has hosted the SME list at different paths over time; try in order.
# Verified live 2026-08-05: the emerge/corporates path serves ~560 rows with
# underscore-style headers (NAME_OF_COMPANY, ISIN_NUMBER). The
# /content/equities/ path answers 200 but is a 1-row stub — order matters,
# and the stub must NOT win, which is why candidates needing >10 rows is
# enforced in sync_equity_lists.
SME_CANDIDATES = [
    NSE_ARCHIVES + "/emerge/corporates/content/SME_EQUITY_L.csv",
    NSE_ARCHIVES + "/content/equities/SME_EQUITY_L.csv",
]
SYMBOL_CHANGE = NSE_ARCHIVES + "/content/equities/symbolchange.csv"
NAME_CHANGE = NSE_ARCHIVES + "/content/equities/namechange.csv"


def _clean(s: str | None) -> str:
    return (s or "").strip()


def _parse_listing_date(s: str) -> datetime | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def _rows(content: bytes) -> list[dict]:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    # NSE headers vary between files: stray spaces ("NAME OF COMPANY "),
    # underscores (NAME_OF_COMPANY on the SME list). Normalise both to the
    # space form once so lookups have a single spelling.
    return [
        {_clean(k).upper().replace("_", " "): _clean(v) for k, v in row.items() if k}
        for row in reader
    ]


def _upsert_security(db: Session, row: dict, is_sme: bool) -> str:
    """Returns 'insert' | 'update' | 'skip'."""
    symbol = row.get("SYMBOL", "")
    isin = row.get("ISIN NUMBER") or row.get("ISIN") or None
    name = row.get("NAME OF COMPANY") or row.get("COMPANY NAME") or ""
    series = row.get("SERIES") or None
    if not symbol or not name:
        return "skip"

    face_value = None
    try:
        face_value = float(row.get("FACE VALUE") or 0) or None
    except ValueError:
        pass
    listing_date = _parse_listing_date(row.get("DATE OF LISTING", ""))

    existing = None
    if isin:
        existing = db.scalar(select(Security).where(Security.isin == isin))
    if existing is None:
        existing = db.scalar(
            select(Security).where(Security.symbol == symbol, Security.series == series)
        )

    if existing is None:
        db.add(
            Security(symbol=symbol, isin=isin, company_name=name, series=series,
                     is_sme=is_sme, listing_date=listing_date, face_value=face_value)
        )
        return "insert"

    changed = False
    if existing.symbol != symbol:
        # Rename detected by ISIN identity — keep the old symbol resolvable.
        db.add(SecurityAlias(security_id=existing.id, alias=existing.symbol,
                             alias_type="old_symbol"))
        log.info("symbol_rename", isin=isin, old=existing.symbol, new=symbol)
        existing.symbol = symbol
        changed = True
    if existing.company_name != name:
        db.add(SecurityAlias(security_id=existing.id, alias=existing.company_name,
                             alias_type="old_name"))
        existing.company_name = name
        changed = True
    for attr, val in (("series", series), ("isin", isin), ("is_sme", is_sme),
                      ("face_value", face_value)):
        if val is not None and getattr(existing, attr) != val:
            setattr(existing, attr, val)
            changed = True
    return "update" if changed else "skip"


def sync_equity_lists(db: Session, client: NSEClient) -> dict:
    """Fetch main-board + SME lists and upsert. Returns a stats dict."""
    stats = {"main": 0, "sme": 0, "inserts": 0, "updates": 0, "sme_source": None}

    res = client.get(EQUITY_L)
    for row in _rows(res.content):
        outcome = _upsert_security(db, row, is_sme=False)
        stats["main"] += 1
        if outcome == "insert":
            stats["inserts"] += 1
        elif outcome == "update":
            stats["updates"] += 1

    sme_rows: list[dict] = []
    for url in SME_CANDIDATES:
        try:
            res = client.get(url)
            candidate = _rows(res.content)
            # >10 guards against the known 1-row stub file answering 200.
            if len(candidate) > 10 and "SYMBOL" in candidate[0]:
                sme_rows = candidate
                stats["sme_source"] = url
                break
        except NSEUnavailable:
            continue
    if not sme_rows:
        # Loud, not fatal: main-board coverage still lands.
        log.error("sme_list_unavailable", tried=SME_CANDIDATES)
    for row in sme_rows:
        outcome = _upsert_security(db, row, is_sme=True)
        stats["sme"] += 1
        if outcome == "insert":
            stats["inserts"] += 1
        elif outcome == "update":
            stats["updates"] += 1

    db.commit()
    return stats


def sync_change_histories(db: Session, client: NSEClient) -> dict:
    """symbolchange.csv + namechange.csv → alias rows for past renames
    that predate our first equity-list sync."""
    stats = {"symbol_changes": 0, "name_changes": 0, "unmatched": 0}

    def alias_exists(security_id: int, alias: str, alias_type: str) -> bool:
        return db.scalar(
            select(func.count()).select_from(SecurityAlias).where(
                SecurityAlias.security_id == security_id,
                SecurityAlias.alias == alias,
                SecurityAlias.alias_type == alias_type,
            )
        ) > 0

    try:
        res = client.get(SYMBOL_CHANGE)
        # Verified live 2026-08-05: symbolchange.csv has NO header row.
        # Columns positionally: company name, old symbol, new symbol,
        # effective date (DD-MON-YYYY).
        text_content = res.content.decode("utf-8", errors="replace")
        for raw in csv.reader(io.StringIO(text_content)):
            if len(raw) < 4:
                continue
            _name, old, new, eff_s = (_clean(x) for x in raw[:4])
            if not old or not new or old == new:
                continue
            sec = db.scalar(select(Security).where(Security.symbol == new))
            if sec is None:
                stats["unmatched"] += 1
                continue
            if not alias_exists(sec.id, old, "old_symbol"):
                db.add(SecurityAlias(security_id=sec.id, alias=old,
                                     alias_type="old_symbol",
                                     event_at=_parse_listing_date(eff_s)))
                stats["symbol_changes"] += 1
    except NSEUnavailable as e:
        log.error("symbolchange_unavailable", error=str(e))

    try:
        res = client.get(NAME_CHANGE)
        # Verified live 2026-08-05 — header: NCH_SYMBOL, NCH_PREV_NAME,
        # NCH_NEW_NAME, NCH_DT (underscores become spaces in _rows()).
        for row in _rows(res.content):
            sym = row.get("NCH SYMBOL") or row.get("SYMBOL") or ""
            old = row.get("NCH PREV NAME") or row.get("PREVIOUS NAME") or ""
            if not sym or not old:
                continue
            sec = db.scalar(select(Security).where(Security.symbol == sym))
            if sec is None:
                stats["unmatched"] += 1
                continue
            if not alias_exists(sec.id, old, "old_name"):
                db.add(SecurityAlias(security_id=sec.id, alias=old, alias_type="old_name"))
                stats["name_changes"] += 1
    except NSEUnavailable as e:
        log.error("namechange_unavailable", error=str(e))

    db.commit()
    return stats


def resolve(db: Session, token: str) -> Security | None:
    """Symbol → security, falling back to aliases (old symbols/names).
    This is what A1 uses to attach filings to securities."""
    token = _clean(token).upper()
    if not token:
        return None
    sec = db.scalar(select(Security).where(Security.symbol == token))
    if sec:
        return sec
    if len(token) == 12 and token[:2].isalpha():
        sec = db.scalar(select(Security).where(Security.isin == token))
        if sec:
            return sec
    alias = db.scalar(
        select(SecurityAlias).where(func.upper(SecurityAlias.alias) == token)
    )
    if alias:
        return db.get(Security, alias.security_id)
    return None
