#!/usr/bin/env python3
"""test_chain_session_hygiene.py — v60.00.

Two related defects from the third third-eye review, plus the false
lead between them:

1. chain_snapshots was 50.6% out-of-session rows (670,824 of 1.3M,
   measured against the REAL session definition) because the writers
   throttled by interval but never asked what time it was. Fixed at the
   single shared write boundary (upsert_chain_snapshot), mirroring the
   upsert_candles gate, plus read-side filters for the pre-gate rows.

2. The EOD audit manufactured a live/replay divergence DAILY: it runs
   from LearningAgent (~15:35) but the day's option candles are synced
   by BacktestAgent (~15:45+), so it always replayed an empty day and
   reported every live trade as one "the rules wouldn't have made".
   The 2026-08-17 case: 0 replay trades at 15:48, 11 after the sync —
   and the live 12:34 entry MATCHED a rule-valid 12:31 setup.

The false lead worth recording: contamination was first blamed for the
audit flip. Falsification test 1 disproved it — the spread replay reads
option CANDLES, not chain_snapshots, so the filter changed nothing
there. Two independent defects, one symptom each; fixing only the first
would have left the audit lying every day with cleaner-looking inputs.
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_chain_session_hygiene")

import agents
import backtester
import history

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


IST = agents.IST


def ts_at(hour, minute, weekday_offset=0):
    """An epoch on the most recent weekday at hour:minute IST."""
    d = datetime.datetime.now(IST).replace(hour=hour, minute=minute,
                                           second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    d -= datetime.timedelta(days=7 * weekday_offset)
    return int(d.timestamp())


STRIKES = [{"strike": 100.0, "ce": {"ltp": 5.0, "oi": 10}, "pe": {"ltp": 4.0, "oi": 12}}]

print("1) the writer refuses out-of-session frames (same gate as candles)")
in_ts = ts_at(11, 0)
out_ts = ts_at(20, 0)
n_in = history.upsert_chain_snapshot("TESTSYM", in_ts, STRIKES)
n_out = history.upsert_chain_snapshot("TESTSYM", out_ts, STRIKES)
check("in-session write accepted", n_in == 2, f"wrote {n_in} rows")
check("out-of-session write refused", n_out == 0, f"wrote {n_out} rows")
check("the drop is COUNTED, not silent",
      history.DROPPED_OUT_OF_SESSION.get("chain:TESTSYM", 0) >= 1,
      "an invisible filter is a hidden sample-selection choice")
n_forced = history.upsert_chain_snapshot("TESTSYM", out_ts, STRIKES,
                                         session_only=False)
check("session_only=False still allows an explicit out-of-hours write",
      n_forced == 2, "escape hatch must be a request, never a default")

print("\n2) readers skip pre-gate contamination")
# Simulate the legacy state: force an evening row in, then ask for the
# latest snapshot as of late evening — the reader must hand back the
# 11:00 session frame, not the 20:00 junk (bare MAX(ts) returned the
# junk, which is why the EOD numbers moved after the close).
m = history.get_chain_snapshot_map("TESTSYM", ts_at(23, 0))
check("get_chain_snapshot_map returns a map", bool(m))
# distinguishable payloads: overwrite the evening row with a marker ltp
history.upsert_chain_snapshot(
    "TESTSYM", out_ts,
    [{"strike": 100.0, "ce": {"ltp": 999.0}, "pe": {"ltp": 999.0}}],
    session_only=False)
m2 = history.get_chain_snapshot_map("TESTSYM", ts_at(23, 0))
check("...and it is the IN-SESSION frame, not the later junk frame",
      m2.get((100.0, "ce"), {}).get("ltp") == 5.0,
      f"got {m2.get((100.0, 'ce'))}")
opened = history.get_chain_session_open_map("TESTSYM", ts_at(0, 1))
check("session-open reader also lands on the session frame",
      opened.get((100.0, "ce"), {}).get("ltp") == 5.0,
      f"got {opened.get((100.0, 'ce'))}")

print("\n3) one session definition, not a second SQL copy")
H = open("history.py").read()
i = H.index("def _chain_session_ts")
block = H[i:H.index("\ndef upsert_candles")]
check("chain filters delegate to agents.in_market_session",
      H.count("agents.in_market_session") >= 3,
      "the shared definition, lazily imported — never a parallel copy")
check("no hardcoded chain session boundary in history.py",
      "'09:15'" not in block and '"09:15"' not in block,
      "a SQL time-window here would be the third drifted-duplicate bug")

print("\n4) the audit refuses to fabricate a divergence from an empty day")
res = backtester.audit_today("bear_call_spread", "NOSYNCSYM",
                             real_trades=[{"closed_date": backtester._now_ist_date(),
                                           "symbol": "NOSYNCSYM",
                                           "strategy": "bear_call_spread",
                                           "pnl": -1000}])
check("unsynced day -> insufficient_data flag",
      res.get("insufficient_data") is True, str(res.get("gap_summary"))[:80])
check("no live trade is branded 'rules wouldn't have made'",
      res["unexpected_in_live"] == [],
      "that phrase was appearing DAILY for want of a sync, not a rule")
check("the summary names the cause and the remedy",
      "not yet synced" in res["gap_summary"])

print("\n5) cleanup")
c = history._conn()
c.execute("DELETE FROM chain_snapshots WHERE symbol='TESTSYM'")
c.commit(); c.close()
check("test rows removed", True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all chain-session-hygiene checks passed")
