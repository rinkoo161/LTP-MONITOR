#!/usr/bin/env python3
"""test_fill_geometry.py — v59.80, the fill-time geometry guard.

Reproduces the 2026-08-11 loss pair from the REAL logged signal and
asserts the guard now refuses it:

    11:39:29  BUY_PE 24450  entry ₹10.00  sl ₹4.00  t1 ₹22.00  conf 92
              filled at ₹43.45 -> target1 below entry -> t1_hit on cycle
              one -> breakeven lock pins the stop to entry -> next
              downtick closes it. mfe ₹0, loss ₹453. Twice. ₹856 total.

Every signal-side invariant passed that signal (60% stop is exactly the
bound; RR exactly 2.0) because they all check the signal against itself
— which is why the check has to run again against the fill.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_fill_geometry")

import agents
import analyzer
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


CFG = {**config.load(), "signal_entry_tolerance_pct": 10.0,
       "signal_entry_rescale_max_pct": 40.0}

# The real signal, verbatim from shadow_signals.jsonl.
REAL = {"signal": "BUY_PE", "strike": 24450.0, "entry": 10.0,
        "stoploss": 4.0, "target1": 22.0, "target2": None}

# --- the shared band rule ------------------------------------------------
v, g, d = analyzer.reprice_to_reference(REAL, 43.45, CFG)
check("the REAL 2026-08-11 signal is REJECTED against its actual fill",
      v == "reject", f"{v} — {d}")
check("rejection names the deviation, not just 'invalid'",
      "%" in d and "43.45" in d, d)

v, g, d = analyzer.reprice_to_reference(REAL, 10.5, CFG)
check("a 5% move leaves the geometry untouched",
      v == "ok" and g["stoploss"] == 4.0 and g["target1"] == 22.0, d)

v, g, d = analyzer.reprice_to_reference(REAL, 12.5, CFG)
check("a 20% move RESCALES rather than rejecting", v == "rescaled", d)
if v == "rescaled":
    rr_before = (REAL["target1"] - REAL["entry"]) / (REAL["entry"] - REAL["stoploss"])
    rr_after = (g["target1"] - g["entry"]) / (g["entry"] - g["stoploss"])
    check("the rescale preserves risk-reward exactly",
          abs(rr_before - rr_after) < 0.02,
          f"{rr_before:.2f} -> {rr_after:.2f}")
    check("and the rescaled target stays ABOVE the new entry",
          g["target1"] > g["entry"], f"t1 {g['target1']} vs entry {g['entry']}")

check("the input signal is never mutated",
      REAL["entry"] == 10.0 and REAL["stoploss"] == 4.0)
check("a missing reference is a SKIP, not a silent pass",
      analyzer.reprice_to_reference(REAL, None, CFG)[0] == "skip")
check("a zero entry is a SKIP",
      analyzer.reprice_to_reference({**REAL, "entry": 0}, 43.45, CFG)[0] == "skip")

# --- one definition, two call sites -------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
an_src = open(os.path.join(HERE, "analyzer.py")).read()
ag_src = open(os.path.join(HERE, "agents.py")).read()
check("the signal path uses the shared function",
      "reprice_to_reference(sig, _live_ltp, cfg)" in an_src)
check("the fill path uses the SAME shared function",
      "reprice_to_reference(\n                sig, fill, cfg)" in ag_src
      or "reprice_to_reference(sig, fill, cfg)" in ag_src.replace("\n", " ").replace("  ", " "))
check("the old inline band arithmetic is gone from the signal path",
      an_src.count("signal_entry_rescale_max_pct") == 1,
      f"{an_src.count('signal_entry_rescale_max_pct')} occurrences — "
      f"a second copy would drift")

# --- the guard is wired into _place(), ahead of the position build ------
def _method_body(src, name):
    """Slice a method by LINE structure, and assert it is non-trivial.

    Written after this test initially targeted `_enter` — which is an
    8-line dispatcher that just calls `place()`. The naive slice
    'succeeded', returned 375 characters, and every assertion below
    would have reported a missing guard as a code defect. The real
    entry path, and the guard, live in `_place()` — `place()` is
    itself only the re-entry-cooldown wrapper around it."""
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"    def {name}("))
    out = []
    for l in lines[start + 1:]:
        if l.startswith("    def ") or (l and not l[0].isspace()):
            break
        out.append(l)
    return "\n".join(out)


# The real entry work lives in _place(); place() is the cooldown
# wrapper around it. Guard belongs in _place, ahead of the fill.
_enter_src = _method_body(ag_src, "_place")
check("the _place() body was extracted (guards against a silent empty slice)",
      len(_enter_src) > 5000, f"{len(_enter_src)} chars")
check("_place() refuses on a rejected fill price",
      "fill-price geometry rejected" in _enter_src)
check("_place() refuses a fill already at/past target1",
      "already at target1" in _enter_src)
i_reprice = _enter_src.find("reprice_to_reference")
i_pos = _enter_src.find("pos = {")
check("both checks run BEFORE the position dict is built",
      0 < i_reprice < i_pos and 0 < _enter_src.find("already at target1") < i_pos,
      f"reprice@{i_reprice} target1@{_enter_src.find('already at target1')} pos@{i_pos}")
check("a failure of the check REFUSES rather than admitting the trade",
      "fill-price check failed" in _enter_src)

# --- the exit-side predicate is deliberately unchanged ------------------
# instant_exit_reason omits target1 on purpose (it is a ratchet trigger,
# not an exit). The fix must not have quietly changed that.
pos = {"ltp": 50.0, "leg": "CE", "stoploss": 10.0, "target2": 100.0,
       "t1_hit": False, "spot_invalidation": None}
check("instant_exit_reason still ignores target1 (unchanged behaviour)",
      agents.instant_exit_reason(pos, 50.0, None) is None)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all fill-geometry checks passed")
