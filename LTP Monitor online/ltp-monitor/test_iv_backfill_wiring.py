#!/usr/bin/env python3
"""test_iv_backfill_wiring.py — the ATM IV series must actually be built.

2026-08-08. `risk_engine.backfill_iv_history()` was written, correct in
its own terms, and called by NOTHING. `daily_atm_iv` was empty on every
symbol for the entire life of the feature, so agents.py's own IV-
percentile tier — which READS that table via
`history.get_daily_atm_iv_history()` — silently had no long-window
source, and the strategy-reset memo could not evaluate the volatility
risk premium at all.

This is the SAME failure `prune_chain_snapshots()` had at v53: a
maintenance function nobody invokes. `test_v53_hygiene.py` exists
because of that one. This file exists so the IV series cannot rot the
same way twice.

Wiring alone produced ZERO rows on all 40 available days. Three defects
sat underneath it, and each of them alone was enough to zero the output.
Every check below defends one of them.
"""
import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_iv_backfill_wiring")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import analyzer
import history
import risk_engine

HERE = os.path.dirname(os.path.abspath(__file__))
AG = open(os.path.join(HERE, "agents.py")).read()
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]

print("1) the EOD job actually CALLS it — the defect this file exists for")
check("LearningAgent invokes backfill_iv_history",
      any("backfill_iv_history(" in l for l in _code),
      "a maintenance function nobody calls is how daily_atm_iv stayed "
      "empty for the whole life of the feature")
check("it has its OWN once-per-day key, not the prune's",
      any('"iv_backfill_done"' in l for l in _code),
      "the prune block sets chain_prune_done mid-try, so sharing that "
      "key would let a prune failure skip the IV backfill forever")
check("and that key is distinct from chain_prune_done",
      any('bus.set("iv_backfill_done"' in l for l in _code) and
      any('bus.set("chain_prune_done"' in l for l in _code))
check("failure is logged, not swallowed",
      any("ATM IV backfill FAILED" in l for l in _code),
      "a bare `except: pass` here would recreate the silence exactly")

print("\n2) it walks the SAME symbol list every other agent walks")
# The near-miss worth defending: a module-level SYMBOLS constant does
# not exist in agents.py, so referencing one would raise NameError —
# caught by the block's own except and logged once a day forever.
check("agents.py has no module-level SYMBOLS to fall back to",
      not re.search(r"^SYMBOLS\s*=", AG, re.M),
      "if this ever gains one, the fallback question reopens")
_seg = AG.split("iv_backfill_done")[1][:900]
check("the backfill loop reads bus 'symbols'",
      'self.bus.get("symbols"' in _seg,
      "the list config.py's own comment calls the one that drives "
      "strategy and risk")

print("\n3) today is NEVER cached (defect: a mid-session read stored as EOD)")
src = open(os.path.join(HERE, "risk_engine.py")).read()
body = src.split("def backfill_iv_history(")[1]
body = body[:body.index("\ndef ")]
_b = [l for l in body.split("\n") if not l.strip().startswith("#")]
check("backfill_iv_history compares the day against today",
      any("day >= today" in l for l in _b),
      "chain_days() reads distinct dates out of `candles`, which is "
      "written live — on any trading day it returns today")
check("and reports the skip rather than hiding it",
      any("skipped_incomplete" in l for l in _b))
check("the result is upserted, which is WHY this matters",
      "upsert_daily_atm_iv" in body,
      "upsert + skip-if-cached means a wrong value written once is "
      "permanent and silent")

print("\n4) expiry-day readings are never cached (they are degenerate)")
check("days_to_expiry <= 1 is skipped",
      any("<= 1" in l and "skipped_expiry_day" not in l for l in _b) or
      any("skipped_expiry_day += 1" in l for l in _b),
      "measured: dte=1 gives 1.2 / 0.4 (NIFTY 07-28, 08-04) and 0.5 "
      "(FINNIFTY 07-28) against a ~10% norm")
check("and it is counted in the return value",
      "days_skipped_expiry_day" in body,
      "a silent skip is indistinguishable from no data")

print("\n5) analyze(as_of=) — historical frames must not count from today")
chain = {"symbol": "NIFTY", "spot": 24600.0, "expiry": "2026-08-11",
         "rows": [{"strike": 24600.0 + i * 50,
                   "ce": {"ltp": 100.0, "oi": 1, "oi_chg": 0, "volume": 1,
                          "iv": 0, "chg": 0, "bid": 0, "ask": 0},
                   "pe": {"ltp": 100.0, "oi": 1, "oi_chg": 0, "volume": 1,
                          "iv": 0, "chg": 0, "bid": 0, "ask": 0}}
                  for i in range(-5, 6)]}
check("analyze accepts as_of", "as_of" in analyzer.analyze.__code__.co_varnames)
today_str = date.today().strftime("%Y-%m-%d")
a_default = analyzer.analyze(chain)
a_today = analyzer.analyze(chain, as_of=today_str)
check("the DEFAULT is byte-identical to as_of=today",
      a_default == a_today,
      "every live caller must be unchanged — analyze() is the single "
      "source every strategy consumes")
a_bad = analyzer.analyze(chain, as_of="not-a-date")
check("a malformed as_of falls back to today, it does not crash",
      a_bad == a_today)

print("\n6) day_chain_frames(expiry=) filters, and DEFAULTS to legacy")
check("day_chain_frames takes an expiry argument",
      "expiry" in history.day_chain_frames.__code__.co_varnames)
import inspect
sig = inspect.signature(history.day_chain_frames)
check("and it defaults to None (backtester call sites untouched)",
      sig.parameters["expiry"].default is None,
      "backtester.py has three call sites whose replay results feed "
      "the promotion gate; changing them silently would move live "
      "enablement decisions")
check("front_expiry_on() exists to choose it",
      hasattr(history, "front_expiry_on"))
check("front_expiry_on picks the nearest expiry ON OR AFTER the day",
      True if not history.option_expiries("NIFTY") else
      history.front_expiry_on("NIFTY", "1990-01-01") ==
      history.option_expiries("NIFTY")[0],
      "an isolated store has no instruments, so this is vacuous here "
      "and meaningful against a real one")

print("\n7) the table this all feeds is still read the same way")
check("agents.py still reads get_daily_atm_iv_history",
      "get_daily_atm_iv_history(" in AG,
      "the consumer that had no producer")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all IV-backfill wiring checks passed")
