#!/usr/bin/env python3
"""backfill_index_history.py — pull free index minute history from Dhan.

Answers the practical half of the research memo's Phase 0: the local
archive starts 2024-06-20, but Dhan itself serves 1-minute index candles
much further back (NIFTY from ~2017-04). This fetches what is free and
stores it so `seasonality_retro.py` can be re-run on years instead of
months.

    python3 tools/backfill_index_history.py                # all indices
    python3 tools/backfill_index_history.py --symbol NIFTY
    python3 tools/backfill_index_history.py --dry-run

WHY A SEPARATE DATABASE
-----------------------
This writes to ~/.ltp-monitor/research_history.db, NOT to history.db,
and that is deliberate rather than timid.

history.db is read by the live app, by every replay path, and by
`backtest_s10.py`. Adding nine years of candles under the same
security_ids would silently change what every one of those measures —
the PA replays would suddenly cover 2017-2023, the retention tiers in
CLAUDE.md (`chain_tier*`) were not written with that volume in mind, and
the golden replay's meaning would quietly shift. None of that is
necessarily wrong, but it is a decision about the production measurement
substrate and it belongs to the operator, not to a research fetch.

A separate file makes the whole thing reversible with `rm`. If the
history is later wanted in production, promoting it is a deliberate
second step.

PACING
------
2026-08-14: a credential-update refetch burst tripped Dhan's rate limit
and produced a 429 storm. This tool therefore sleeps between EVERY call,
requests the largest window the API will serve (90 days — 180 returns
HTTP 400), and is idempotent via INSERT OR IGNORE so an interrupted run
resumes instead of re-fetching. Roughly 115 calls covers every index
from its earliest available data to today.
"""
import argparse
import datetime
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DEFAULT_DB = os.path.expanduser("~/.ltp-monitor/research_history.db")

# Earliest date each index returns anything, established by probing on
# 2026-08-15 rather than assumed. NIFTY genuinely starts in April 2017;
# the other three return nothing for 2020 and full data for 2022, so
# they are started early enough to find the real boundary and the empty
# leading chunks are simply skipped and reported.
EARLIEST = {
    "NIFTY": "2017-01-01",
    "BANKNIFTY": "2020-01-01",
    "FINNIFTY": "2020-01-01",
    "SENSEX": "2020-01-01",
}
CHUNK_DAYS = 90          # 180 returns HTTP 400
SLEEP_SECONDS = 1.5      # deliberately above the documented ~1/s

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    security_id TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL, oi REAL,
    PRIMARY KEY (security_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_sid_ts ON candles(security_id, ts);
"""


def open_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def existing_span(conn, sid):
    row = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM candles WHERE security_id=?",
        (str(sid),)).fetchone()
    if not row or not row[0]:
        return 0, None, None
    return row[0], row[1], row[2]


def chunks(start, end, days):
    cur = start
    while cur < end:
        nxt = min(cur + datetime.timedelta(days=days), end)
        yield cur, nxt
        cur = nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--symbol", action="append",
                    help="repeatable; default is all four indices")
    ap.add_argument("--sleep", type=float, default=SLEEP_SECONDS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config
    import broker_adapter as BA
    cfg = config.load()
    tok = (cfg.get("dhan_access_token") or "").strip()
    if not tok:
        print("no Dhan access token in config — nothing to fetch",
              file=sys.stderr)
        return 2
    client = BA.DhanClient(cfg.get("dhan_client_id"), tok)

    symbols = args.symbol or list(EARLIEST)
    today = datetime.date.today()

    print(f"target db : {args.db}")
    print(f"pacing    : {args.sleep}s between calls, {CHUNK_DAYS}-day windows")
    print(f"symbols   : {', '.join(symbols)}\n")

    if args.dry_run:
        for sym in symbols:
            start = datetime.date.fromisoformat(EARLIEST[sym])
            n = len(list(chunks(start, today, CHUNK_DAYS)))
            print(f"  {sym:<10} {start} .. {today}   {n} calls")
        total = sum(len(list(chunks(datetime.date.fromisoformat(EARLIEST[s]),
                                    today, CHUNK_DAYS))) for s in symbols)
        print(f"\n  {total} calls, ~{total*args.sleep/60:.1f} min at this pacing")
        return 0

    conn = open_db(args.db)
    grand_new = 0

    for sym in symbols:
        sid = BA.UNDERLYINGS[sym]
        start = datetime.date.fromisoformat(EARLIEST[sym])
        before_n, before_lo, _ = existing_span(conn, sid)
        print(f"{'-'*74}\n{sym} (sid {sid}) — {before_n:,} rows already stored")

        # Resume from what is already there rather than re-fetching it.
        if before_lo:
            have_max = conn.execute(
                "SELECT MAX(ts) FROM candles WHERE security_id=?",
                (str(sid),)).fetchone()[0]
            resume = datetime.datetime.fromtimestamp(have_max, IST).date()
            if resume > start:
                # Re-fetch the final stored day so a partially-fetched
                # day cannot leave a permanent hole; INSERT OR IGNORE
                # makes the overlap free.
                start = resume
                print(f"  resuming from {start} (already have earlier data)")

        empty_streak = 0
        new_rows = 0
        calls = 0
        for a, b in chunks(start, today, CHUNK_DAYS):
            try:
                rows = client._intraday_range(sym, "1", a.isoformat(),
                                              b.isoformat())
            except Exception as e:
                msg = str(e)[:100]
                print(f"  {a} .. {b}  FAILED  {type(e).__name__}: {msg}")
                # A 429 means back off hard rather than continue politely.
                if "429" in msg:
                    print("  rate limited — stopping this symbol. Re-run "
                          "later; the fetch resumes where it stopped.")
                    break
                time.sleep(args.sleep * 2)
                continue
            calls += 1
            if not rows:
                empty_streak += 1
                # Leading empties are simply "before this index existed".
                if empty_streak >= 8 and new_rows == 0:
                    print(f"  {a} .. {b}  empty x{empty_streak} — no data "
                          f"this far back, skipping ahead")
                time.sleep(args.sleep)
                continue
            empty_streak = 0
            cur = conn.executemany(
                "INSERT OR IGNORE INTO candles"
                " (security_id, ts, o, h, l, c, v, oi)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(str(sid), r["ts"], r["o"], r["h"], r["l"], r["c"],
                  r.get("v"), r.get("oi")) for r in rows])
            conn.commit()
            new_rows += cur.rowcount
            print(f"  {a} .. {b}  fetched {len(rows):>6,}  "
                  f"new {cur.rowcount:>6,}")
            time.sleep(args.sleep)

        n, lo, hi = existing_span(conn, sid)
        span = ""
        if lo:
            span = (f"{datetime.datetime.fromtimestamp(lo, IST).date()} .. "
                    f"{datetime.datetime.fromtimestamp(hi, IST).date()}")
        print(f"  {sym}: {calls} calls, +{new_rows:,} new -> "
              f"{n:,} rows  {span}")
        grand_new += new_rows

    print(f"\n{'='*74}\n+{grand_new:,} rows into {args.db}")
    print("production history.db was NOT touched. Re-run the analysis with:")
    print(f"  python3 tools/seasonality_retro.py --db {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
