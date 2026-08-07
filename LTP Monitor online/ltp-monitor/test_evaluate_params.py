#!/usr/bin/env python3
"""test_evaluate_params.py — a tuner that cannot relax is not a tuner.

2026-08-08. `strategies.evaluate()` took no params argument at all. It
re-read the PERSISTED version off disk every call, so
`backtester._eval_with_params()` — whose docstring promises it
"inject[s] tunable params into the real evaluator" — could only
re-apply the same two filters AFTERWARDS, i.e. tighten further.

A candidate that RELAXED wall_gap_frac was therefore still gated on the
incumbent's tighter value and could never show more trades than the
incumbent. The search ratchets one way. The persisted history proves it
happened: bear_call_spread/NIFTY went v1 (0.8/0.15) -> v10 (4.0/0.40)
and bull_put_spread/BANKNIFTY v1 -> v13 (4.0/0.40), and EVERY version
reason on the way reads "tightening". Not one relaxation in 23 versions.

Both pairs ended at the restrictive corner of SPREAD_BOUNDS producing
~0 trades, which the promotion gate reads as "no edge" when it means
"never evaluated".

Measured on NIFTY bear_call_spread, same frames and days:
    persisted 4.0/0.40  ->  0 of 60 eligible
    passed    1.5/0.25  ->  5 of 60 eligible
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_evaluate_params")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import inspect

import backtester as bt
import strategies as slib

HERE = os.path.dirname(os.path.abspath(__file__))

print("1) evaluate() accepts params")
sig = inspect.signature(slib.evaluate)
check("params is in the signature", "params" in sig.parameters)
check("and defaults to None, so every existing caller is unchanged",
      sig.parameters["params"].default is None,
      "the live agent (agents.py:5122) and both app.py endpoints pass "
      "no params and must keep reading the persisted version")

print("\n2) when params IS given, the persisted version is NOT consulted")
# The decisive check: make the disk read explode. If evaluate() still
# works, it genuinely used what the caller handed it.
_orig = bt.get_params
calls = []


def _boom(name, symbol=None):
    calls.append((name, symbol))
    raise AssertionError("get_params must not be called when params= is supplied")


analysis = {"spot": 24600.0, "symbol": "NIFTY",
            "strikes": [{"strike": 24600.0 + i * 50} for i in range(-6, 7)]}
regime = {"regime": "rangebound"}
P = {"wall_gap_frac": 1.5, "credit_min_frac": 0.25,
     "profit_capture": 0.18, "loss_mult": 1.0}

bt.get_params = _boom
try:
    slib.evaluate("bear_call_spread", analysis, regime, params=P)
    raised = False
except AssertionError:
    raised = True
except Exception:
    raised = False          # some other failure downstream is fine here
finally:
    bt.get_params = _orig
check("evaluate(params=...) never reads the persisted version",
      not raised and not calls,
      f"get_params calls: {calls} — a single call here means the "
      f"caller's parameters are still being overridden by disk")

print("\n3) params=None still DOES read it (live path intact)")
seen = []


def _spy(name, symbol=None):
    seen.append((name, symbol))
    return _orig(name, symbol)


bt.get_params = _spy
try:
    slib.evaluate("bear_call_spread", analysis, regime)
except Exception:
    pass
finally:
    bt.get_params = _orig
check("params=None consults get_params", bool(seen), f"{seen}")

print("\n4) the backtester actually passes them")
BT = open(os.path.join(HERE, "backtester.py")).read()
_code = [l for l in BT.split("\n") if not l.strip().startswith("#")]
body = BT.split("def _eval_with_params(")[1]
body = body[:body.index("\ndef ")]
check("_eval_with_params forwards params= to slib.evaluate",
      "params=p" in body,
      "without this the function's own docstring is false")

print("\n5) the SEED params are not the restrictive corner")
# Deliberately NOT asserting on the two real pairs. This runs against an
# isolated store with no strategy_versions.json, so get_params() would
# just mint a fresh entry from DEFAULT_PARAMS and the check would pass
# without ever seeing the persisted state it claims to test — a check
# that cannot fail is worse than no check. What IS meaningful here is
# that any NEW (symbol, strategy) entry starts away from the corner.
hi_w = slib.SPREAD_BOUNDS["bear_call_spread"]["wall_gap_frac"][1]
hi_c = slib.SPREAD_BOUNDS["bear_call_spread"]["credit_min_frac"][1]
for name, sym in (("bear_call_spread", "NIFTY"), ("bull_put_spread", "BANKNIFTY")):
    p = bt.get_params(name, sym)
    pinned = p["wall_gap_frac"] >= hi_w and p["credit_min_frac"] >= hi_c
    check(f"a fresh {sym} {name} entry is not seeded at ({hi_w}, {hi_c})",
          not pinned,
          f"wall_gap={p['wall_gap_frac']} credit_min={p['credit_min_frac']} "
          f"— the REAL pairs were reset out-of-band by "
          f"scratch/reset_pinned_params.py; that state lives in "
          f"~/.ltp-monitor and is not visible from an isolated test")

print("\n6) the defaults they were reset to are the documented ones")
for name in ("bear_call_spread", "bull_put_spread"):
    d = bt.DEFAULT_PARAMS[name]
    check(f"{name} default is not the permissive bound either",
          d["wall_gap_frac"] > slib.SPREAD_BOUNDS[name]["wall_gap_frac"][0],
          f"{d['wall_gap_frac']} — resetting to the bounds MINIMUM would "
          f"be choosing the value that generates the most trades, which "
          f"is the same mistake pointing the other way")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all evaluate-params checks passed")
