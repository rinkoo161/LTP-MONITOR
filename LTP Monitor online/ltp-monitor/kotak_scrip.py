"""kotak_scrip.py — Kotak scrip master over the PUBLIC, unauthenticated URL.

`kotak_quotes` can read the whole option chain in one ~290 ms call using
nothing but a consumer key — no TOTP, no MPIN, no daily session. That
advantage evaporates if resolving strike -> instrument token needs an
authenticated call, because then the daily login is back on the critical
path.

`KotakNeoClient._load_master()` resolves tokens the authenticated way
(`/script-details/1.0/masterscrip/file-paths`). This module takes the
same CSVs from the public date-stamped path instead:

    https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/
        {YYYY-MM-DD}/transformed/{nse_fo|bse_fo}.csv

so the quotes path stays session-free end to end.

ONE PARSER, TWO CALLERS
-----------------------
The row parsing is NOT duplicated here. `broker_adapter._find` and
`broker_adapter._parse_expiry` were lifted to module level for exactly
this, and both loaders call them. They encode two things a fork would
get quietly wrong:

  * Kotak's column names carry inconsistent trailing whitespace
    ('lExpiryDate ' vs 'lExpiryDate'), and one is literally named
    'dStrikePrice;' — with the semicolon.
  * `lExpiryDate` is epoch + 315511200 for nse_fo/cde_fo and raw epoch
    for bse_fo/mcx_fo. The earlier ~10-year heuristic was off by six
    hours: close enough to look right, wrong enough to matter.

Strikes are stored x100.

TODAY'S FILE MAY NOT EXIST YET
------------------------------
Verified 2026-08-17 at ~03:00 IST: that day's path returned 403 while
the previous day's returned 200. So this walks back day by day rather
than assuming today, and reports which date it actually used — a
resolver that silently served a stale calendar would be worse than one
that failed.

NOT WIRED INTO ANY TRADING PATH.
"""
import csv
import datetime
import io
import json
import os
import time

import requests

import broker_adapter as _ba

PUBLIC_URL = ("https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/"
              "{date}/transformed/{fname}")
SEGMENT_FILE = {"nse_fo": "nse_fo.csv", "bse_fo": "bse_fo.csv"}
NSE_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
BSE_NAMES = ("SENSEX", "BSXOPT")
MAX_LOOKBACK_DAYS = 5
CACHE_DIR = os.path.expanduser("~/.ltp-monitor")

_cache = {}          # segment -> (date_used, [row dicts])


def _fetch_csv(segment, max_lookback=MAX_LOOKBACK_DAYS, timeout=90):
    """(date_used, csv_text). Walks back until a day is published."""
    fname = SEGMENT_FILE[segment]
    today = datetime.date.today()
    errors = []
    for delta in range(max_lookback):
        d = (today - datetime.timedelta(days=delta)).isoformat()
        disk = os.path.join(CACHE_DIR, f"kotak_public_{segment}_{d}.csv")
        if os.path.exists(disk):
            return d, open(disk, encoding="utf-8", errors="ignore").read()
        try:
            r = requests.get(PUBLIC_URL.format(date=d, fname=fname),
                             timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            errors.append(f"{d}: {type(e).__name__}")
            continue
        if r.status_code == 200 and len(r.content) > 10000:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(disk, "w", encoding="utf-8") as f:
                f.write(r.text)
            return d, r.text
        errors.append(f"{d}: HTTP {r.status_code}")
    raise RuntimeError(
        f"no published {fname} in the last {max_lookback} days ({'; '.join(errors)})")


def load(segment="nse_fo", names=None, force=False):
    """[{token, type, strike, lot, expiry, tsym, segment}] for `names`.

    Shape is deliberately identical to KotakNeoClient._load_master()'s
    entries, so a caller can be switched between the two without
    touching anything downstream.
    """
    names = tuple(n.upper() for n in (names or (
        NSE_NAMES if segment == "nse_fo" else BSE_NAMES)))
    key = (segment, names)
    if not force and key in _cache:
        return _cache[key]

    date_used, text = _fetch_csv(segment)
    out = []
    skipped = 0
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("pSymbolName") or "").strip().upper()
        if name not in names:
            continue
        if (row.get("pOptionType") or "").strip() not in ("CE", "PE"):
            continue
        expiry, _src = _ba._parse_expiry(row, segment)   # shared, not forked
        if expiry <= 0:
            skipped += 1
            continue
        out.append({
            "name": name,
            "token": (row.get("pSymbol") or "").strip(),
            "type": (row.get("pOptionType") or "").strip(),
            "strike": float(row.get("dStrikePrice;") or 0) / 100.0,
            "lot": int(_ba._find(row, "lotsize") or 0),
            "expiry": expiry,
            "tsym": (row.get("pTrdSymbol") or "").strip(),
            "segment": segment,
        })
    _cache[key] = out
    _cache[("_meta", segment)] = {"date_used": date_used, "skipped": skipped,
                                  "rows": len(out)}
    return out


def meta(segment="nse_fo"):
    """Which scrip-master date the resolved tokens actually came from."""
    return _cache.get(("_meta", segment))


def expiries(symbol, segment=None):
    """Sorted future expiry epochs available for `symbol`."""
    segment = segment or ("bse_fo" if symbol.upper() == "SENSEX" else "nse_fo")
    now = time.time()
    return sorted({r["expiry"] for r in load(segment)
                   if r["name"] == symbol.upper() and r["expiry"] >= now - 86400})


def chain_tokens(symbol, spot, width=10, expiry=None, segment=None):
    """[(segment, token)] for the ATM +/- `width` strikes, both legs.

    Returns the pairs `kotak_quotes.quotes()` wants, so a full chain is
    one batched call. `width=10` is 21 strikes x 2 legs = 42 tokens,
    far inside the feed's 3000-token ceiling.
    """
    segment = segment or ("bse_fo" if symbol.upper() == "SENSEX" else "nse_fo")
    rows = [r for r in load(segment) if r["name"] == symbol.upper()]
    if not rows:
        raise RuntimeError(f"no {symbol} contracts in the {segment} master")
    if expiry is None:
        fut = expiries(symbol, segment)
        if not fut:
            raise RuntimeError(f"no future expiry for {symbol}")
        expiry = fut[0]
    rows = [r for r in rows if r["expiry"] == expiry]
    strikes = sorted({r["strike"] for r in rows})
    if not strikes:
        raise RuntimeError(f"no strikes for {symbol} at expiry {expiry}")
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    keep = set(strikes[max(0, i - width): i + width + 1])
    sel = [r for r in rows if r["strike"] in keep]
    sel.sort(key=lambda r: (r["strike"], r["type"]))
    return [(r["segment"], r["token"]) for r in sel], sel, expiry
