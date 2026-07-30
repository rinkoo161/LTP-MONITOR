"""Reproduces the 2026-07-26 "database is locked" storm and verifies the
fix, by running the same shape of concurrent load the live system does.

From the live activity.log: 15 x "database is locked" inside 3 minutes of
a restart, hitting regime candle persistence, daily OHLC, chain
snapshots, volume and the backtest agent. Absent in the preceding 11
days of logs -- introduced by a read-amplification regression in the
chart websocket's indicator path on top of a pre-existing fragility
(11 CREATE TABLE statements per connection, rollback journal, no
busy_timeout).

This spawns writers (the agents) alongside heavy readers (the chart) and
counts OperationalErrors.

Run:  python3 test_db_concurrency.py
"""
import os
import sys
import sqlite3
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history

SYM = "CONCTEST"
ERRORS = []
STOP = threading.Event()


def seed(n=6000):
    """A candle table of realistic size -- the user confirmed ~2 years of
    persisted history, which is what makes the wide reads expensive."""
    base = 1700000000
    rows = [{"ts": base + i * 60, "o": 100.0 + i % 50, "h": 101.0 + i % 50,
             "l": 99.0 + i % 50, "c": 100.5 + i % 50, "v": None, "oi": None}
            for i in range(n)]
    history.upsert_candles(f"{SYM}_SPOT_1m", rows)
    return base, n


def writer(tag, fn, every=0.05):
    while not STOP.is_set():
        try:
            fn()
        except sqlite3.OperationalError as e:
            ERRORS.append((tag, str(e)))
        except Exception as e:
            ERRORS.append((tag, f"{type(e).__name__}: {e}"))
        time.sleep(every)


def reader(tag, fn, every=0.02):
    while not STOP.is_set():
        try:
            fn()
        except sqlite3.OperationalError as e:
            ERRORS.append((tag, str(e)))
        except Exception as e:
            ERRORS.append((tag, f"{type(e).__name__}: {e}"))
        time.sleep(every)


def main():
    base, n = seed()
    print(f"seeded {n} candles for {SYM}")
    print(f"journal_mode = "
          f"{history._conn().execute('PRAGMA journal_mode').fetchone()[0]}")

    day = 0

    def w_candles():
        history.upsert_index_candles(
            SYM, [{"time": base + 60 * (n + 1), "open": 1, "high": 2,
                   "low": 0, "close": 1}], 1)

    def w_daily():
        nonlocal day
        day += 1
        history.upsert_daily_ohlc(SYM, f"2026-01-{(day % 28) + 1:02d}",
                                  1, 2, 0, 1)

    def w_chain():
        history.upsert_chain_snapshot(SYM, int(time.time()), [
            {"strike": 100 + i, "ce": {"ltp": 1, "oi": 2}, "pe": {"ltp": 1, "oi": 2}}
            for i in range(10)])

    def w_volume():
        history.upsert_volume_history(f"{SYM}_FUT_1m", int(time.time()), 123)

    # The chart's reads -- the amplification that tipped this over.
    def r_wide():
        history.candles_before(f"{SYM}_SPOT_1m", base + 60 * n, 400)

    def r_since():
        history.candles_since(f"{SYM}_SPOT_1m", base + 60 * (n - 50), 500)

    def r_session():
        history.most_recent_session_candles(f"{SYM}_SPOT_1m")

    threads = [
        threading.Thread(target=writer, args=("regime.candles", w_candles), daemon=True),
        threading.Thread(target=writer, args=("regime.daily_ohlc", w_daily), daemon=True),
        threading.Thread(target=writer, args=("technical.chain", w_chain), daemon=True),
        threading.Thread(target=writer, args=("market_data.volume", w_volume), daemon=True),
        threading.Thread(target=reader, args=("chart.warm", r_wide), daemon=True),
        threading.Thread(target=reader, args=("chart.since", r_since), daemon=True),
        threading.Thread(target=reader, args=("chart.session", r_session), daemon=True),
    ]
    for t in threads:
        t.start()
    dur = 12
    print(f"running {len(threads)} concurrent workers for {dur}s "
          f"(4 writers + 3 readers, mirroring the live agent mix)...")
    time.sleep(dur)
    STOP.set()
    for t in threads:
        t.join(timeout=5)

    locked = [e for e in ERRORS if "locked" in e[1]]
    print(f"\ntotal errors: {len(ERRORS)}   'database is locked': {len(locked)}")
    if ERRORS:
        seen = {}
        for tag, msg in ERRORS:
            seen.setdefault((tag, msg[:60]), 0)
            seen[(tag, msg[:60])] += 1
        for (tag, msg), cnt in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"   {cnt:4d}  {tag}: {msg}")

    # cleanup
    c = history._conn()
    c.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
    c.execute("DELETE FROM daily_ohlc WHERE symbol=?", (SYM,))
    c.execute("DELETE FROM chain_snapshots WHERE symbol=?", (SYM,))
    for tbl, col in (("volume_hist", "security_id"), ("volume", "security_id")):
        try:
            c.execute(f"DELETE FROM {tbl} WHERE {col} LIKE ?", (f"{SYM}%",))
        except sqlite3.OperationalError:
            pass
    c.commit()
    c.close()

    print("\n" + "=" * 60)
    if locked:
        print(f"FAIL — {len(locked)} lock error(s) under concurrent load")
        return 1
    if ERRORS:
        print(f"FAIL — {len(ERRORS)} non-lock error(s); see above")
        return 1
    print("PASS — no lock errors under concurrent writer/reader load")
    return 0


if __name__ == "__main__" and "--ab" not in sys.argv:
    sys.exit(main())


def ab_compare():
    """A/B proof that the fix is what matters, not that the load is easy.

    Runs the SAME concurrent load against a scratch DB configured the way
    _conn() used to be — rollback journal, no busy_timeout, and all 11
    CREATE TABLE statements re-executed on every connection — versus the
    new configuration. Uses a separate file because journal_mode is
    persistent in the DB header, so the real store cannot be flipped back.
    """
    import tempfile
    results = {}
    for label, wal, timeout_s, ddl_every_time in (
            ("OLD (rollback journal, no busy_timeout, DDL per connect)",
             False, 0.0, True),
            ("NEW (WAL, busy_timeout=30s, one-time schema)",
             True, 30.0, False)):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        boot = sqlite3.connect(path)
        boot.execute("""CREATE TABLE candles(security_id TEXT, ts INTEGER,
            o REAL, h REAL, l REAL, c REAL, v REAL, oi REAL,
            PRIMARY KEY(security_id, ts))""")
        if wal:
            boot.execute("PRAGMA journal_mode=WAL")
        rows = [(f"{SYM}_SPOT_1m", 1700000000 + i * 60, 1.0, 2.0, 0.0, 1.5,
                 None, None) for i in range(6000)]
        boot.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
        boot.commit(); boot.close()

        def conn():
            c = sqlite3.connect(path, timeout=timeout_s)
            if timeout_s:
                c.execute("PRAGMA busy_timeout=30000")
            if ddl_every_time:
                for i in range(11):
                    c.execute(f"CREATE TABLE IF NOT EXISTS pad{i}(a TEXT)")
            return c

        errs = []
        stop = threading.Event()

        def w():
            i = 0
            while not stop.is_set():
                i += 1
                try:
                    c = conn()
                    c.execute("INSERT OR REPLACE INTO candles VALUES "
                              "(?,?,?,?,?,?,?,?)",
                              (f"{SYM}_SPOT_1m", 1800000000 + i, 1.0, 2.0,
                               0.0, 1.5, None, None))
                    c.commit(); c.close()
                except Exception as e:
                    errs.append(str(e))
                time.sleep(0.01)

        def r():
            while not stop.is_set():
                try:
                    c = conn()
                    c.execute("""SELECT ts,o,h,l,c FROM candles
                                 WHERE security_id=? ORDER BY ts DESC
                                 LIMIT 400""", (f"{SYM}_SPOT_1m",)).fetchall()
                    c.close()
                except Exception as e:
                    errs.append(str(e))
                time.sleep(0.005)

        ts = [threading.Thread(target=w, daemon=True) for _ in range(4)] + \
             [threading.Thread(target=r, daemon=True) for _ in range(3)]
        for t in ts:
            t.start()
        time.sleep(8)
        stop.set()
        for t in ts:
            t.join(timeout=5)
        locked = [e for e in errs if "locked" in e]
        results[label] = (len(errs), len(locked))
        print(f"  {label}\n      errors={len(errs)}  locked={len(locked)}")
    return results


if __name__ == "__main__" and "--ab" in sys.argv:
    print("A/B: same load, old vs new connection configuration\n")
    ab_compare()
