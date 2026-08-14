#!/usr/bin/env python3
"""test_target_reachability.py — v59.86.

`analyzer.option_stop_geometry` builds target1 as
`entry x (1 + stop_pct x 2)`, so target distance is welded to stop
width: a wider stop mechanically buys a more DISTANT target. Nothing
asked whether that target could actually be reached — the existing
feasibility gate only asks whether the designed edge clears its costs.

Measured over the resolved shadow journal (RR median 2.00 in every
bucket, so hit rates compare directly):

    move needed to reach T1     n    hit T1 first   E[R]
    <20%                      215       47.0%      +0.307
    20-40%                    144       22.9%      -0.398

The median signal needed 28.6% — inside the worst bucket.

Section 4 re-derives that relationship from the journal rather than
trusting the number above. A threshold whose evidence has eroded should
fail loudly, not sit in config looking authoritative.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_target_reachability")

import config
import edge_feasibility as EF

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


CFG = {"signal_max_target_move_pct": 20.0}

print("1) the gate admits reachable targets and refuses distant ones")
ok, why = EF.target_reachable(100, 115, CFG)
check("a 15% target is admitted", ok is True, why)
ok, why = EF.target_reachable(100, 120, CFG)
check("exactly at the cap is admitted", ok is True, why)
ok, why = EF.target_reachable(100, 135, CFG)
check("a 35% target is refused", ok is False, why)
check("the refusal states the number", "35.0%" in why and "20%" in why, why)

print("\n2) it fails OPEN on missing data and when disabled")
for args, lab in (((0, 135), "no entry"), ((100, 0), "no target")):
    ok, why = EF.target_reachable(*args, CFG)
    check(f"{lab} -> admitted, not silently blocked", ok is True, why)
ok, why = EF.target_reachable(100, 999, {"signal_max_target_move_pct": 0})
check("cap of 0 disables the check", ok is True, why)

print("\n3) it is operator-controlled and wired into the risk gate")
check("signal_max_target_move_pct is in config.DEFAULTS",
      "signal_max_target_move_pct" in config.DEFAULTS,
      "config.save() silently drops unregistered keys")
APP = open("app.py").read()
import re
_b = re.search(r"class SettingsIn\(BaseModel\):(.*?)\n\n\n", APP, re.S)
check("...and declared in SettingsIn",
      _b and "signal_max_target_move_pct:" in _b.group(1),
      "pydantic drops undeclared fields before config.save() sees them")
SRC = open("agents.py").read()
_i = SRC.find("target_reachable(")
_j = SRC.find("edge_feasibility.option_buy_feasible(")
check("the risk gate calls it", _i != -1)
check("it sits alongside the cost-feasibility gate, not somewhere else",
      _i != -1 and _j != -1 and abs(_i - _j) < 1200,
      "the two structural gates belong together")

print("\n4) the empirical basis still holds in the journal")
import json
p = os.path.expanduser("~/.ltp-monitor/shadow_signals.jsonl")
if not os.path.exists(p):
    print("  SKIP  no shadow journal on this host")
else:
    rows = [json.loads(l) for l in open(p) if l.strip()]
    res = [r for r in rows
           if r.get("signal") in ("BUY_CE", "BUY_PE")
           and r.get("resolution") in ("would_have_hit_target1",
                                       "would_have_hit_stoploss")
           and (r.get("entry") or 0) > 0 and (r.get("target1") or 0) > 0]
    cap = config.DEFAULTS["signal_max_target_move_pct"]

    def move(r):
        return (r["target1"] - r["entry"]) / r["entry"] * 100

    def won(r):
        return r["resolution"] == "would_have_hit_target1"

    near = [r for r in res if move(r) < cap]
    far = [r for r in res if cap <= move(r) < cap * 2]
    if len(near) < 30 or len(far) < 30:
        print(f"  SKIP  too few resolved signals (near={len(near)} far={len(far)})")
    else:
        pn = sum(won(r) for r in near) / len(near) * 100
        pf = sum(won(r) for r in far) / len(far) * 100
        check(f"targets under the {cap:.0f}% cap are reached MORE often "
              f"than those just beyond it",
              pn > pf,
              f"under {pn:.1f}% (n={len(near)}) vs just-beyond {pf:.1f}% "
              f"(n={len(far)}) — if this inverts, the cap has lost its basis")
        check("the gap is material, not a rounding artefact",
              (pn - pf) > 5.0, f"{pn - pf:+.1f} pp")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
