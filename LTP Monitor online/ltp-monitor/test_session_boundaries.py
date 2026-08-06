#!/usr/bin/env python3
"""test_session_boundaries.py — NSE split one boundary into two (2026-08-03).

From 3 Aug, index F&O trades until 15:40 while INTRADAY F&O positions are
auto-squared by the broker at 15:25. The code had ONE boundary at 15:30,
which is simultaneously too LATE to be flat and too EARLY to stop
collecting data.

The tests here pin ORDERING, not clock values, because the exchange just
demonstrated that clock values change. What must hold:

    open  <  squareoff  <  close  <  candle-gate tail
    trading stops at squareoff       (money)
    data continues to close          (evidence)

The asymmetry matters and is asserted directly: `market_open()` keeps its
original "may I be in a position" meaning and moves EARLIER, so any call
site nobody reviewed stands down early rather than holding past the
broker's square-off. Only data-path callers were promoted to
`fno_session_open()`.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_session_boundaries")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def at(h, m, day=3):
    return datetime.datetime(2026, 8, day, h, m, tzinfo=agents.IST)


def gates(h, m, day=3):
    """(may_trade, data_flowing, candle_kept) at a given IST clock time."""
    t = at(h, m, day)
    real = agents.now_ist
    agents.now_ist = lambda _t=t: _t
    try:
        return agents.market_open(), agents.fno_session_open(), \
            agents.in_market_session(t.timestamp())
    finally:
        agents.now_ist = real


SQ = agents._session_min("fno_squareoff_time", "15:22")
CL = agents._session_min("fno_close_time", "15:40")
OP = agents._session_min("fno_open_time", "09:15")

print("1) the three boundaries are ORDERED")
check("open < squareoff < close", OP < SQ < CL, f"{OP} < {SQ} < {CL}")
check("square-off leaves margin before the broker's 15:25",
      SQ <= 15 * 60 + 25 - 2,
      f"square-off {SQ//60:02d}:{SQ%60:02d} vs broker 15:25 — a late square-off "
      f"means the broker closes the position first, at its price not ours")
check("close matches the new F&O session", CL == 15 * 60 + 40,
      "index F&O trades to 15:40 from 2026-08-03")

print("\n2) trading STOPS at square-off, data CONTINUES to close")
for h, m, trade, data in ((9, 14, False, False), (9, 15, True, True),
                          (15, 22, True, True), (15, 23, False, True),
                          (15, 30, False, True), (15, 40, False, True),
                          (15, 41, False, False)):
    mo, fs, _ = gates(h, m)
    check(f"{h:02d}:{m:02d} trade={trade} data={data}",
          mo is trade and fs is data, f"got trade={mo} data={fs}")

print("\n3) the candle write gate keeps the FULL session")
_, _, k1530 = gates(15, 30)
_, _, k1539 = gates(15, 39)
check("bars at 15:30 are kept", k1530)
check("bars at 15:39 are kept", k1539,
      "the old 15:35 bound silently DISCARDED real F&O bars")
check("the gate outlasts the close (auction tail)",
      gates(15, 44)[2], "closing-auction prints still belong to the session")

print("\n4) weekends are still closed")
mo, fs, k = gates(12, 0, day=8)      # Saturday
check("Saturday: no trading, no data", not mo and not fs)

print("\n5) the risk asymmetry is deliberate")
check("market_open() closes EARLIER than the market does", SQ < CL,
      "an unreviewed call site stands down early rather than holding "
      "past the broker's square-off")
AG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]
# 2026-08-06 — the SPREAD square-off branch moved into the shared
# agents.spread_exit_reason(), which takes the gate as a PARAMETER so a
# replay can drive historical timestamps through identical code. The
# invariant is unchanged: both branches must be driven by the TRADING
# gate market_open(), never by fno_session_open(). Assert that at the
# branch AND at the call site that feeds it.
_sq = [l for l in _code if "elif not market_open():" in l]
_shared = AG.split("def spread_exit_reason(")[1]
_shared = _shared[:_shared.index("\ndef ")]
check("the shared spread exit squares off on the gate, not the data feed",
      "if not market_is_open:" in _shared
      and "fno_session_open" not in _shared,
      "the data gate runs LATER than the trading gate — using it here "
      "would hold spreads past the broker's square-off")
_mon = AG.split("    def _monitor_spreads(self")[1]
_mon = _mon[:_mon.index("\n    def ")]
check("and the LIVE call site passes market_open()",
      "market_open()" in _mon,
      "the parameter exists so replay can pass a historical value — "
      "live must still pass the real trading gate")
_bt = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "backtester.py")).read()
check("the replay passes its own frame-based gate, not market_open()",
      "market_is_open=" in _bt and "market_open()" not in _bt,
      "a replay calling the live clock would square off every historical "
      "spread the moment the test runs after 15:23")
check("the single-leg square-off branch still uses the trading gate", len(_sq) == 1,
      f"{len(_sq)} found — spreads and single-leg positions")
check("data path uses the session gate", "fno_session_open()" in AG)

print("\n6) no second definition of the boundary survives")
NM = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "news_macro_agent.py")).read()
check("news_macro_agent no longer hardcodes 15:30",
      "15 * 60 + 30" not in NM,
      "it held a byte-copy of the old rule and would have kept it")
check("it delegates to the shared definition",
      "_a.market_open()" in NM or "_session_min" in NM)

print("\n7) downstream constants follow the new close")
RE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "risk_engine.py")).read()
check("risk_engine session length is 385 min", "session_minutes = 385" in RE,
      "09:15-15:40 is 385 minutes, not 375")
FH = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fhedge_shadow.py")).read()
check("hedge EOD precedes the square-off",
      "EOD_HH, EOD_MM = 15, 18" in FH,
      "a hedge must be flat before the position it hedges")
check("the journal writes AFTER the close, not before",
      'fno_close_time", "15:40") + 5' in AG,
      "15:35 was before the 15:40 close — trades closing in that window "
      "would land after the day was written")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all session-boundary checks passed")
