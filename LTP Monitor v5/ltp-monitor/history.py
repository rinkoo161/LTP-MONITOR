"""history.py — local historical market-data store (SQLite).

Layout (~/.ltp-monitor/history.db):
  instruments(security_id PK, symbol, kind idx|opt, leg, strike, expiry)
  candles(security_id, ts, o, h, l, c, v, oi)  PK(security_id, ts)
  sync_log(day, symbol, detail)

Design: index candles backfilled 2 years deep; option candles archived
daily (expired contracts are unavailable from brokers, so the chain
archive builds forward from the first sync). Compact + indexed: a year
of 4 indices with chains is roughly 1 GB.
"""
import os
import sqlite3
import time
from datetime import date, datetime, timedelta

DB = os.path.expanduser("~/.ltp-monitor/history.db")


def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS instruments(
        security_id TEXT PRIMARY KEY, symbol TEXT, kind TEXT,
        leg TEXT, strike REAL, expiry TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS candles(
        security_id TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL,
        v REAL, oi REAL, PRIMARY KEY(security_id, ts))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_c_ts ON candles(ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS sync_log(
        day TEXT, symbol TEXT, detail TEXT)""")
    return c


def upsert_candles(security_id, candles):
    c = _conn()
    c.executemany(
        "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
        [(security_id, int(x["ts"]), x.get("o"), x.get("h"), x.get("l"),
          x.get("c"), x.get("v"), x.get("oi")) for x in candles])
    c.commit(); c.close()
    return len(candles)


def upsert_instrument(security_id, symbol, kind, leg=None, strike=None, expiry=None):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?)",
              (str(security_id), symbol, kind, leg, strike, expiry))
    c.commit(); c.close()


def log_sync(symbol, detail):
    c = _conn()
    c.execute("INSERT INTO sync_log VALUES (?,?,?)",
              (date.today().isoformat(), symbol, detail))
    c.commit(); c.close()


def coverage():
    """Summary for the dashboard: rows, span, chain-days per symbol."""
    c = _conn()
    out = {}
    for sym, in c.execute("SELECT DISTINCT symbol FROM instruments"):
        ids = [r[0] for r in c.execute(
            "SELECT security_id FROM instruments WHERE symbol=? AND kind='idx'", (sym,))]
        n = span = 0
        if ids:
            q = ",".join("?" * len(ids))
            n, lo, hi = c.execute(
                f"SELECT COUNT(*),MIN(ts),MAX(ts) FROM candles WHERE security_id IN ({q})",
                ids).fetchone()
            span = round((hi - lo) / 86400) if n else 0
        days = c.execute("""SELECT COUNT(DISTINCT date(ts,'unixepoch')) FROM candles
            WHERE security_id IN (SELECT security_id FROM instruments
                                  WHERE symbol=? AND kind='opt')""", (sym,)).fetchone()[0]
        out[sym] = {"index_candles": n, "span_days": span, "chain_days": days}
    c.close()
    return out


def chain_days(symbol):
    """Trading days that have option-candle coverage for symbol."""
    c = _conn()
    rows = c.execute("""SELECT DISTINCT date(ts,'unixepoch') FROM candles
        WHERE security_id IN (SELECT security_id FROM instruments
                              WHERE symbol=? AND kind='opt') ORDER BY 1""",
        (symbol,)).fetchall()
    c.close()
    return [r[0] for r in rows]


def day_chain_frames(symbol, day):
    """Minute-by-minute chain reconstruction for one day.
    Returns sorted list of (ts, chain_dict) usable by analyzer.analyze."""
    c = _conn()
    insts = {r[0]: {"leg": r[1], "strike": r[2]} for r in c.execute(
        "SELECT security_id,leg,strike FROM instruments WHERE symbol=? AND kind='opt'",
        (symbol,))}
    idx = [r[0] for r in c.execute(
        "SELECT security_id FROM instruments WHERE symbol=? AND kind='idx'", (symbol,))]
    t0 = int(datetime.fromisoformat(day).timestamp())
    t1 = t0 + 86400
    frames = {}
    if idx:
        for ts, close in c.execute(
                "SELECT ts,c FROM candles WHERE security_id=? AND ts>=? AND ts<?",
                (idx[0], t0, t1)):
            frames.setdefault(ts, {"spot": close, "rows": {}})
    q = ",".join("?" * len(insts)) if insts else "''"
    for sid, ts, close, v, oi in c.execute(
            f"SELECT security_id,ts,c,v,oi FROM candles WHERE security_id IN ({q}) "
            "AND ts>=? AND ts<?", (*insts, t0, t1)):
        f = frames.get(ts)
        if f is None:
            continue
        meta = insts[sid]
        row = f["rows"].setdefault(meta["strike"], {
            "strike": meta["strike"],
            "ce": {"ltp": 0, "oi": 0, "oi_chg": 0, "volume": 0, "iv": 0,
                  "chg": 0, "bid": 0, "ask": 0, "security_id": None,
                  "delta": None, "theta": None, "gamma": None, "vega": None},
            "pe": {"ltp": 0, "oi": 0, "oi_chg": 0, "volume": 0, "iv": 0,
                  "chg": 0, "bid": 0, "ask": 0, "security_id": None,
                  "delta": None, "theta": None, "gamma": None, "vega": None}})
        row[meta["leg"].lower()] = {"ltp": close, "oi": oi, "oi_chg": 0,
                                    "volume": v, "iv": None, "chg": 0,
                                    "bid": None, "ask": None, "security_id": sid,
                                    "delta": None, "theta": None,
                                    "gamma": None, "vega": None}
    c.close()
    out = []
    for ts in sorted(frames):
        f = frames[ts]
        if len(f["rows"]) >= 8:            # enough strikes to analyze
            rows = [f["rows"][k] for k in sorted(f["rows"])]
            # backfill oi_chg vs previous frame
            out.append((ts, {"symbol": symbol, "spot": f["spot"],
                             "expiry": "", "rows": rows,
                             "atm": min(f["rows"], key=lambda s: abs(s - f["spot"]))}))
    for i in range(1, len(out)):
        prev = {r["strike"]: r for r in out[i - 1][1]["rows"]}
        for r in out[i][1]["rows"]:
            p = prev.get(r["strike"])
            for leg in ("ce", "pe"):
                if p and r[leg].get("oi") is not None and p[leg].get("oi") is not None:
                    r[leg]["oi_chg"] = r[leg]["oi"] - p[leg]["oi"]
    return out


def index_days(symbol, limit=250):
    """Most recent trading days with index 1m coverage (oldest first)."""
    c = _conn()
    rows = c.execute("""SELECT date(ts,'unixepoch'), COUNT(*) FROM candles
        WHERE security_id IN (SELECT security_id FROM instruments
                              WHERE symbol=? AND kind='idx')
        GROUP BY 1 HAVING COUNT(*)>=100 ORDER BY 1 DESC LIMIT ?""",
        (symbol, limit)).fetchall()
    c.close()
    return [r[0] for r in rows][::-1]


def day_index_candles(symbol, day):
    from datetime import datetime as _dt
    c = _conn()
    sec = c.execute("SELECT security_id FROM instruments WHERE symbol=? AND kind='idx'",
                    (symbol,)).fetchone()
    if not sec:
        c.close(); return []
    t0 = int(_dt.fromisoformat(day).timestamp()); t1 = t0 + 86400
    rows = c.execute("SELECT ts,o,h,l,c FROM candles WHERE security_id=? "
                     "AND ts>=? AND ts<? ORDER BY ts", (sec[0], t0, t1)).fetchall()
    c.close()
    return [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
            for r in rows]


# ------------------------------------------------------------ sync
SEG = {"NIFTY": ("IDX_I", "13"), "BANKNIFTY": ("IDX_I", "25"),
       "FINNIFTY": ("IDX_I", "27"), "SENSEX": ("IDX_I", "51")}


def sync_index_history(dhan, symbol, years=2, log=print):
    """Backfill index minute candles as deep as the API serves (chunked,
    paced), plus daily candles for `years`."""
    import requests
    seg_kind, sec = SEG[symbol]
    upsert_instrument(sec, symbol, "idx")
    total = 0
    to = date.today()
    empty_chunks = 0
    while (date.today() - to).days < years * 365 and empty_chunks < 3:
        frm = to - timedelta(days=75)
        try:
            time.sleep(1.2)
            data = dhan._intraday_range(symbol, "1", frm.isoformat(), to.isoformat())
            n = upsert_candles(sec, data)
            total += n
            empty_chunks = empty_chunks + 1 if n == 0 else 0
            log(f"  {symbol} {frm}..{to}: {n} candles")
        except Exception as e:
            log(f"  {symbol} {frm}..{to}: stopped ({str(e)[:60]})")
            break
        to = frm - timedelta(days=1)
    log_sync(symbol, f"index backfill {total} candles")
    return total


def _dhan_call_with_retry(fn, *args, attempts=3, base_delay=3, **kwargs):
    """Retry a Dhan call with backoff — 502/500/DNS blips are often
    transient (confirmed 2026-07-21: SENSEX hit three DIFFERENT
    transient error types at three different times the same day, all
    on Dhan specifically, none on NIFTY/BANKNIFTY/FINNIFTY — a retry
    would very plausibly have recovered most of these)."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    raise last_err


def sync_day_chain(get_chain, dhan, symbol, log=print, progress=lambda m: None):
    """Archive today's chain: register instruments from the live chain and
    store 1m candles for each strike (paced)."""
    day = date.today().isoformat()
    # market-closed guard: if the index printed no candles today there is
    # nothing to archive (weekends/holidays) — bail out fast. IMPORTANT:
    # a real API failure (502/500/DNS) must NOT be silently relabeled as
    # "market closed" — that mislabeling is exactly what hid a real Dhan
    # outage for SENSEX on 2026-07-21 behind a misleading log line.
    seg_kind, sec = SEG[symbol]
    try:
        probe = _dhan_call_with_retry(dhan._intraday_range, symbol, "1", day, day)
    except Exception as e:
        log(f"  {symbol}: chain sync FAILED after retries — {str(e)[:150]}")
        log_sync(symbol, f"failed: {str(e)[:100]}")
        return 0
    if not probe:
        log(f"  {symbol}: no candles today (market closed) — skipping")
        log_sync(symbol, "skipped: market closed")
        return 0
    upsert_candles(sec, probe)
    chain = get_chain(symbol)
    n_ok = 0
    n_failed = 0
    total = sum(1 for r in chain["rows"] for l in ("ce","pe")
                if r[l].get("security_id"))
    for row in chain["rows"]:
        for leg in ("ce", "pe"):
            sid = row[leg].get("security_id")
            if not sid:
                continue
            upsert_instrument(sid, symbol, "opt", leg.upper(), row["strike"],
                              chain.get("expiry"))
            try:
                time.sleep(1.2)
                data = _dhan_call_with_retry(dhan._intraday_range_sid, sid, "1", day, day)
                if data:
                    upsert_candles(str(sid), data)
                    n_ok += 1
                progress(f"{n_ok}/{total} legs")
            except Exception as e:
                n_failed += 1
                log(f"  {symbol} {row['strike']}{leg.upper()}: {str(e)[:50]}")
    if n_failed:
        log(f"  {symbol}: {n_failed} legs failed even after retries — "
           f"archive for {day} is PARTIAL, not complete")
    log_sync(symbol, f"day chain {n_ok} legs" + (f" ({n_failed} failed)" if n_failed else ""))
    log(f"  {symbol}: archived {n_ok} option legs for {day}")
    return n_ok
