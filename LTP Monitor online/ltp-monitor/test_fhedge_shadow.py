#!/usr/bin/env python3
"""test_fhedge_shadow.py — v59.0 Phase D.

A green test is NOT the deliverable here. The instruction was explicit:
instrument the invariant against real cycles and surface near-misses,
because a hedge that fails to unwind is a naked directional position on
a 15x instrument that nobody chose.

So this checks two different things:

  1. SAFETY — nothing in the shadow path can place an order, in live or
     in paper, and nothing in it can prevent a spread from exiting.
  2. THE INSTRUMENT ITSELF — that the journal would actually RECORD a
     violation if one occurred. A monitor that cannot express failure is
     worse than no monitor, because it reads as evidence of safety.

Point 2 is tested by feeding it a hedge that does NOT unwind and
confirming the report counts it, rather than only feeding it the happy
path.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_fhedge_shadow")

import fhedge_shadow as fh

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "fhedge_shadow.py")).read()
AG = open(os.path.join(HERE, "agents.py")).read()
OBS = AG.split("def _fhedge_observe")[1].split("\n    def ")[0]

print("1) SAFETY — this cannot trade")
for bad in ("place_order", "broker", "manual_trade", "enter_future",
            "exit_spread", "positions"):
    check(f"shadow module never calls {bad}", bad not in SRC)
check("observer never places or exits anything",
      not any(w in OBS for w in ("place_order", "enter_future", "exit_spread")),
      "it observes a spread; it must not act on one")
_call = AG.split("self._fhedge_observe(sp, sid")
check("observer is wrapped so it cannot block an exit",
      len(_call) > 1 and "try:" in _call[0][-300:]
      and "except Exception" in _call[1][:300],
      "an unhandled observer error would stop the spread from exiting")
check("exit_spread still runs after the observer",
      AG.split("self._fhedge_observe(sp, sid")[1].split("\n\n")[0].count("exit_spread") >= 1
      or "self.exit_spread(sid, reason)" in AG.split("_fhedge_observe")[1][:900],
      "an observer must never gate the parent's exit")
check("gated on its own switch", "fhedge_shadow_enabled" in OBS)

print("\n2) trigger geometry")
bull_put = {"symbol": "NIFTY", "strategy": "bull_put_spread", "width": 100,
            "short_strike": 24000, "qty": 75,
            "legs": [{"action": "SELL", "leg": "PE", "strike": 24000, "ltp": 120},
                     {"action": "BUY", "leg": "PE", "strike": 23900, "ltp": 80}]}
bear_call = dict(bull_put, strategy="bear_call_spread",
                 legs=[{"action": "SELL", "leg": "CE", "strike": 24000, "ltp": 120},
                       {"action": "BUY", "leg": "CE", "strike": 24100, "ltp": 80}])
check("bull put breaches when spot FALLS through the strike",
      fh.breach_points(bull_put, 23950) == 50)
check("bull put is not breached above the strike",
      fh.breach_points(bull_put, 24050) == -50)
check("bear call breaches when spot RISES through the strike",
      fh.breach_points(bear_call, 24050) == 50)
check("buffer scales with spread width, not an absolute constant",
      fh.buffer_points(bull_put) == fh.buffer_points(dict(bull_put, width=100))
      and fh.buffer_points(dict(bull_put, width=500)) >
          fh.buffer_points(bull_put),
      "a fixed buffer is noise on BANKNIFTY and a wall on NIFTY")

print("\n3) sizing refuses rather than guesses")
nd = fh.net_delta(bull_put, 23950, None, {}, dte=3)
check("net delta solves on real inputs", nd is not None, f"{nd}")
check("returns None when expiry is unknown",
      fh.net_delta(bull_put, 23950, None, {}, dte=None) is None,
      "sizing a 15x position off a guessed delta is the failure mode")
lots, capped, ratio = fh.hedge_lots(75 * 1.9, 75, {})
check("whole lots only, never rounded up", lots == 1 and not capped,
      f"1.9 lots of delta -> {lots}; rounding up would over-hedge")
check("over-hedge ratio recorded and <= 1 under flooring",
      ratio is not None and ratio <= 1.0, f"ratio {ratio}")
# The item-28 case: rounding 0.9 to NEAREST would open a full lot.
l09, _, r09 = fh.hedge_lots(75 * 0.9, 75, {})
check("0.9 lots floors to ZERO, not to one", l09 == 0,
      "rounding to nearest here opens 75 shares against 68 of delta")
lots2, capped2, _ = fh.hedge_lots(75 * 99, 75, {})
check("capped by fhedge_max_lots", lots2 == 2 and capped2, f"{lots2} {capped2}")
check("sub-lot delta hedges nothing", fh.hedge_lots(10, 75, {})[0] == 0)

# --- item 28: the parent must be big enough to hedge at all -----------
ok1, why1 = fh.parent_lots_ok(dict(bull_put, lots=1), {})
check("a ONE-lot parent cannot fire a hedge", not ok1, why1)
ok3, _ = fh.parent_lots_ok(dict(bull_put, lots=3), {})
check("a three-lot parent may", ok3)
check("the observer checks parent size BEFORE solving delta",
      OBS.index("parent_lots_ok") < OBS.index("net_delta("),
      "so the journal records the refusal instead of showing nothing")
check("the refusal is journalled, not silently returned",
      "trigger_blocked" in OBS.split("parent_lots_ok")[1][:400])
# A vertical's net delta peaks near the short strike at roughly 0.15-0.5
# per share and collapses toward 0 as the breach deepens (both legs go
# to delta 1). At ONE lot that peak is ~11-37 shares against a 75-share
# lot, so the hedge can never size to a whole lot. This is a real
# property of the instrument, not a bug — recorded here so a shadow
# journal showing few triggers is read correctly.
_1lot = fh.net_delta(bull_put, 23950, None, {}, dte=3)
check("a ONE-LOT spread cannot produce a whole hedge lot",
      fh.hedge_lots(_1lot, 75, {})[0] == 0,
      f"net delta {_1lot:.0f} shares vs 75/lot — hedging needs a multi-lot "
      f"spread or a deeper breach; expect few triggers at 1 lot")

print("\n4) would_unwind EXCLUDES the parent-closed rule")
check("parent close is not one of its reasons", "parent" not in SRC.split(
      "def would_unwind")[1].split("\ndef ")[0].replace("parent-closed rule", "")
      .replace("the parent's actual close", ""),
      "including it would make the near-miss measurement circular")
u, why, margin = fh.would_unwind(bull_put, 24050, {}, now=time.mktime(
    (2026, 8, 3, 11, 0, 0, 0, 0, -1)))
check("unwinds once the strike is reclaimed past the buffer", u, why)
u2, _, m2 = fh.would_unwind(bull_put, 23950, {}, now=time.mktime(
    (2026, 8, 3, 11, 0, 0, 0, 0, -1)))
check("does NOT unwind while still breached", not u2)
check("margin reports the gap to reclaiming", m2["reclaim_gap_pts"] > 0,
      f"{m2['reclaim_gap_pts']}pts still to go — this is the near-miss number")
u3, why3, _ = fh.would_unwind(bull_put, 23950, {}, now=time.mktime(
    (2026, 8, 3, 15, 30, 0, 0, 0, -1)))
check("unwinds at EOD regardless of breach", u3 and why3 == "eod")

print("\n5) THE INSTRUMENT CAN EXPRESS FAILURE")
# Feed it a violation. If the report cannot count this, then a clean
# report from live data proves nothing at all.
recs = [
    {"event": "parent_close", "ts": time.time(), "hedge_active": True,
     "independently_unwound": False, "hedge_closed_same_cycle": True,
     "margin": {"reclaim_gap_pts": 12.0, "minutes_to_eod": 90}},
    {"event": "parent_close", "ts": time.time(), "hedge_active": True,
     "independently_unwound": False, "hedge_closed_same_cycle": False,
     "margin": {"reclaim_gap_pts": 3.0, "minutes_to_eod": 40}},
    {"event": "parent_close", "ts": time.time(), "hedge_active": False,
     "independently_unwound": True, "margin": {"reclaim_gap_pts": -5.0}},
]
rep = fh.invariant_report(recs)
check("counts a real violation", rep["violations"] == 1, str(rep))
check("counts forced-only cycles (the load-bearing ones)",
      rep["forced_only"] == 2, str(rep))
check("surfaces the CLOSEST near-miss", rep["closest_near_miss_pts"] == 3.0)
check("surfaces the widest margin too", rep["widest_margin_pts"] == 12.0,
      "a wide margin means the forced close carried the whole invariant")
check("a clean journal reports zero, not None",
      fh.invariant_report([])["violations"] == 0)
# item 28 — the report must be able to SEE an over-hedge if flooring broke.
over = fh.invariant_report([
    {"event": "trigger", "ts": 1, "over_hedge_ratio": 3.0},
    {"event": "trigger", "ts": 1, "over_hedge_ratio": 0.5}])
check("an over-hedge is counted", over["over_hedged"] == 1, str(over))
check("the worst ratio is surfaced", over["max_over_hedge_ratio"] == 3.0)

print("\n6) journal write is fail-loud")
try:
    fh.write({"event": "x", "bad": object()})
    check("unserialisable entry raises", False)
except TypeError:
    check("unserialisable entry raises", True,
          "a silent drop would read as 'the hedge never triggered'")
r = fh.write({"event": "trigger", "sid": "zz"})
check("round-trips through the journal",
      any(x.get("sid") == "zz" for x in fh.read()), str(r))
check("tagged kind=fhedge", r["kind"] == "fhedge")
check("sessions are counted for the 40-session gate",
      fh.invariant_report()["sessions_required"] == 40)

print("\n7) futures switches stay OFF")
import config
check("futures_strategy_enabled still False",
      config.DEFAULTS.get("futures_strategy_enabled") is False)
check("futures_live_enabled still False",
      config.DEFAULTS.get("futures_live_enabled") is False)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all hedge-shadow checks passed")
