#!/usr/bin/env python3
"""test_fee_floor.py — a trade must never be able to look free.

2026-08-06. Restating the journal found 184 of 300 records (61%)
charged ZERO fees, across six sessions:

    07-22 (11)  07-23 (33)  07-24 (47)  07-27 (47)  07-28 (16)  07-29 (30)

`fee_per_lot` had been saved as 0 from Settings. Nothing in the
codebase writes that key, so it was an operator action — and it
overstated every P&L figure, every Quality breakdown, and every
backtest that `is_live_enabled()` reads to decide whether a strategy
may trade real money.

A WARNING ALREADY EXISTED. `warn_zero_fees` fired once a day saying
exactly this, and the zero-cost trades ran for a week anyway. Warnings
are read after the fact. This file defends a FLOOR, which is not
skippable.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_fee_floor")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config

print("1) the config boundary refuses a free-trading fee")
check("fee_per_lot has a floor", "fee_per_lot" in config.FLOORS,
      f"FLOORS={config.FLOORS}")
saved = config.save({"fee_per_lot": 0})
check("saving 0 stores the floor instead",
      saved["fee_per_lot"] >= config.FLOORS["fee_per_lot"],
      f"stored {saved['fee_per_lot']}")
check("and it PERSISTS as the floor, not just in the return value",
      config.load()["fee_per_lot"] >= config.FLOORS["fee_per_lot"],
      str(config.load()["fee_per_lot"]))

print("\n2) a LEGITIMATE low value is still the operator's to choose")
config.save({"fee_per_lot": 25})
check("25 is preserved", config.load()["fee_per_lot"] == 25,
      "a floor that clamps every value would be a different bug — the "
      "operator may tune costs, they may not make trading free")
config.save({"fee_per_lot": 40})

print("\n3) negatives are caught too, not just zero")
config.save({"fee_per_lot": -100})
check("a negative fee cannot be stored",
      config.load()["fee_per_lot"] >= config.FLOORS["fee_per_lot"],
      f"{config.load()['fee_per_lot']} — a negative fee would PAY you "
      f"to trade")
config.save({"fee_per_lot": 40})

print("\n4) the cost fallback cannot return zero either")
# The floor stops the value being SAVED. This stops a hand-built or
# stale cfg dict reaching the fallback and charging nothing.
zero_cfg = {"fee_per_lot": 0, "lot_sizes": {"NIFTY": 65}}
f = agents.realistic_fees("option", "NIFTY", 1, None, None, zero_cfg)
check("a cfg with fee_per_lot=0 still charges something", f > 0, str(f))
check("and charges at least the shipped default",
      f >= config.DEFAULTS["fee_per_lot"] * 2,
      f"{f} — this path runs when the real model could not be computed, "
      f"which is the worst moment to understate")

print("\n5) the guard is a FLOOR, not another warning")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config.py")).read()
_code = [l for l in SRC.split("\n") if not l.strip().startswith("#")]
check("save() consults FLOORS", any("FLOORS.get(" in l for l in _code),
      "warn_zero_fees already warned about exactly this and 184 trades "
      "still recorded no cost — the warning was not the fix")
check("and it still says loudly what it did",
      any("_warn_floored" in l for l in _code),
      "clamping silently would trade one invisible problem for another")

print("\n6) the original warning is still there")
AG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
check("warn_zero_fees was not removed", "def warn_zero_fees(" in AG,
      "the floor prevents the config path; a zero fee arriving any "
      "other way should still be shouted about")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all fee-floor checks passed")
