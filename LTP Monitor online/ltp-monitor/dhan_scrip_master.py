"""dhan_scrip_master.py — futures instrument lookup via Dhan's scrip
master CSV.

Options don't need this: broker_adapter.py's option_chain() already
returns security_id per strike/leg directly from Dhan's Option Chain
REST endpoint. Futures are different — Dhan's option-chain endpoint
does not return futures contracts at all, and there's no "futures
chain" API. The scrip master CSV is the only way to find a futures
contract's security_id, and unlike option legs (looked up fresh every
REST poll), futures security_ids are effectively permanent-per-month:
a NEW security_id is generated for each new month's contract, and the
old one simply stops being current once it expires — there is no way
to compute a future month's security_id from a formula, it has to be
looked up.

Design: rather than hardcode security_ids (which would go stale every
month — exactly the problem this module exists to avoid), this
downloads Dhan's scrip master CSV, filters to FUTIDX rows for a given
underlying, and picks whichever unexpired contract has the NEAREST
expiry — i.e. "current month" is always computed dynamically from the
expiry dates themselves, so it self-updates through every monthly
rollover with no manual security-ID maintenance.

SWITCHED 2026-07-25 to the DETAILED file (api-scrip-master-detailed.csv),
per explicit request with real confirmed rows (not guessed) — this is
what actually fixes SENSEX futures, which the earlier compact-file
version (api-scrip-master.csv, SEM_* columns) could never resolve: the
compact file has no UNDERLYING_SYMBOL column at all, forcing a fragile
trading-symbol-prefix-parsing fallback, and its BSE/SENSEX exchange
code was never confirmed. The detailed file's real schema, confirmed
directly from user-provided sample rows:
  EXCH_ID  SEGMENT  SECURITY_ID  ISIN  INSTRUMENT
  UNDERLYING_SECURITY_ID  UNDERLYING_SYMBOL  SYMBOL_NAME  DISPLAY_NAME
  INSTRUMENT_TYPE  SERIES  LOT_SIZE  SM_EXPIRY_DATE  STRIKE_PRICE
  OPTION_TYPE  TICK_SIZE  EXPIRY_FLAG  ... (plus margin/ASM/GSM columns
  not needed here)
Sample FUTIDX rows (all confirmed real, 2026-07-25):
  BSE, D, 1144507, FUTIDX, 1, SENSEX, BSXFUT, "SENSEX JUL  FUT", ...,
    LOT_SIZE=20, SM_EXPIRY_DATE="30/07/26", TICK_SIZE=5
  NSE, D, 61093, FUTIDX, 26000, NIFTY, "NIFTY-Jul2026-FUT", ...,
    LOT_SIZE=65, SM_EXPIRY_DATE="28/07/26", TICK_SIZE=10
This resolves the previously-unconfirmed SENSEX exchange code directly:
EXCH_ID is the plain text "BSE" for SENSEX FUTIDX rows in THIS file —
no BSE_FNO/BFO guessing needed. UNDERLYING_SYMBOL is populated
directly and cleanly for every sampled row (unlike the compact file,
where it was empty for FUTIDX) — so the trading-symbol-prefix fallback
in _derive_underlying_symbol is kept only as a defensive fallback, not
the primary path anymore.

TICK_SIZE and LOT_SIZE are both surfaced in the returned dict — per
explicit request, these feed margin-required calculations (alongside
option-chain instruments elsewhere in the system, which already carry
their own lot size from broker_adapter.py).

REMAINING HONEST STATUS: this sandbox's egress allowlist still blocks
images.dhan.co directly (confirmed via `curl -I` → 403 host_not_allowed),
so this parsing logic is validated against samples built from the
user's exact confirmed rows above — not a live download. The schema
itself is no longer a guess for the columns actually used here, but a
live run of test_dhan_scrip_master.py against the real 25MB+ file is
still needed to confirm there's no row-level surprise (e.g. a stray
UNDERLYING_SYMBOL value that doesn't exactly match "SENSEX"/"NIFTY"/
etc.) at full scale.
"""
import csv
import io
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STORE_DIR = os.path.expanduser("~/.ltp-monitor")
CACHE_PATH = os.path.join(STORE_DIR, "scrip_master_detailed.csv")
CACHE_TTL_HOURS = 20   # refresh roughly daily — this file is large and
                       # doesn't change intraday; new-month contracts
                       # appear well ahead of expiry

# Switched 2026-07-25 to the DETAILED file per explicit request — see
# module docstring for why (SENSEX futures fix, real UNDERLYING_SYMBOL
# column, confirmed real sample rows).
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Same exchange convention already established in broker_adapter.py's
# UNDERLYINGS dict — NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY futures trade
# on NSE; SENSEX futures trade on BSE. Confirmed 2026-07-25 directly
# from a real SENSEX FUTIDX row in the detailed file: EXCH_ID="BSE"
# plainly — the earlier BSE_FNO/BFO guesses are no longer needed, but
# kept as trailing fallbacks in case the file ever varies.
SYMBOL_EXCHANGE = {
    "NIFTY": ("NSE",), "BANKNIFTY": ("NSE",), "FINNIFTY": ("NSE",),
    "MIDCPNIFTY": ("NSE",), "NIFTYNXT50": ("NSE",),
    "SENSEX": ("BSE", "BSE_FNO", "BFO"),
}

# Column name candidates tried in order. Detailed-CSV names (confirmed
# real, 2026-07-25) listed FIRST; the earlier compact-CSV SEM_* names
# kept as fallbacks in case a different scrip master variant is ever
# used instead.
COLUMN_CANDIDATES = {
    "security_id": ("SECURITY_ID", "SEM_SMST_SECURITY_ID", "Security_ID"),
    "exch_id": ("EXCH_ID", "SEM_EXM_EXCH_ID"),
    "segment": ("SEGMENT", "SEM_SEGMENT"),
    "instrument": ("INSTRUMENT", "SEM_INSTRUMENT_NAME"),
    "instrument_type": ("INSTRUMENT_TYPE", "SEM_EXCH_INSTRUMENT_TYPE"),
    "underlying_security_id": ("UNDERLYING_SECURITY_ID",),
    "underlying_symbol": ("UNDERLYING_SYMBOL", "SM_SYMBOL_NAME"),
    "symbol_name": ("SYMBOL_NAME", "SEM_TRADING_SYMBOL"),
    "display_name": ("DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"),
    "expiry_date": ("SM_EXPIRY_DATE", "SEM_EXPIRY_DATE"),
    "lot_size": ("LOT_SIZE", "SEM_LOT_UNITS"),
    "tick_size": ("TICK_SIZE", "SEM_TICK_SIZE"),
    "strike_price": ("STRIKE_PRICE", "SEM_STRIKE_PRICE"),
    "option_type": ("OPTION_TYPE", "SEM_OPTION_TYPE"),
}


def _resolve_columns(fieldnames):
    """Map our logical field names to whichever actual column name is
    present in this CSV — defends against compact-vs-detailed naming
    differences without guessing wrong silently."""
    fieldset = set(fieldnames or [])
    resolved = {}
    missing = []
    for logical, candidates in COLUMN_CANDIDATES.items():
        found = next((c for c in candidates if c in fieldset), None)
        if found:
            resolved[logical] = found
        else:
            missing.append(logical)
    return resolved, missing


def _parse_expiry(raw):
    """Detailed-file format confirmed 2026-07-25 from real sample
    rows: "DD/MM/YY" with NO time component (e.g. "30/07/26",
    "28/07/26") — different from the compact file's "DD/MM/YY HH:MM".
    Both tried, plus older fallbacks, in case either file variant is
    ever used."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d/%m/%y", "%d/%m/%y %H:%M", "%Y-%m-%d", "%d-%b-%Y",
               "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None



def fetch_csv_text(force=False):
    """Download (with a local daily cache) the detailed scrip master
    CSV. Returns the raw text. Raises on total failure (no cache and
    no network) rather than silently returning nothing — a caller
    needs to know this failed, not get an empty future list that
    looks like "no contract exists"."""
    os.makedirs(STORE_DIR, exist_ok=True)
    if not force and os.path.exists(CACHE_PATH):
        age_hours = (time.time() - os.path.getmtime(CACHE_PATH)) / 3600
        if age_hours < CACHE_TTL_HOURS:
            with open(CACHE_PATH, encoding="utf-8", errors="ignore") as f:
                return f.read()
    req = urllib.request.Request(
        SCRIP_MASTER_URL, headers={"User-Agent": "ltp-monitor/1.0"})
    # This is a large file (all NSE/BSE/MCX instruments) — Dhan's own
    # docs don't specify a rate limit for it, but it's fetched once a
    # day at most either way given the cache above.
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def get_current_future(symbol, csv_text=None):
    """Return the nearest-unexpired FUTIDX contract for `symbol`, e.g.
    {"security_id": "68390", "symbol_name": "BANKNIFTY-Sep2026-FUT",
    "display_name": "BANKNIFTY SEP FUT", "expiry": datetime(...),
    "lot_size": 35, "exchange": "NSE"}, or None if nothing matched
    (expired list, wrong symbol, or the CSV schema didn't resolve —
    check the returned error detail via get_current_future_detailed).

    "Current" is computed dynamically from expiry dates — this is
    what makes the monthly rollover automatic: no security_id is ever
    hardcoded, so a new contract appearing (or an old one expiring)
    is picked up on the next call with no code change needed.
    """
    result, _ = get_current_future_detailed(symbol, csv_text)
    return result


def _derive_underlying_symbol(row, cols):
    """SM_SYMBOL_NAME confirmed empty for live FUTIDX rows (2026-07-24
    — the staged diagnostics below caught this directly: symbol_matches
    was 0 with 'sample underlying_symbol values seen: ['']'). Falls
    back to parsing SEM_TRADING_SYMBOL's prefix before the first
    hyphen, which the user's own originally-confirmed sample row
    showed IS reliably populated: "FINNIFTY-Jul2026-FUT" for FINNIFTY,
    security_id 61091. Tries the direct column first in case it's
    populated for other instrument types (options, equities) even
    though it's empty for FUTIDX specifically."""
    direct = row.get(cols["underlying_symbol"], "").strip().upper()
    if direct:
        return direct
    trading_symbol = row.get(cols.get("symbol_name", ""), "").strip().upper()
    if trading_symbol and "-" in trading_symbol:
        return trading_symbol.split("-")[0]
    return ""


def get_current_future_detailed(symbol, csv_text=None):
    """Same as get_current_future() but also returns a diagnostic
    dict explaining what happened — used by the validation test so a
    failure is actionable instead of a bare None."""
    results, diag = get_current_futures_detailed(symbol, n=1, csv_text=csv_text)
    if results:
        return results[0], diag
    return None, diag


def _scan_futures_for_symbol(rows, cols, symbol, n, now):
    """The actual per-symbol candidate scan, factored out so it can run
    against ALREADY-PARSED rows — shared by both get_current_futures_
    detailed() (single symbol, parses its own copy) and get_current_
    futures_for_symbols() (many symbols, ONE shared parse). Added
    2026-07-25 after a live report: resolving 4 symbols sequentially,
    each via its own get_current_futures_detailed() call, re-parsed the
    same ~200k-row CSV from scratch 4 times — a visible ~3-5s-per-
    symbol staggered delay (parsing/scanning that many rows in Python
    isn't free), even though the download itself was already 20h-
    cached. Extracting the scan step is what lets the multi-symbol
    entry point below do the expensive `list(reader)` materialization
    exactly once regardless of how many symbols are being resolved."""
    exch_candidates = SYMBOL_EXCHANGE.get(symbol)
    if not exch_candidates:
        return [], {"error": f"no exchange mapping for symbol {symbol!r}"}

    all_exch_seen = set()
    attempts = []
    for exch in exch_candidates:
        stage_counts = {"exch_matches": 0, "instrument_matches": 0, "symbol_matches": 0}
        sample_symbol_names = set()
        candidates = []
        for row in rows:
            row_exch = row.get(cols["exch_id"], "").strip().upper()
            if row_exch:
                all_exch_seen.add(row_exch)
            if row_exch != exch:
                continue
            stage_counts["exch_matches"] += 1
            if row.get(cols["instrument"], "").strip().upper() != "FUTIDX":
                continue
            stage_counts["instrument_matches"] += 1
            row_symbol = _derive_underlying_symbol(row, cols)
            if len(sample_symbol_names) < 15:
                sample_symbol_names.add(row_symbol)
            if row_symbol != symbol:
                continue
            stage_counts["symbol_matches"] += 1
            expiry = _parse_expiry(row.get(cols["expiry_date"], ""))
            if not expiry or expiry < now:
                continue   # already expired — not a candidate for "current"
            candidates.append((expiry, row))

        if candidates:
            candidates.sort(key=lambda t: t[0])
            results = []
            for expiry, row in candidates[:n]:
                results.append({
                    "security_id": row.get(cols["security_id"]),
                    "symbol_name": row.get(cols.get("symbol_name", ""), ""),
                    "display_name": row.get(cols.get("display_name", ""), ""),
                    "expiry": expiry,
                    "lot_size": row.get(cols.get("lot_size", ""), ""),
                    "tick_size": row.get(cols.get("tick_size", ""), ""),
                    "exchange": exch,
                })
            return results, {"candidates_found": len(candidates),
                             "returned": len(results), "exch_code_used": exch}
        attempts.append({"exch_tried": exch, "stage_counts": stage_counts,
                         "sample_symbol_names": sorted(sample_symbol_names)[:15]})

    diag = {
        "error": f"no unexpired FUTIDX contract found for {symbol} — "
                f"tried exchange codes {list(exch_candidates)}, none worked",
        "attempts": attempts,
        "all_exchange_codes_seen_in_file": sorted(all_exch_seen),
    }
    for a in attempts:
        if a["stage_counts"]["exch_matches"] == 0:
            continue
        if a["stage_counts"]["instrument_matches"] == 0:
            diag["likely_cause"] = (f"exchange {a['exch_tried']!r} has rows "
                                    f"but none with instrument=FUTIDX")
            break
        if a["stage_counts"]["symbol_matches"] == 0:
            diag["likely_cause"] = (f"exchange {a['exch_tried']!r} has FUTIDX "
                                    f"rows but none matched symbol {symbol!r} "
                                    f"— sample names seen: "
                                    f"{a['sample_symbol_names']}")
            break
    else:
        diag["likely_cause"] = (f"NONE of {list(exch_candidates)} matched any "
                                f"row at all — check "
                                f"all_exchange_codes_seen_in_file above for "
                                f"the actual code to add to SYMBOL_EXCHANGE")
    return [], diag


def _parse_scrip_master(csv_text):
    """Shared CSV-parse step: DictReader + column resolution + the
    required-columns check, materialized once. Returns (rows, cols) on
    success, or (None, error_dict) on a schema mismatch."""
    reader = csv.DictReader(io.StringIO(csv_text))
    cols, missing = _resolve_columns(reader.fieldnames)
    required = ("security_id", "exch_id", "instrument", "underlying_symbol",
               "expiry_date")
    missing_required = [c for c in required if c in missing]
    if missing_required:
        return None, {"error": f"CSV schema mismatch — missing required "
                      f"columns: {missing_required} (found: "
                      f"{reader.fieldnames})"}
    return list(reader), cols


def get_current_futures_detailed(symbol, n=3, csv_text=None):
    """Same row-scanning logic as get_current_future_detailed, but
    returns up to `n` nearest-unexpired FUTIDX contracts (sorted by
    expiry ascending) instead of just the single nearest one.

    Added 2026-07-25 per explicit request: "there are 2 more months -
    capture those as well" — futures OI/volume walls benefit from
    seeing the next couple of months' contracts too (same idea as the
    options chain covering multiple strikes, applied across expiries
    instead). Front-month behavior is UNCHANGED: get_current_future_
    detailed() above still returns exactly the single nearest contract
    it always did, by calling this with n=1 — existing callers (the
    live strategy/OI-buildup pipeline) are unaffected.

    For resolving MULTIPLE symbols (e.g. all 4 indices), prefer
    get_current_futures_for_symbols() instead — this single-symbol
    version parses the whole CSV fresh on every call, which is fine
    for one symbol but wasteful (and was the actual cause of a live-
    reported ~3-5s-per-symbol staggered delay) when called once per
    symbol in a loop.

    Returns (list_of_up_to_n_contracts, diagnostic_dict). An empty list
    (not None) means nothing was found — check the diagnostic dict the
    same way get_current_future_detailed's callers already do.
    """
    symbol = symbol.upper()
    if csv_text is None:
        try:
            csv_text = fetch_csv_text()
        except Exception as e:
            return [], {"error": f"failed to fetch scrip master: {e}"}
    rows, cols = _parse_scrip_master(csv_text)
    if rows is None:
        return [], cols   # cols is actually the error dict in this branch
    return _scan_futures_for_symbol(rows, cols, symbol, n, datetime.now(IST))


def get_current_futures_for_symbols(symbols, n=3, csv_text=None):
    """Resolves MULTIPLE symbols' current futures in ONE CSV parse —
    added 2026-07-25 specifically to fix the ~3-5s-per-symbol staggered
    delay a live report showed when 4 symbols were each resolved via
    their own separate get_current_futures_detailed() call (4x
    redundant parse+scan of the same ~200k-row file). Downloads/parses
    the CSV exactly once here regardless of how many symbols are
    requested, then scans it once per symbol against the SAME already-
    parsed rows.

    Returns {symbol.upper(): (results_list, diag), ...} — same per-
    symbol shape get_current_futures_detailed() returns, just computed
    together so the expensive part happens once."""
    symbols = [s.upper() for s in symbols]
    if csv_text is None:
        try:
            csv_text = fetch_csv_text()
        except Exception as e:
            err = {"error": f"failed to fetch scrip master: {e}"}
            return {s: ([], err) for s in symbols}
    rows, cols = _parse_scrip_master(csv_text)
    if rows is None:
        return {s: ([], cols) for s in symbols}   # cols is the error dict here
    now = datetime.now(IST)
    return {s: _scan_futures_for_symbol(rows, cols, s, n, now) for s in symbols}
