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
import store
import threading
import sqlite3
import time
from datetime import date, datetime, timedelta

DB = store.path("history.db")


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _conn():
    """Open a connection to the local store.

    2026-07-26 — rewritten after a live log showed a lock storm that had
    never appeared in the preceding 11 days of logs: 15 x "database is
    locked" across regime candle persistence, daily OHLC, chain
    snapshots, volume and the backtest agent, all within 3 minutes of a
    restart. Three compounding causes, all addressed here:

      1. This function ran ELEVEN `CREATE TABLE/INDEX IF NOT EXISTS`
         statements on EVERY connection — and connections are opened per
         operation, by ~14 agents, several times a minute. Even as
         no-ops those are schema statements on the write path. Now run
         once per process behind a lock.
      2. SQLite's default journal mode is `delete` (a rollback journal),
         under which a writer takes an EXCLUSIVE lock that blocks all
         readers, and any reader blocks the writer. WAL instead lets
         readers and one writer proceed concurrently — the correct mode
         for this workload (many small agent writes + increasingly large
         analytical reads now that ~2 years of candles are persisted).
      3. No `busy_timeout`, so a contended operation raised immediately
         instead of waiting. Now 30s, plus a 30s connect timeout.

    The trigger was a read-amplification regression in the chart
    websocket (see app._indicator_candles), which is fixed separately —
    but the underlying fragility was pre-existing and would have surfaced
    again as the candle table grew, so it is fixed at the source rather
    than only by backing out the reads.
    """
    global _SCHEMA_READY
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30.0)
    # Per-connection and cheap (no lock taken) — must be set every time.
    c.execute("PRAGMA busy_timeout=30000")
    if _SCHEMA_READY:
        return c
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return c
        # Persistent (stored in the DB header) so this only genuinely
        # changes anything on the first run after this upgrade.
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            # A non-WAL-capable filesystem (some network mounts) — keep
            # working in the default mode rather than failing to start.
            pass
        _ensure_schema(c)
        _SCHEMA_READY = True
    return c


def _ensure_schema(c):
    """All CREATE TABLE/INDEX statements. Called once per process from
    _conn() — previously inlined there and re-executed on every single
    connection (see the note above)."""
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
    # Feature #9 (Institutional Portfolio Risk Engine) — per the
    # spec's own explicit "Database: Store Risk Score, Trade Quality,
    # Liquidity Score, Greeks, Portfolio Greeks, Exposure, Reason,
    # Approval, Rejection, Timestamp" requirement. One row per risk
    # DECISION (i.e. once per RiskAgent.evaluate() call for a proposed
    # signal — NOT the ambient per-10s portfolio score, which lives
    # only on the bus since it's not tied to a specific approval/
    # rejection event worth permanently recording).
    c.execute("""CREATE TABLE IF NOT EXISTS risk_decisions(
        ts INTEGER, symbol TEXT, signal TEXT, verdict TEXT,
        risk_score REAL, risk_level TEXT, trade_quality_score REAL,
        liquidity_score REAL, portfolio_delta REAL, portfolio_gamma REAL,
        portfolio_theta REAL, portfolio_vega REAL, deployed_capital REAL,
        reason TEXT,
        PRIMARY KEY(ts, symbol))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_rd_symbol_ts ON risk_decisions(symbol, ts)")
    # IV Percentile/Rank long-window backfill — per direct user
    # question ("why can't we build a true 30-90 day IV Rank, we have
    # backtest data for 2 years"): the backtest replay pipeline
    # (day_chain_frames/chain_days below) can reconstruct a FULL
    # historical option chain from already-persisted candle data for
    # any day with coverage — a much richer source than the 5-day-
    # pruned chain_snapshots table IV percentile originally used.
    # Reconstruction is expensive (minute-by-minute chain rebuild +
    # a full analyze() call per day), so this table CACHES one
    # representative (near-EOD) ATM IV reading per historical day —
    # computed once by risk_engine.backfill_iv_history(), not on every
    # percentile lookup.
    # v58.32 — Strategy 9 calibration observations. Written EVERY agent
    # cycle regardless of auto_deploy, because the whole point is to
    # observe which of the seven confluence signals actually fire on
    # real market data BEFORE enabling the strategy. Replay against
    # synthetic days showed only one of seven ever triggering; whether
    # that holds on live NIFTY is the open question this table answers.
    c.execute("""CREATE TABLE IF NOT EXISTS ta_calibration(
        ts INTEGER, as_of INTEGER, day TEXT, symbol TEXT, strategy TEXT,
        phase TEXT, route TEXT, tide INTEGER, direction INTEGER,
        bb_state TEXT, gmma_state TEXT, adx REAL, dynamic INTEGER,
        macd_zero_reversal INTEGER, rsi REAL,
        sig_bb_stall INTEGER, sig_gmma INTEGER, sig_macd_zero INTEGER,
        sig_hidden_div INTEGER, sig_regular_div INTEGER,
        sig_rsi_div INTEGER, sig_adx INTEGER,
        confluence_hits INTEGER, confluence_need INTEGER,
        fired INTEGER, blocked TEXT,
        -- v58.41 — RAW inputs alongside the derived states. The first
        -- cut stored conclusions only, so a real session could show
        -- that bb_slope_eps was too high without showing what it
        -- should be, and could not diagnose the dead divergence
        -- signals at all (no pivot count). Thresholds set from a
        -- distribution beat thresholds set from a guess.
        bb_slope REAL, bb_width_pct REAL,
        gmma_spread REAL, gmma_thresh REAL, gmma_tf TEXT,
        pivots_5m INTEGER, pivot_lows INTEGER, pivot_highs INTEGER,
        -- Keyed on the CANDLE timestamp, not wall clock. Two things
        -- broke with a wall-clock key: every observation written inside
        -- the same second collapsed into one row (35 writes -> 1 row,
        -- caught in verification), and re-running over the same period
        -- duplicated rather than replaced. Candle time is also this
        -- project's standing rule for anchoring anything time-series.
        -- Natural consequence, and the right one: two agent cycles
        -- inside one 5m candle produce ONE observation, not two.
        PRIMARY KEY (as_of, symbol, strategy))""")
    c.execute("""CREATE INDEX IF NOT EXISTS idx_ta_cal_day
                 ON ta_calibration(day, symbol)""")
    # Existing installs predate the v58.41 raw-value columns. CREATE TABLE
    # IF NOT EXISTS cannot add them; ALTER TABLE can.
    # EVERY column, not just the v58.41 additions. Listing only the
    # newest ones assumes the rest are present, which is the same
    # assumption that caused the original bug -- and a table left
    # partial by anything else (an interrupted migration, a test) then
    # fails an INSERT with "has N columns but 34 values were supplied".
    # Idempotent, so listing them all costs nothing.
    _added = _migrate_columns(c, "ta_calibration", [
        ("ts", "INTEGER"), ("as_of", "INTEGER"), ("day", "TEXT"),
        ("symbol", "TEXT"), ("strategy", "TEXT"), ("phase", "TEXT"),
        ("route", "TEXT"), ("tide", "INTEGER"), ("direction", "INTEGER"),
        ("bb_state", "TEXT"), ("gmma_state", "TEXT"), ("adx", "REAL"),
        ("dynamic", "INTEGER"), ("macd_zero_reversal", "INTEGER"),
        ("rsi", "REAL"), ("sig_bb_stall", "INTEGER"), ("sig_gmma", "INTEGER"),
        ("sig_macd_zero", "INTEGER"), ("sig_hidden_div", "INTEGER"),
        ("sig_regular_div", "INTEGER"), ("sig_rsi_div", "INTEGER"),
        ("sig_adx", "INTEGER"), ("confluence_hits", "INTEGER"),
        ("confluence_need", "INTEGER"), ("fired", "INTEGER"),
        ("blocked", "TEXT"), ("bb_slope", "REAL"), ("bb_width_pct", "REAL"),
        ("gmma_spread", "REAL"), ("gmma_thresh", "REAL"), ("gmma_tf", "TEXT"),
        ("pivots_5m", "INTEGER"), ("pivot_lows", "INTEGER"),
        ("pivot_highs", "INTEGER")])
    if _added:
        print(f"[migrate] ta_calibration: added {', '.join(_added)}")
    # SQLite cannot add a PRIMARY KEY with ALTER TABLE, so a table that
    # lost its PK (or never had one) cannot be repaired by adding
    # columns -- and without the PK, INSERT OR REPLACE stops deduping,
    # so two agent cycles inside one candle write two rows instead of
    # one. The only fix is rebuild-and-copy. Cheap here: this is a
    # diagnostic table, a few hundred rows a day.
    try:
        _sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                         "AND name='ta_calibration'").fetchone()
        if _sql and "PRIMARY KEY" not in (_sql[0] or ""):
            _cols = [r[1] for r in c.execute("PRAGMA table_info(ta_calibration)")]
            _shared = ", ".join(_cols)
            c.execute("ALTER TABLE ta_calibration RENAME TO ta_calibration_old")
            c.execute("""CREATE TABLE ta_calibration(
                ts INTEGER, as_of INTEGER, day TEXT, symbol TEXT, strategy TEXT,
                phase TEXT, route TEXT, tide INTEGER, direction INTEGER,
                bb_state TEXT, gmma_state TEXT, adx REAL, dynamic INTEGER,
                macd_zero_reversal INTEGER, rsi REAL,
                sig_bb_stall INTEGER, sig_gmma INTEGER, sig_macd_zero INTEGER,
                sig_hidden_div INTEGER, sig_regular_div INTEGER,
                sig_rsi_div INTEGER, sig_adx INTEGER,
                confluence_hits INTEGER, confluence_need INTEGER,
                fired INTEGER, blocked TEXT,
                bb_slope REAL, bb_width_pct REAL, gmma_spread REAL,
                gmma_thresh REAL, gmma_tf TEXT,
                pivots_5m INTEGER, pivot_lows INTEGER, pivot_highs INTEGER,
                PRIMARY KEY (as_of, symbol, strategy))""")
            c.execute(f"INSERT OR REPLACE INTO ta_calibration ({_shared}) "
                      f"SELECT {_shared} FROM ta_calibration_old")
            c.execute("DROP TABLE ta_calibration_old")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_ta_cal_day
                         ON ta_calibration(day, symbol)""")
            print("[migrate] ta_calibration: rebuilt to restore the PRIMARY KEY "
                  "(dedupe was silently disabled without it)")
    except Exception as _me:
        print(f"[migrate] ta_calibration PK rebuild skipped: {_me}")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_atm_iv(
        symbol TEXT, date TEXT, atm_iv REAL,
        PRIMARY KEY(symbol, date))""")
    c.commit()


def upsert_daily_atm_iv(symbol, date_str, atm_iv):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO daily_atm_iv VALUES (?,?,?)",
             (symbol, date_str, atm_iv))
    c.commit()
    c.close()


def get_daily_atm_iv_history(symbol, since_date=None):
    """Cached long-window ATM IV series (from `daily_atm_iv`, populated
    by risk_engine.backfill_iv_history()) — oldest first. Returns a
    plain list of floats, same shape as get_iv_history() so callers
    (risk_engine.iv_percentile) can use either interchangeably."""
    c = _conn()
    if since_date:
        rows = c.execute(
            "SELECT atm_iv FROM daily_atm_iv WHERE symbol=? AND date>=? ORDER BY date",
            (symbol, since_date)).fetchall()
    else:
        rows = c.execute(
            "SELECT atm_iv FROM daily_atm_iv WHERE symbol=? ORDER BY date",
            (symbol,)).fetchall()
    c.close()
    return [r[0] for r in rows if r[0] is not None]


def insert_risk_decision(ts, symbol, signal, verdict, risk_score, risk_level,
                         trade_quality_score, liquidity_score, portfolio_greeks,
                         deployed_capital, reason):
    """Persists one risk-evaluation outcome — per explicit request,
    every approval AND rejection, not just approvals (so a later
    review can see what got turned away and why, not only what
    traded). `portfolio_greeks` is the dict aggregate_portfolio_greeks()
    already returns; unpacked into flat columns here for easy SQL
    querying later rather than stored as a JSON blob."""
    c = _conn()
    c.execute("""INSERT OR REPLACE INTO risk_decisions VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (int(ts), symbol, signal, verdict, risk_score, risk_level,
              trade_quality_score, liquidity_score,
              (portfolio_greeks or {}).get("delta"),
              (portfolio_greeks or {}).get("gamma"),
              (portfolio_greeks or {}).get("theta"),
              (portfolio_greeks or {}).get("vega"),
              deployed_capital, reason))
    c.commit()
    c.close()


def get_risk_decisions(symbol=None, since_ts=0, limit=200):
    """Retrieve recent risk decisions, optionally filtered by symbol —
    for a dashboard history view or later weekly-analytics work (not
    built this pass)."""
    c = _conn()
    if symbol:
        rows = c.execute(
            """SELECT * FROM risk_decisions WHERE symbol=? AND ts>=?
               ORDER BY ts DESC LIMIT ?""", (symbol, since_ts, limit)).fetchall()
    else:
        rows = c.execute(
            """SELECT * FROM risk_decisions WHERE ts>=?
               ORDER BY ts DESC LIMIT ?""", (since_ts, limit)).fetchall()
    cols = ["ts", "symbol", "signal", "verdict", "risk_score", "risk_level",
           "trade_quality_score", "liquidity_score", "portfolio_delta",
           "portfolio_gamma", "portfolio_theta", "portfolio_vega",
           "deployed_capital", "reason"]
    c.close()
    return [dict(zip(cols, r)) for r in rows]


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


def upsert_volume_history(security_id, ts, volume):
    """Persist a single per-minute traded-volume value into the EXISTING
    `candles` table (same security_id/ts convention as price candles,
    e.g. "NIFTY_SPOT_1m") — per explicit instruction to reuse existing
    infrastructure rather than a new table. Uses ON CONFLICT DO UPDATE
    (not a naive INSERT OR REPLACE) specifically so writing volume for
    a (security_id, ts) that ALREADY has a price candle only touches
    the `v` column — a naive REPLACE would silently null out that
    row's o/h/l/c/oi. Called from MarketDataAgent._build_volume_candle
    once per completed minute, not per tick."""
    c = _conn()
    c.execute("""INSERT INTO candles (security_id, ts, v) VALUES (?, ?, ?)
                ON CONFLICT(security_id, ts) DO UPDATE SET v=excluded.v""",
             (security_id, int(ts), volume))
    c.commit()
    c.close()


def get_volume_history(security_id, since_ts):
    """Retrieve persisted per-minute volume for a security_id from a
    given timestamp onward — used to attach volume to the chart's
    historical candle payload. Returns {ts: volume} for easy merging
    by timestamp against the price-candle series (which may not have
    volume recorded for every single bar — a real, disclosed gap when
    the futures quote poll and the price-candle builder's minute
    buckets don't align perfectly, rather than a fabricated fill)."""
    c = _conn()
    rows = c.execute(
        """SELECT ts, v FROM candles WHERE security_id=? AND ts>=?
           AND v IS NOT NULL ORDER BY ts""",
        (security_id, int(since_ts))).fetchall()
    c.close()
    return {r[0]: r[1] for r in rows}


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


def candles_since(security_id, since_ts, limit=500):
    """Persisted candles at or after `since_ts`, oldest-first, bounded.

    Added 2026-07-26 to replace a re-read of up to 2000 rows on every
    chart-indicator refresh cycle. Bars already held by the caller never
    change, so only the tail needs fetching — this keeps the chart's
    read volume proportional to what has actually appeared since the last
    cycle instead of to the size of the (now ~2 year) candle table.
    """
    if since_ts is None:
        return []
    c = _conn()
    rows = c.execute(
        """SELECT ts, o, h, l, c FROM candles
           WHERE security_id=? AND ts>=? AND c IS NOT NULL
           ORDER BY ts LIMIT ?""",
        (security_id, int(since_ts), int(limit))).fetchall()
    c.close()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4]} for r in rows]


def candles_before(security_id, before_ts, limit=400):
    """The last `limit` persisted candles STRICTLY BEFORE `before_ts`,
    returned oldest-first in the same dict shape the chart uses.

    Added 2026-07-26 for chart-indicator warm-up. The chart displays
    one session's bars, but EMA50 / MACD(26,9) / ADX / Supertrend all
    need a run-up of prior bars before their first output is
    meaningful -- and `_indicator_overlays()`/`_pane_series()` refuse
    to compute at all below 60 candles, which a session's first hour
    can't satisfy on its own. These prior-session bars are fed into the
    indicator math and then clipped back out of what's sent to the
    browser, so they warm the indicators up without extending the
    chart's visible time range.
    """
    if before_ts is None:
        return []
    c = _conn()
    rows = c.execute(
        """SELECT ts, o, h, l, c FROM candles
           WHERE security_id=? AND ts<? AND c IS NOT NULL
           ORDER BY ts DESC LIMIT ?""",
        (security_id, before_ts, int(limit))).fetchall()
    c.close()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4]} for r in reversed(rows)]


def get_iv_history(symbol, strike, leg, since_ts):
    """ATM (or any specific strike's) IV values over a time range —
    for IV Percentile/Rank calculation. Reuses the SAME `chain_
    snapshots` table Feature #4 already persists to every ~60s
    (`upsert_chain_snapshot`) — no new persistence pipeline. Returns a
    plain list of IV floats (None values excluded), oldest first.

    Honest limitation, stated here directly: `chain_snapshots` is
    pruned after 5 days (`prune_chain_snapshots`), so this can only
    ever support a genuine 5-DAY IV percentile, not the traditional
    30-90 day IV Rank options traders usually mean by that term. The
    caller (risk_engine.iv_percentile) labels its output accordingly
    rather than implying a longer lookback than the data can support."""
    c = _conn()
    rows = c.execute(
        """SELECT iv FROM chain_snapshots
           WHERE symbol=? AND strike=? AND leg=? AND ts>=? AND iv IS NOT NULL
           ORDER BY ts""",
        (symbol, strike, leg, int(since_ts))).fetchall()
    c.close()
    return [r[0] for r in rows]


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


def _migrate_columns(c, table, wanted):
    """Add any missing columns to an EXISTING table.

    2026-07-30 -- the bug this exists for. v58.41 added eight raw-value
    columns to `ta_calibration` by editing its `CREATE TABLE IF NOT
    EXISTS`. That statement does NOTHING when the table already exists,
    so every install created before v58.41 kept the 26-column schema
    while the summary query asked for 34. Result: OperationalError ->
    HTTP 500 -> the calibration panel showing
    "SyntaxError: Unexpected token 'I', \"Internal S\"..." because the
    frontend tried to JSON.parse an error page.

    It was invisible in development because a fresh DB gets the new
    schema, and I had dropped and recreated the table while testing --
    which is exactly the state a real user never has.

    `wanted` is [(name, decl), ...]. Idempotent: adding an existing
    column is skipped, so this can run on every startup.
    """
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if not have:
        return []          # table does not exist yet; CREATE will handle it
    added = []
    for name, decl in wanted:
        if name not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added



def log_future_oi(symbol, ts, oi, oi_chg, ltp, chg, quadrant):
    """Persist the futures OI quadrant so Strategy 10 can be backtested.

    2026-07-30 -- found while answering "can we test it using
    backtesting?". `chain_snapshots` already stores per-strike OI,
    oi_chg, volume and delta every 60s for 5 days, so the OPTION half of
    Strategy 10's trigger is fully replayable. Futures OI was computed
    live into `future_oi_quadrant:{sym}` on the bus and then thrown
    away, so the FUTURES half -- which is the first condition in the
    operator's rule -- could not be replayed at all.

    Half a trigger is not a backtest. Recording it costs one row per
    symbol per minute.
    """
    c = _conn()
    c.execute("""CREATE TABLE IF NOT EXISTS future_oi_snapshots(
        symbol TEXT, ts INTEGER, oi REAL, oi_chg REAL, ltp REAL, chg REAL,
        quadrant TEXT, PRIMARY KEY (symbol, ts))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_foi_sym_ts "
              "ON future_oi_snapshots(symbol, ts)")
    c.execute("INSERT OR REPLACE INTO future_oi_snapshots "
              "(symbol, ts, oi, oi_chg, ltp, chg, quadrant) "
              "VALUES (?,?,?,?,?,?,?)",
              (symbol.upper(), int(ts), oi, oi_chg, ltp, chg, quadrant))
    c.commit()
    c.close()


def future_oi_series(symbol, day):
    """Futures OI quadrant series for one day, oldest first."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT ts, oi, oi_chg, ltp, chg, quadrant FROM future_oi_snapshots "
            "WHERE symbol=? AND date(ts,'unixepoch','+5 hours','+30 minutes')=? "
            "ORDER BY ts", (symbol.upper(), day)).fetchall()
    except Exception:
        rows = []
    c.close()
    return [{"ts": r[0], "oi": r[1], "oi_chg": r[2], "ltp": r[3],
             "chg": r[4], "quadrant": r[5]} for r in rows]


def chain_series(symbol, day):
    """Per-minute chain snapshots for one day, grouped by timestamp.

    Returns [{"ts":..., "strikes":[{strike, ce:{...}, pe:{...}}, ...]}, ...]
    in the shape analyzer.analyze() produces, so a replay can feed
    oi_composite.detect_setup() the same structure it sees live.

    `chg` is DERIVED from the previous snapshot's ltp for the same
    strike/leg -- it is not stored, and the four-quadrant classifier
    needs it. At 60s cadence that is a genuine one-minute price change,
    which is what the live path computes too.
    """
    c = _conn()
    rows = c.execute(
        "SELECT ts, strike, leg, ltp, oi, oi_chg, volume, delta "
        "FROM chain_snapshots "
        "WHERE symbol=? AND date(ts,'unixepoch','+5 hours','+30 minutes')=? "
        "ORDER BY ts, strike", (symbol.upper(), day)).fetchall()
    c.close()
    by_ts = {}
    prev_ltp = {}
    for ts, strike, leg, ltp, oi, oi_chg, vol, delta in rows:
        snap = by_ts.setdefault(ts, {})
        row = snap.setdefault(strike, {"strike": strike})
        key = (strike, leg)
        prev = prev_ltp.get(key)
        leg_d = {"ltp": ltp, "oi": oi, "oi_chg": oi_chg, "volume": vol,
                 "delta": delta,
                 "chg": (ltp - prev) if prev is not None else 0.0}
        # 2026-07-30 -- the QUADRANT is not archived, only the raw inputs.
        # chain_snapshots has no `state` column, so a replay that merely
        # reproduced the row shape handed the detector state=None on
        # every leg and could only ever produce zero setups. It did:
        # 4,112 snapshots across 5 days, zero triggers, in the mode that
        # is supposed to OVERSTATE the count.
        #
        # Derived here with analyzer.classify_leg -- the SAME function
        # the live path uses, not a reimplementation -- so the replay
        # cannot drift from production. That is the whole point of a
        # backtest agreeing with the thing it backtests.
        try:
            import analyzer as _an
            st, churn = _an.classify_leg(leg_d)
            leg_d["state"] = st
            leg_d["churn"] = churn
        except Exception:
            leg_d["state"] = None
            leg_d["churn"] = False
        row[leg] = leg_d
        prev_ltp[key] = ltp
    out = []
    for ts in sorted(by_ts):
        strikes = [by_ts[ts][k] for k in sorted(by_ts[ts])]
        # Both legs must be present or the quadrant cannot be read.
        strikes = [s for s in strikes if "ce" in s and "pe" in s]
        if strikes:
            out.append({"ts": ts, "strikes": strikes})
    return out


def _now_ist_date():
    """IST calendar date as YYYY-MM-DD, for day-bucketing rows."""
    import datetime
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def log_ta_observation(symbol, strategy, state, conf, fired=False, blocked=None):
    """v58.32 — persist one Strategy 9 calibration observation.

    Called on EVERY agent cycle, including when the strategy cannot
    trade (auto_deploy off, position already open, cooldown). That is
    deliberate and is the entire point: before v58.32 the confluence
    breakdown was computed only INSIDE the auto_deploy gate, so running
    the system with auto_deploy off — which is how it ships and how
    anyone would sensibly observe a new strategy — captured nothing at
    all. The bus key it did set was in-memory and overwritten every
    cycle, so a full session left no record to calibrate against.

    Signal columns are stored as 0/1 rather than the mixed
    True/False/"skipped (...)" the conf dict carries, so the summary
    query below can just SUM() them.
    """
    def bit(k):
        return 1 if conf.get(k) is True else 0
    bb = (state.get("bb") or {})
    gm = (state.get("gmma") or {})
    hits = conf.get("count", "0/0")
    try:
        h, need = (int(x) for x in str(hits).split("/"))
    except Exception:
        h, need = 0, 0
    c = _conn()
    as_of = state.get("as_of")
    if as_of is None:
        as_of = int(time.time())
    raw = state.get("raw") or {}
    # NAMED columns, not positional. A positional INSERT assumes the
    # table's column ORDER, and ALTER TABLE appends -- so a migrated
    # table has the right columns in the wrong positions and every value
    # lands in the wrong field SILENTLY. That is strictly worse than the
    # crash it replaced: the 500 was visible, this would have quietly
    # written adx into confluence_hits and nobody would have known until
    # the calibration numbers were used to change a threshold.
    #
    # Found because a migrated table put confluence_hits at position 6.
    c.execute("""INSERT OR REPLACE INTO ta_calibration
                 (ts, as_of, day, symbol, strategy, phase, route, tide,
                  direction, bb_state, gmma_state, adx, dynamic,
                  macd_zero_reversal, rsi, sig_bb_stall, sig_gmma,
                  sig_macd_zero, sig_hidden_div, sig_regular_div,
                  sig_rsi_div, sig_adx, confluence_hits, confluence_need,
                  fired, blocked, bb_slope, bb_width_pct, gmma_spread,
                  gmma_thresh, gmma_tf, pivots_5m, pivot_lows, pivot_highs)
                 VALUES
                 (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                  ?,?,?,?,?,?,?,?)""", (
        int(time.time()), int(as_of), _now_ist_date(), symbol, strategy,
        state.get("phase"), state.get("route"), state.get("tide"),
        conf.get("_direction"),
        bb.get("state"), gm.get("state"), state.get("adx"),
        1 if state.get("dynamic") else 0,
        state.get("macd_zero_reversal"), state.get("rsi"),
        bit("bb_corrective_stall"), bit("gmma_expansion"),
        bit("macd_zero_reversal"), bit("hidden_divergence"),
        bit("regular_divergence"), bit("rsi_divergence"), bit("adx_dynamic"),
        h, need, 1 if fired else 0, blocked,
        raw.get("bb_slope"), raw.get("bb_width_pct"),
        raw.get("gmma_spread"), raw.get("gmma_thresh"), raw.get("gmma_tf"),
        raw.get("pivots_5m"), raw.get("pivot_lows"), raw.get("pivot_highs")))
    c.commit(); c.close()


def ta_calibration_summary(days=5, symbol=None):
    """Aggregated hit-rate per confluence signal — the actual
    calibration answer. "adx_dynamic fired on 71% of observations,
    gmma_expansion on 0%" is what tells you which thresholds are wrong;
    a pile of raw rows does not.
    """
    cutoff = int(time.time()) - days * 86400
    c = _conn()
    where = "WHERE ts >= ?" + (" AND symbol = ?" if symbol else "")
    args = (cutoff,) + ((symbol,) if symbol else ())
    row = c.execute(f"""SELECT COUNT(*),
        SUM(sig_bb_stall), SUM(sig_gmma), SUM(sig_macd_zero),
        SUM(sig_hidden_div), SUM(sig_regular_div), SUM(sig_rsi_div),
        SUM(sig_adx), SUM(fired),
        SUM(CASE WHEN phase='IMPULSE' THEN 1 ELSE 0 END),
        SUM(CASE WHEN phase='CORRECTIVE' THEN 1 ELSE 0 END),
        SUM(CASE WHEN phase='UNCLEAR' THEN 1 ELSE 0 END),
        SUM(CASE WHEN tide IS NULL THEN 1 ELSE 0 END),
        AVG(confluence_hits), MAX(confluence_hits)
        FROM ta_calibration {where}""", args).fetchone()
    dist = c.execute(f"""SELECT confluence_hits, COUNT(*)
        FROM ta_calibration {where} GROUP BY confluence_hits
        ORDER BY confluence_hits""", args).fetchall()
    c.close()
    n = row[0] or 0
    if not n:
        return {"observations": 0,
                "note": "no observations yet - run during market hours with "
                        "ta_elliott_enabled on (auto_deploy can stay off)"}
    names = ["bb_corrective_stall", "gmma_expansion", "macd_zero_reversal",
             "hidden_divergence", "regular_divergence", "rsi_divergence",
             "adx_dynamic"]
    # Distributions of the RAW inputs, so a threshold can be read off
    # the data instead of guessed. |bb_slope| percentiles say directly
    # what bb_slope_eps should be; pivot counts say whether the
    # divergence signals had anything to compare at all.
    c2 = _conn()
    try:
        raws = c2.execute(f"""SELECT ABS(bb_slope), bb_width_pct, gmma_spread,
        gmma_thresh, pivots_5m, pivot_lows, pivot_highs, gmma_tf
        FROM ta_calibration {where}""", args).fetchall()
    except Exception as _e:
        # A missing column must degrade to "no distributions", never a
        # 500 -- the panel has to render SOMETHING or the user sees a
        # JSON parse error instead of a diagnosis.
        raws = []
    c2.close()

    def _pct(vals, q):
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        return round(v[min(len(v) - 1, int(len(v) * q))], 8)

    cols = list(zip(*raws)) if raws else [()] * 8
    distributions = {
        "abs_bb_slope": {f"p{int(q*100)}": _pct(cols[0], q)
                         for q in (0.5, 0.75, 0.9, 0.95)},
        "bb_width_pct": {f"p{int(q*100)}": _pct(cols[1], q) for q in (0.1, 0.5, 0.9)},
        "gmma_spread": {f"p{int(q*100)}": _pct(cols[2], q) for q in (0.25, 0.5, 0.75)},
        "pivots_5m": {f"p{int(q*100)}": _pct(cols[4], q) for q in (0.1, 0.5, 0.9)},
        "pivot_lows": {f"p{int(q*100)}": _pct(cols[5], q) for q in (0.1, 0.5)},
        "pivot_highs": {f"p{int(q*100)}": _pct(cols[6], q) for q in (0.1, 0.5)},
        "gmma_computable_pct": (round(100.0 * sum(1 for x in cols[2] if x is not None)
                                      / max(len(cols[2]), 1), 1) if raws else None),
    }
    # Divergence needs TWO same-side pivots to compare. If most
    # observations never had two, no threshold on the oscillator can
    # make those three signals fire.
    if raws:
        two_lows = sum(1 for x in cols[5] if x is not None and x >= 2)
        two_highs = sum(1 for x in cols[6] if x is not None and x >= 2)
        distributions["obs_with_2plus_pivot_lows_pct"] = round(100.0 * two_lows / len(raws), 1)
        distributions["obs_with_2plus_pivot_highs_pct"] = round(100.0 * two_highs / len(raws), 1)
    return {
        "raw_distributions": distributions,
        "observations": n,
        "signal_hit_rate_pct": {nm: round(100.0 * (row[i + 1] or 0) / n, 1)
                                for i, nm in enumerate(names)},
        "signals_never_firing": [nm for i, nm in enumerate(names)
                                 if not (row[i + 1] or 0)],
        "phase_pct": {"IMPULSE": round(100.0 * (row[9] or 0) / n, 1),
                      "CORRECTIVE": round(100.0 * (row[10] or 0) / n, 1),
                      "UNCLEAR": round(100.0 * (row[11] or 0) / n, 1)},
        "tide_unavailable_pct": round(100.0 * (row[12] or 0) / n, 1),
        "confluence_avg": round(row[13] or 0, 2),
        "confluence_max": row[14] or 0,
        "confluence_distribution": {str(k): v for k, v in dist},
        "signals_fired": row[8] or 0,
    }


def prune_ta_calibration(days=10):
    """Retention. One row per symbol per 180s cycle across a 375-minute
    session is ~500 rows/day for 4 symbols - small, but unbounded
    without this."""
    cutoff = int(time.time()) - days * 86400
    c = _conn()
    c.execute("DELETE FROM ta_calibration WHERE ts < ?", (cutoff,))
    c.commit(); c.close()


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


DROPPED_OUT_OF_SESSION = {}   # security_id -> count dropped this process


def upsert_candles(security_id, candles, session_only=True):
    """Persist intraday candles, DROPPING out-of-session bars by default.

    2026-07-31 — this used to write whatever it was handed. Correctness
    depended on every read path filtering plus a manual prune, and that
    held for the 1m series (its producer was gated in the 2026-07-26
    keepalive fix) but never for the others:

        NIFTY_SPOT_15m   64% of rows out of session
        NIFTY_FUT_1m     57%
        NIFTY_SPOT_5m    40%

    13,577 rows database-wide, drawing flat evening/weekend bars on the
    chart. Gating each producer separately is what produced that split,
    so the gate belongs at the single write boundary they all share.

    Every current caller writes MINUTE data (sync_index_history fetches
    interval "1"; daily bars live in daily_ohlc), so in-session is the
    right invariant for all of them. `session_only=False` exists for a
    caller that genuinely needs to store out-of-hours bars — it must ask
    for it explicitly rather than getting it by default.

    Dropping silently would repeat this session's own recurring bug, so
    the first drop per security_id per process announces itself and the
    running total is readable in DROPPED_OUT_OF_SESSION.

    Returns the number of rows actually WRITTEN, not the number offered.
    """
    import agents
    rows = list(candles or [])
    if session_only:
        kept = [x for x in rows if agents.in_market_session(int(x["ts"]))]
        dropped = len(rows) - len(kept)
        if dropped:
            seen = DROPPED_OUT_OF_SESSION.get(security_id, 0)
            DROPPED_OUT_OF_SESSION[security_id] = seen + dropped
            if not seen:
                print(f"  [history] {security_id}: dropped {dropped} "
                      f"out-of-session candle(s) at write — keepalive "
                      f"contamination, not persisted")
        rows = kept
    if not rows:
        return 0
    c = _conn()
    c.executemany(
        "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
        [(security_id, int(x["ts"]), x.get("o"), x.get("h"), x.get("l"),
          x.get("c"), x.get("v"), x.get("oi")) for x in rows])
    c.commit(); c.close()
    return len(rows)


def upsert_instrument(security_id, symbol, kind, leg=None, strike=None, expiry=None):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?)",
              (str(security_id), symbol, kind, leg, strike, expiry))
    c.commit(); c.close()


def count_non_market_session_candles(batch_size=5000):
    """v58.9 — dry-run count for the pre-v50 weekend-keepalive candle
    prune (roadmap: "cosmetic, low priority... a one-time offline
    prune would reclaim space and remove the read-filter dependency").
    Deliberately a SEPARATE function from the actual prune below — a
    destructive operation on the persisted table should never run
    without the caller first seeing a real count to decide whether to
    proceed, matching the same "no silent number" discipline used
    throughout this project.

    Reads the ENTIRE table's (security_id, ts) pairs (this table has
    an index only on `ts`, not on the market-session predicate itself,
    since "is this timestamp inside trading hours" isn't expressible
    as a simple SQL range condition once IST conversion and weekday are
    involved — a full scan is unavoidable for an honest count, but it's
    read-only and one-time, not a recurring cost). Processed in
    batches via `fetchmany()` so the connection doesn't hold the
    entire multi-year table in memory at once.

    Returns {"total_rows": int, "non_market_session_rows": int,
    "by_security_id": {security_id: count, ...}}.
    """
    import agents
    c = _conn()
    cur = c.execute("SELECT security_id, ts FROM candles")
    total = 0
    stale = 0
    by_sec = {}
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        for sec_id, ts in rows:
            total += 1
            if not agents.in_market_session(ts):
                stale += 1
                by_sec[sec_id] = by_sec.get(sec_id, 0) + 1
    c.close()
    return {"total_rows": total, "non_market_session_rows": stale,
           "by_security_id": by_sec}


def prune_non_market_session_candles(dry_run=True, batch_size=5000, log=print):
    """v58.9 — the actual prune. Requires an EXPLICIT `dry_run=False`
    to delete anything (the safe default just calls the dry-run count
    above and returns without touching the table) — this is a
    destructive, one-time operation on persisted data, and this
    project has an established precedent of NOT doing this kind of
    thing casually: the read-side filter (`agents.in_market_session`,
    used by the indicator/chart paths since 2026-07-26) was explicitly
    chosen at the time specifically to avoid "a risky destructive prune
    of the persisted table" until it was actually wanted — this
    function is that deliberate, opt-in follow-through, not a silent
    default that could run unexpectedly.

    Deletes by PRIMARY KEY (security_id, ts) in batches rather than one
    giant DELETE statement, so a large prune doesn't hold an exclusive
    write lock against the many other agents that write to this same
    table for an extended stretch (this table has previously produced
    a real lock-storm incident — see `_conn()`'s own docstring history).

    Returns the SAME shape as count_non_market_session_candles(), plus
    "deleted": int and "dry_run": bool, so the caller can always see
    exactly what happened (or would have happened).
    """
    import agents
    counts = count_non_market_session_candles(batch_size)
    if dry_run:
        log(f"[prune] DRY RUN — would delete {counts['non_market_session_rows']} "
           f"of {counts['total_rows']} total candle rows "
           f"(outside NSE/BSE market-session hours) across "
           f"{len(counts['by_security_id'])} security_id(s). "
           f"Call with dry_run=False to actually delete.")
        return {**counts, "deleted": 0, "dry_run": True}

    c = _conn()
    # 2026-07-27 — read the FULL set of rows to delete before issuing
    # any DELETE at all, rather than interleaving writes with an open
    # read cursor on the same connection. This table has already
    # produced a real lock-storm incident once (see _conn()'s own
    # docstring) — mixing a live SELECT cursor with concurrent DELETEs
    # on the same connection risks exactly that class of problem again,
    # for no real benefit given the matching rows are a small fraction
    # of the whole table and fit comfortably in memory as a plain list.
    cur = c.execute("SELECT security_id, ts FROM candles")
    to_delete = []
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        for sec_id, ts in rows:
            if not agents.in_market_session(ts):
                to_delete.append((sec_id, ts))
    deleted = 0
    for i in range(0, len(to_delete), batch_size):
        chunk = to_delete[i:i + batch_size]
        c.executemany("DELETE FROM candles WHERE security_id=? AND ts=?", chunk)
        c.commit()
        deleted += len(chunk)
        log(f"[prune] deleted {deleted}/{len(to_delete)} rows so far...")
    c.close()
    log(f"[prune] done — deleted {deleted} non-market-session rows "
       f"({counts['total_rows'] - deleted} rows remain).")
    return {**counts, "deleted": deleted, "dry_run": False}


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
    """Summary for the dashboard: rows, span, chain-days per symbol.

    2026-07-27 — added `includes_today`: a live report ("current day
    data is not available even after backtest execution") turned out
    to be the two-button distinction most people wouldn't notice —
    "Run backtest" replays against whatever's already archived,
    "Sync + backtest" re-archives today's data FIRST. Clicking the
    first one when today hasn't been synced yet (e.g. by the automatic
    15:45 daily run) will never pick up today's data, silently. This
    flag lets the frontend say so explicitly rather than leaving the
    person to notice the date is missing from a dropdown themselves.
    """
    c = _conn()
    out = {}
    import agents as _agents
    today_str = _agents.now_ist().date().isoformat()
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
        opt_days = [r[0] for r in c.execute("""SELECT DISTINCT date(ts,'unixepoch') FROM candles
            WHERE security_id IN (SELECT security_id FROM instruments
                                  WHERE symbol=? AND kind='opt')""", (sym,)).fetchall()]
        out[sym] = {"index_candles": n, "span_days": span, "chain_days": len(opt_days),
                   "includes_today": today_str in opt_days}
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
    Returns sorted list of (ts, chain_dict) usable by analyzer.analyze.

    2026-07-26 — real gap found and fixed while wiring up
    risk_engine.backfill_iv_history(): this function's own reconstructed
    chain dict carried `"expiry": ""` (an empty-string placeholder,
    deliberate — backtester.py's existing replay strategies only need
    price movement to check SL/target hits, never real IV/greeks, so
    this was never needed before). `analyzer.analyze()` reads `chain.
    get("expiry")` to compute `days_to_expiry`, which its own Black-
    Scholes fallback requires to compute IV — an empty string is falsy,
    so that fallback silently never ran on any reconstructed historical
    frame, leaving `iv` stuck at 0. Fixed by surfacing the REAL expiry
    already sitting in the `instruments` table (just not previously
    selected here) — a genuine one-line data gap, not a deeper design
    problem, and now any consumer of this reconstruction (not just the
    IV backfill) gets working IV/greeks via the same fallback the live
    system already relies on."""
    c = _conn()
    insts = {r[0]: {"leg": r[1], "strike": r[2], "expiry": r[3]} for r in c.execute(
        "SELECT security_id,leg,strike,expiry FROM instruments WHERE symbol=? AND kind='opt'",
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
    day_expiry = next((m["expiry"] for m in insts.values() if m.get("expiry")), "")
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
                             "expiry": day_expiry, "rows": rows,
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
