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
    # 2026-07-25 — added per explicit request: persist daily OHLC to a
    # local DB rather than deriving Previous Day levels from a live
    # multi-day candle re-fetch every cycle. One row per symbol per
    # trading day — separate from the tick-level `candles` table above
    # (which is keyed by security_id, meant for per-instrument
    # intraday series) since this only needs a single end-of-day
    # summary row, not the full intraday series.
    c.execute("""CREATE TABLE IF NOT EXISTS daily_ohlc(
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, PRIMARY KEY(symbol, date))""")
    # Price-bucketed volume — foundational storage for the "Volume
    # Profile" gap explicitly flagged as unavailable in Feature #3
    # (support_resistance.py). Built now per the same explicit
    # request ("store volume profile... for analysis in database") —
    # the actual Volume Profile ANALYSIS (identifying high-volume
    # nodes as S/R) is not yet wired into support_resistance.py, but
    # the data needed for it is now being captured rather than
    # discarded, so that feature won't need a data-collection lead
    # time later.
    c.execute("""CREATE TABLE IF NOT EXISTS volume_profile(
        symbol TEXT, date TEXT, price_bucket REAL, volume REAL,
        PRIMARY KEY(symbol, date, price_bucket))""")
    # 2026-07-25 — Feature #4 (Option Chain Intelligence Engine), per
    # the spec's "Store historical snapshots every configurable
    # interval... to calculate intraday trends" requirement. One row
    # per strike per leg per snapshot — persisted at a configurable
    # cadence (chain_snapshot_interval_sec, default 60s) by
    # TechnicalAgent, NOT on every ~3s chain refresh, to keep this
    # table's growth bounded (spec's own Performance section: avoid
    # full recalculation on every tick). Only the analyzed focus
    # window (~10 strikes either side of ATM) is persisted, matching
    # what analyze() already computes — no new API calls.
    c.execute("""CREATE TABLE IF NOT EXISTS chain_snapshots(
        symbol TEXT, strike REAL, leg TEXT, ts INTEGER,
        ltp REAL, oi REAL, oi_chg REAL, volume REAL, iv REAL,
        delta REAL, gamma REAL, theta REAL, vega REAL,
        bid REAL, ask REAL,
        PRIMARY KEY(symbol, strike, leg, ts))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_cs_symbol_ts ON chain_snapshots(symbol, ts)")
    return c


def upsert_daily_ohlc(symbol, date_str, open_, high, low, close, volume=None):
    """Idempotent — safe to call repeatedly through a session (e.g.
    once per candle-builder cycle, updating high/low/close as the day
    progresses) since it's a REPLACE on the (symbol, date) key, not an
    append."""
    c = _conn()
    c.execute("INSERT OR REPLACE INTO daily_ohlc VALUES (?,?,?,?,?,?,?)",
             (symbol, date_str, open_, high, low, close, volume))
    c.commit()
    c.close()


def get_previous_day_ohlc(symbol, before_date=None):
    """Most recent persisted daily_ohlc row strictly before
    `before_date` (defaults to today). Returns None if nothing's
    persisted yet (e.g. first run before any daily close has been
    captured) — callers should fall back to the live multi-day-candle
    derivation in that case, not hard-fail."""
    from agents import now_ist
    before_date = before_date or now_ist().strftime("%Y-%m-%d")
    c = _conn()
    row = c.execute(
        """SELECT date, open, high, low, close, volume FROM daily_ohlc
           WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1""",
        (symbol, before_date)).fetchone()
    c.close()
    if not row:
        return None
    return {"date": row[0], "open": row[1], "high": row[2], "low": row[3],
           "close": row[4], "volume": row[5]}


def record_volume_at_price(symbol, date_str, price, volume, bucket_size=None):
    """Accumulates volume into a price bucket for the given symbol/day
    — the raw data a future Volume Profile feature needs. bucket_size
    defaults per symbol (matches typical option-strike gaps, a
    reasonable granularity for index-level price buckets): 50 for
    NIFTY/BANKNIFTY-scale indices, 100 for SENSEX-scale."""
    if bucket_size is None:
        bucket_size = 100 if price > 40000 else 50
    bucket = round(price / bucket_size) * bucket_size
    c = _conn()
    c.execute("""INSERT INTO volume_profile VALUES (?,?,?,?)
               ON CONFLICT(symbol, date, price_bucket)
               DO UPDATE SET volume = volume + excluded.volume""",
             (symbol, date_str, bucket, volume))
    c.commit()
    c.close()


def get_volume_profile(symbol, date_str):
    """Returns [(price_bucket, volume), ...] sorted by volume
    descending — the highest-volume price levels for the day, which a
    future Volume Profile S/R feature would read directly."""
    c = _conn()
    rows = c.execute(
        """SELECT price_bucket, volume FROM volume_profile
           WHERE symbol=? AND date=? ORDER BY volume DESC""",
        (symbol, date_str)).fetchall()
    c.close()
    return rows


def upsert_index_candles(symbol, candles, timeframe):
    """Persist a symbol's index candles at a given timeframe into the
    existing `candles` table, reusing the exact security_id convention
    MarketDataAgent._build_candle already established for the 1m
    websocket-tick builder ("{symbol}_SPOT_1m") — extended here to
    "{symbol}_SPOT_{timeframe}m" for 5m/15m too, so both mechanisms
    write into the SAME table/key for a given timeframe rather than a
    parallel store (upsert is REPLACE-on-(security_id,ts), so this is
    safe to call repeatedly and safe to coexist with the tick builder).

    Added 2026-07-25 per explicit request: "store the candles in local
    db for further use and analysis, now onwards". RegimeAgent already
    fetches c1/c5/c15 for all four symbols every ~90s via REST during
    market hours (needed for regime/bias/levels) — this reuses that
    exact fetch, no new API calls, and covers ALL symbols on every
    cycle rather than only whichever symbol happens to be receiving
    live websocket ticks. `candles` here uses Dhan's
    time/open/high/low/close dict shape (as returned by d.intraday());
    volume/oi aren't part of the index intraday response, left None.
    """
    if not candles:
        return 0
    security_id = f"{symbol}_SPOT_{timeframe}m"
    rows = [{"ts": c["time"], "o": c.get("open"), "h": c.get("high"),
             "l": c.get("low"), "c": c.get("close"), "v": c.get("volume"),
             "oi": None} for c in candles if c.get("time") is not None]
    return upsert_candles(security_id, rows)


def upsert_chain_snapshot(symbol, ts, strikes_out):
    """Persists one option-chain snapshot (the analyzed focus window —
    ~10 strikes either side of ATM, matching analyzer.analyze()'s own
    `strikes` output) at timestamp `ts`. `strikes_out` is exactly
    analyze()'s `strikes` list: [{"strike": ..., "ce": {...}, "pe":
    {...}}, ...] with ltp/oi/oi_chg/volume/iv/delta/gamma/theta/vega/
    bid/ask already present per leg — no reshaping needed by callers.
    Idempotent (REPLACE on symbol+strike+leg+ts)."""
    rows = []
    for s in strikes_out:
        strike = s["strike"]
        for leg in ("ce", "pe"):
            d = s.get(leg) or {}
            rows.append((symbol, strike, leg, int(ts),
                        d.get("ltp"), d.get("oi"), d.get("oi_chg"),
                        d.get("volume"), d.get("iv"), d.get("delta"),
                        d.get("gamma"), d.get("theta"), d.get("vega"),
                        d.get("bid"), d.get("ask")))
    if not rows:
        return 0
    c = _conn()
    c.executemany(
        "INSERT OR REPLACE INTO chain_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    c.commit(); c.close()
    return len(rows)


def get_chain_snapshot_map(symbol, at_or_before_ts):
    """Returns the most recent FULL snapshot at-or-before `at_or_before_ts`
    as {(strike, leg): {ltp, oi, oi_chg, volume, iv, delta, gamma,
    theta, vega, bid, ask}}, or {} if nothing's persisted yet for this
    symbol before that time (callers should treat that as "no
    comparison available yet", not an error — same graceful-degradation
    pattern as Market Breadth/Volume Profile elsewhere in this
    codebase). "Most recent full snapshot" means the latest single `ts`
    value at-or-before the cutoff — all rows sharing that ts, not a
    per-strike nearest-match, so the comparison is always against one
    consistent point in time rather than a blend of different moments."""
    c = _conn()
    row = c.execute(
        "SELECT MAX(ts) FROM chain_snapshots WHERE symbol=? AND ts<=?",
        (symbol, int(at_or_before_ts))).fetchone()
    snap_ts = row[0] if row else None
    if snap_ts is None:
        c.close()
        return {}
    rows = c.execute(
        """SELECT strike, leg, ltp, oi, oi_chg, volume, iv, delta, gamma,
                  theta, vega, bid, ask FROM chain_snapshots
           WHERE symbol=? AND ts=?""", (symbol, snap_ts)).fetchall()
    c.close()
    out = {}
    for r in rows:
        out[(r[0], r[1])] = {"ltp": r[2], "oi": r[3], "oi_chg": r[4],
                             "volume": r[5], "iv": r[6], "delta": r[7],
                             "gamma": r[8], "theta": r[9], "vega": r[10],
                             "bid": r[11], "ask": r[12]}
    return out


def get_chain_session_open_map(symbol, session_start_ts):
    """Same shape as get_chain_snapshot_map, but the EARLIEST snapshot
    at-or-after `session_start_ts` (i.e. today's first persisted
    snapshot) — for "change vs session open" rather than "change vs
    previous snapshot". Returns {} if today's first snapshot hasn't
    landed yet."""
    c = _conn()
    row = c.execute(
        "SELECT MIN(ts) FROM chain_snapshots WHERE symbol=? AND ts>=?",
        (symbol, int(session_start_ts))).fetchone()
    snap_ts = row[0] if row else None
    if snap_ts is None:
        c.close()
        return {}
    rows = c.execute(
        """SELECT strike, leg, ltp, oi, oi_chg, volume, iv, delta, gamma,
                  theta, vega, bid, ask FROM chain_snapshots
           WHERE symbol=? AND ts=?""", (symbol, snap_ts)).fetchall()
    c.close()
    out = {}
    for r in rows:
        out[(r[0], r[1])] = {"ltp": r[2], "oi": r[3], "oi_chg": r[4],
                             "volume": r[5], "iv": r[6], "delta": r[7],
                             "gamma": r[8], "theta": r[9], "vega": r[10],
                             "bid": r[11], "ask": r[12]}
    return out


def prune_chain_snapshots(days=5):
    """Routine retention — chain snapshots at 60s cadence add up fast
    (4 symbols x ~42 rows x 1440/min-per-day at 60s interval ≈ 240k
    rows/day). Keeps `days` days, same order of magnitude as the news-
    tracker retention pattern elsewhere in this codebase. Not called
    automatically by this module — a caller (e.g. a daily maintenance
    cycle) should invoke this periodically."""
    cutoff = int(time.time()) - days * 86400
    c = _conn()
    c.execute("DELETE FROM chain_snapshots WHERE ts < ?", (cutoff,))
    c.commit(); c.close()


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


def recent_sync_log(days=14):
    """Read back sync_log — written on every daily archive attempt but,
    until 2026-07-22, never read anywhere. This is the actual reason a
    symbol like SENSEX shows 0 chain days: the detail (probe failed /
    market closed / N legs failed) was persisted here the whole time,
    just invisible."""
    c = _conn()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute(
        "SELECT day, symbol, detail FROM sync_log WHERE day >= ? "
        "ORDER BY day DESC, symbol", (cutoff,)).fetchall()
    c.close()
    return [{"day": r[0], "symbol": r[1], "detail": r[2]} for r in rows]


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


def most_recent_session_candles(security_id):
    """Returns (ts,o,h,l,c) rows for the most recent IST calendar date
    that has ANY persisted data for this security_id — i.e. "today's
    candles if today has data, else the most recent prior trading
    day's full set". Mirrors the same fallback principle app.py's
    ws_candles endpoint already applies to REST/bus data
    (most_recent_session()), but sourced from the DB now that
    RegimeAgent persists 1m/5m/15m candles for all four symbols on
    every cycle — a faster, network-independent tier ahead of a live
    REST call. Returns [] if nothing has ever been persisted for this
    security_id."""
    c = _conn()
    row = c.execute(
        "SELECT MAX(date(ts,'unixepoch','+5 hours','+30 minutes')) "
        "FROM candles WHERE security_id=?", (security_id,)).fetchone()
    latest_date = row[0] if row else None
    if not latest_date:
        c.close()
        return []
    rows = c.execute(
        "SELECT ts,o,h,l,c FROM candles WHERE security_id=? "
        "AND date(ts,'unixepoch','+5 hours','+30 minutes')=? ORDER BY ts",
        (security_id, latest_date)).fetchall()
    c.close()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4]} for r in rows]


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
