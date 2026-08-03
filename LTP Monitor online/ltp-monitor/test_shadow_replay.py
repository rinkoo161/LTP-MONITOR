#!/usr/bin/env python3
"""test_shadow_replay.py — the first-touch rule the B2 conclusion rests on.

`shadow_replay.first_touch` is the entire basis for "the risk gates were
right to reject" (v59.6). If its ordering or its ambiguous-case rule is
wrong, the answer flips. So the rule is pinned here rather than trusted:

    walk forward in TIME; whichever level is crossed FIRST decides
    a snapshot past BOTH levels is charged as a STOP
    reaching neither by the horizon is OPEN, marked to the last price

The spanning rule matters because it is the conservative direction — it
charges the ambiguous case against the trade — and because it keeps this
comparable with the v59.0 futures first-touch grid, which used the same
convention. Silently flipping it would make an old result and a new one
look comparable when they are not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_shadow_replay")

import shadow_replay as sr

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def path(*ltps, step=60):
    return [(1000 + i * step, v, v) for i, v in enumerate(ltps)]


ENTRY, STOP, TARGET = 100.0, 90.0, 120.0     # risk 10, reward 20 => 2R

print("1) whichever level comes FIRST in time decides")
oc, R, _ = sr.first_touch(path(100, 105, 121, 89), ENTRY, STOP, TARGET)
check("target first -> target", oc == "target" and abs(R - 2.0) < 1e-9,
      f"{oc} {R}")
oc, R, _ = sr.first_touch(path(100, 95, 89, 121), ENTRY, STOP, TARGET)
check("stop first -> stop, even though target came later",
      oc == "stop" and R == -1.0,
      "this is the ordering the whole result depends on")

print("\n2) malformed level ordering is EXCLUDED, not scored")
# 22 rejected signals on disk have target1 <= entry, one with the target
# below its own stop. Before the guard, the first snapshot satisfied
# `ltp >= target` and the trade was labelled "target" — which the caller
# counts as a WIN. This check found that and the headline win rate moved.
oc, R, _ = sr.first_touch(path(100, 95), ENTRY, STOP, 94.0)
check("target below entry is excluded rather than counted as a win",
      oc is None, f"{oc} {R}")
oc, _, _ = sr.first_touch(path(345.4, 300.0), 345.4, 297.9, 254.1)
check("the real malformed signal from the journal is excluded",
      oc is None, "entry 345.4 / stop 297.9 / target 254.1")

print("\n3) neither level reached -> OPEN, marked to last")
oc, R, _ = sr.first_touch(path(100, 103, 108), ENTRY, STOP, TARGET)
check("outcome is open", oc == "open")
check("and marked at the last price, not at zero",
      abs(R - 0.8) < 1e-9, f"R={R} (108 vs entry 100, risk 10)")
check("an open trade is NOT scored as a win",
      oc != "target", "48% of the replay ends open; calling those wins "
                      "would invert the conclusion")

print("\n4) degenerate input returns nothing rather than guessing")
check("empty path", sr.first_touch([], ENTRY, STOP, TARGET)[0] is None)
check("stop at/above entry", sr.first_touch(path(100), 100.0, 100.0,
                                            120.0)[0] is None,
      "risk would be zero or negative — R is undefined, not infinite")

print("\n5) R is measured against the trade's OWN risk")
oc, R, _ = sr.first_touch(path(100, 130), 100.0, 80.0, 130.0)
check("risk 20, reward 30 -> +1.5R", oc == "target" and abs(R - 1.5) < 1e-9,
      f"R={R}")
oc, R, mins = sr.first_touch(path(100, 100, 121), ENTRY, STOP, TARGET)
check("minutes-to-touch is measured from the first snapshot",
      abs(mins - 2.0) < 1e-9, f"{mins} min at 60s steps")

print("\n6) the script is read-only about the journal")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "shadow_replay.py")).read()
check("it never opens the journal for writing",
      'open(p, "w")' not in SRC and "'w'" not in SRC,
      "re-resolution must not overwrite the live record it is auditing")
check("and it states the sampling limit it cannot escape",
      "LOWER bound" in SRC,
      "60s snapshots are a sampled path, not ticks")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all shadow-replay checks passed")
