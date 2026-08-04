#!/usr/bin/env python3
"""instrument_registry.py — resolve any F&O underlying from Dhan's scrip
master, so the symbol list stops being four hardcoded indices.

Phase 1 of the stock-options work (2026-08-04), and DATA ONLY. Nothing
here places an order or enables a strategy. It answers one question:
"given a symbol the user typed, what does the rest of the system need to
know about it, and is it actually tradable at all?"

WHY A REGISTRY RATHER THAN MORE DICTS. The index assumptions are spread
across at least six places — `broker_adapter.UNDERLYINGS`, a hardcoded
`"UnderlyingSeg": "IDX_I"`, `FNO_SEGMENT`, `history.SEG`, the
`{"IDX_I": [...]}` quote-batch shape, and `config.lot_sizes`. Adding a
fifth symbol to five dicts is exactly the near-duplicate drift this
project keeps being bitten by (the market-session check, the news
regexes, the OI quadrant classifier were each collapsed to one
definition for the same reason). One descriptor, resolved from the CSV
that is already the source of truth for futures contracts.

WHAT THE CSV ACTUALLY GIVES US, verified 2026-08-04:

    UNDERLYING_SYMBOL   ADANIENSOL          the user-facing name
    INSTRUMENT          OPTSTK/FUTSTK/EQUITY
    SECURITY_ID         10217 (NSE equity)  what the API wants
    LOT_SIZE            675.0               per-contract, and it CHANGES
    EXCH_ID             NSE / BSE           dual-listed names exist

LOT SIZE IS READ, NEVER HARDCODED. `config.lot_sizes` carries four
indices and has already been wrong once this project (corrected in
v59.0). Stock F&O lots are revised by the exchange far more often than
index lots, so a hardcoded table for stocks would be wrong by
construction.

A TRAP WORTH KNOWING. ADANIPORTS is listed on both exchanges and its
BSE futures rows carry `SYMBOL_NAME = 'MPSLFUT'` — nothing to do with
the underlying. Matching on SYMBOL_NAME silently resolves the wrong
contract, so everything here matches on UNDERLYING_SYMBOL + EXCH_ID.
"""
import csv
import io

# Dhan's option-chain call needs an "underlying segment". Indices live in
# IDX_I; equities whose options we want live in NSE_FNO. Verified against
# the live API on 2026-08-04: ADANIENSOL (scrip 10217, seg NSE_FNO)
# returns a real expiry list and a 63-strike chain.
SEG_FOR_EXCH = {"NSE": "NSE_FNO", "BSE": "BSE_FNO"}

_CACHE = {}


def _rows(force=False):
    """Parsed scrip-master rows, cached per process.

    Deliberately reuses dhan_scrip_master.fetch_csv_text() — the same
    fetch (and the same on-disk cache) the futures resolver uses. A
    second downloader for a 34 MB file would be both wasteful and the
    two-copies problem again.
    """
    if _CACHE.get("rows") is not None and not force:
        return _CACHE["rows"]
    import dhan_scrip_master as sm
    text = sm.fetch_csv_text(force=force)
    rows = list(csv.DictReader(io.StringIO(text)))
    _CACHE["rows"] = rows
    return rows


def _norm(sym):
    return (sym or "").strip().upper()


def resolve(symbol, exchange="NSE"):
    """Descriptor for `symbol`, or None if it has no options at all.

    Returns:
        {symbol, exchange, underlying_id, underlying_seg, fno_segment,
         lot_size, n_option_rows, n_future_rows, kind}

    `kind` is "index" or "stock" — the two differ in more than segment
    (stock options are MONTHLY only; indices carry weeklies), and
    callers that care should branch on this rather than on a name.
    """
    sym = _norm(symbol)
    exch = _norm(exchange) or "NSE"
    if not sym:
        return None
    opts = futs = 0
    lot = None
    underlying_id = None
    kind = None
    for r in _rows():
        if _norm(r.get("UNDERLYING_SYMBOL")) != sym:
            continue
        if _norm(r.get("EXCH_ID")) != exch:
            continue
        inst = (r.get("INSTRUMENT") or "").upper()
        if inst in ("OPTSTK", "OPTIDX"):
            opts += 1
            kind = "stock" if inst == "OPTSTK" else "index"
            if not lot:
                try:
                    lot = int(float(r.get("LOT_SIZE") or 0)) or None
                except (TypeError, ValueError):
                    pass
        elif inst in ("FUTSTK", "FUTIDX"):
            futs += 1
        elif inst in ("EQUITY", "INDEX"):
            # The UNDERLYING the option chain is quoted against. For a
            # stock that is the equity security_id; for an index it is
            # the index id.
            if underlying_id is None:
                underlying_id = (r.get("SECURITY_ID") or "").strip() or None
    if not opts:
        return None
    kind = kind or "stock"
    # The option-chain call's UnderlyingSeg is NOT the same as the F&O
    # segment the contracts live in. An INDEX is quoted against IDX_I
    # (that is what broker_adapter has always sent); a STOCK is quoted
    # against its exchange's F&O segment. Returning NSE_FNO for NIFTY
    # compiled fine and would simply have failed at the API — caught by
    # running the registry against NIFTY, which is why indices are
    # resolved here too rather than left on the old hardcoded path.
    return {
        "symbol": sym,
        "exchange": exch,
        "underlying_id": underlying_id,
        "underlying_seg": ("IDX_I" if kind == "index"
                           else SEG_FOR_EXCH.get(exch, "NSE_FNO")),
        "fno_segment": SEG_FOR_EXCH.get(exch, "NSE_FNO"),
        "lot_size": lot,
        "n_option_rows": opts,
        "n_future_rows": futs,
        "kind": kind,
    }


def validate(symbol, exchange="NSE"):
    """(ok, reason, descriptor) — say WHY, not just no.

    The reason string is user-facing: this is what a Settings field shows
    when someone types a name that will not work, and "invalid symbol" is
    useless to them.
    """
    sym = _norm(symbol)
    if not sym:
        return False, "empty symbol", None
    d = resolve(sym, exchange)
    if d is None:
        # Distinguish "no such name" from "exists but has no options",
        # because they need different fixes from the user.
        known = any(_norm(r.get("UNDERLYING_SYMBOL")) == sym for r in _rows())
        if not known:
            return False, (f"{sym} is not in Dhan's scrip master — check "
                           f"the spelling against the underlying name, "
                           f"e.g. ADANIENSOL not 'Adani Energy'"), None
        return False, (f"{sym} exists but has no options on {exchange} — "
                       f"only F&O underlyings can be analysed here"), None
    if not d["underlying_id"]:
        return False, (f"{sym} has options but no resolvable underlying "
                       f"security id on {exchange} — the option chain call "
                       f"needs one"), d
    if not d["lot_size"]:
        return False, (f"{sym} has no LOT_SIZE in the scrip master — "
                       f"sizing cannot be derived and must never be "
                       f"guessed"), d
    return True, (f"{sym}: {d['n_option_rows']} option and "
                  f"{d['n_future_rows']} future contracts, lot "
                  f"{d['lot_size']}"), d


def search(prefix, limit=25):
    """Underlyings whose name contains `prefix` AND actually have options.

    For a Settings picker. Filtering to option-bearing names here is the
    point: offering a name the system cannot analyse is how you get a
    support question instead of a trade.
    """
    p = _norm(prefix)
    if not p:
        return []
    seen = {}
    for r in _rows():
        u = _norm(r.get("UNDERLYING_SYMBOL"))
        if not u or p not in u:
            continue
        if (r.get("INSTRUMENT") or "").upper() not in ("OPTSTK", "OPTIDX"):
            continue
        key = (u, _norm(r.get("EXCH_ID")))
        seen[key] = seen.get(key, 0) + 1
    out = [{"symbol": u, "exchange": e, "option_rows": n}
           for (u, e), n in seen.items()]
    out.sort(key=lambda x: (-x["option_rows"], x["symbol"]))
    return out[:limit]


def futures(symbol, exchange="NSE", n=3):
    """Nearest-expiry futures contracts for any underlying.

    Same SHAPE as dhan_scrip_master.get_current_futures_for_symbols()
    returns per symbol — [{security_id, symbol_name, expiry}] sorted by
    expiry — so callers can use either without branching on the data.

    This exists because that resolver carries an index-only exchange
    map and answers "no exchange mapping for symbol 'ADANIENSOL'". It is
    NOT a second resolver competing with it: callers keep using the
    original for the four indices, whose behaviour is proven, and reach
    for this only for names it cannot express. Same split as
    broker_adapter._scrip_and_seg, and for the same reason — a live path
    should not change to support a data-only feature.

    Expired contracts are dropped: the CSV keeps them, and archiving a
    contract that no longer trades would quietly write empty days.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    ist = _tz(_td(hours=5, minutes=30))
    today = _dt.now(ist).date()
    sym, exch = _norm(symbol), _norm(exchange) or "NSE"
    out = []
    for r in _rows():
        if _norm(r.get("UNDERLYING_SYMBOL")) != sym:
            continue
        if _norm(r.get("EXCH_ID")) != exch:
            continue
        if (r.get("INSTRUMENT") or "").upper() not in ("FUTSTK", "FUTIDX"):
            continue
        raw = (r.get("SM_EXPIRY_DATE") or "").strip()
        if not raw:
            continue
        try:
            exp = _dt.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=ist)
        except ValueError:
            continue
        if exp.date() < today:
            continue
        out.append({"security_id": (r.get("SECURITY_ID") or "").strip(),
                    "symbol_name": r.get("SYMBOL_NAME"),
                    "expiry": exp})
    out.sort(key=lambda x: x["expiry"])
    return out[:n]
