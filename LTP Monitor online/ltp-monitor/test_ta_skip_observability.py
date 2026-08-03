#!/usr/bin/env python3
"""test_ta_skip_observability.py — a session's skip profile must survive
the process that observed it.

2026-08-03. `ta_calibration` captured 9 rows for the session against a
design of roughly one per symbol per 5m candle (~300). What the archive
could rule out, it did:

    candle drought      no — 166,076 candles written that day
    REST starvation     no — 11 rate-limit lines, vs 98 on a healthy day
    restarts            no — one restart during market hours
    compute_state       no — ok on 55 of 55 progressive 5m slices

What it could NOT rule out was the skip profile, because
`TAElliottAgent` kept its counters ONLY in `self.summary` — an in-memory
string on the Agents page. Every restart erased a session's worth of
evidence, and the one question left standing was the one nobody could
answer after the fact.

The confirmed half is separate and is NOT what this test covers:
`compute_state` requires `bb_period + 3` = 23 FIVE-MINUTE bars and is
handed SESSION-ONLY candles, so S9 is structurally blind from 09:15
until roughly 11:05 every single day while ~150 bars sit unused in the
archive. That is a behaviour change to propose, not to slip in here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_ta_skip_observability")

import agents

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeBus:
    def __init__(self):
        self.logs = []

    def log(self, who, msg):
        self.logs.append(msg)

    def get(self, k, d=None):
        return d

    def set(self, k, v):
        pass


def agent():
    a = agents.TAElliottAgent.__new__(agents.TAElliottAgent)
    a.name = "ta_elliott"
    a.bus = FakeBus()
    return a


# The block under test is the tail of cycle(); exercise it directly with
# the same shape cycle() builds, rather than driving a whole agent tick
# that would need a broker, a bus and a market session.
def emit(a, skipped):
    _skip_now = tuple(sorted((k, v) for k, v in skipped.items() if v))
    if _skip_now != getattr(a, "_last_skip_profile", None):
        a._last_skip_profile = _skip_now
        if _skip_now:
            a.bus.log(a.name, "skipping: " + ", ".join(
                f"{k}={v}" for k, v in _skip_now))


print("1) the source actually contains this logic at cycle scope")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "agents.py")).read()
check("the skip profile is logged, not only summarised",
      "_last_skip_profile" in SRC and 'self.bus.log(self.name, "skipping: "' in SRC,
      "self.summary alone dies with the process")
# It must sit OUTSIDE the `if fired:` branch — a profile that is only
# reported on cycles that produced a signal reports nothing on the
# cycles that are actually the problem.
_anchor = next((l for l in SRC.split("\n") if "_skip_now = tuple(sorted(" in l), "")
_indent = len(_anchor) - len(_anchor.lstrip())
check("it runs on every cycle, not only when a signal fired",
      _indent == 8,
      f"indent={_indent} (8 = cycle scope; 12+ would put it inside a branch)")

print("\n2) a persisting condition is reported ONCE, not every cycle")
a = agent()
for _ in range(5):
    emit(a, {"no_pack": 4, "state_not_ok": 0})
check("five identical cycles produce one line", len(a.bus.logs) == 1,
      f"{len(a.bus.logs)} lines — 180s cadence means ~125 cycles a session")
check("and it names the reason and the count",
      "no_pack=4" in a.bus.logs[0], a.bus.logs[0])

print("\n3) a CHANGE is what gets reported")
emit(a, {"no_pack": 2})
check("a changed count emits again", len(a.bus.logs) == 2, str(a.bus.logs))
emit(a, {"no_pack": 2, "on_cooldown": 1})
check("a new reason emits again", len(a.bus.logs) == 3,
      a.bus.logs[-1] if len(a.bus.logs) > 2 else "")
check("both reasons appear", "no_pack=2" in a.bus.logs[-1]
      and "on_cooldown=1" in a.bus.logs[-1], a.bus.logs[-1])

print("\n4) a clean cycle is silent")
b = agent()
for _ in range(3):
    emit(b, {"no_pack": 0, "state_not_ok": 0})
check("nothing skipped logs nothing", b.bus.logs == [], str(b.bus.logs))
# and returning to clean does not emit a line either
emit(a, {})
check("recovering to clean is silent too", len(a.bus.logs) == 3,
      "the absence of a skip line IS the recovery signal")

print("\n5) the blackout this investigation confirmed is NOT silently fixed")
import ta_elliott
need = int(ta_elliott.TA_ELLIOTT_DEFAULTS["bb_period"]) + 3
check("compute_state still requires bb_period+3 5m bars", need == 23,
      f"{need} bars = ~{need * 5} minutes; fed session-only candles this "
      f"blanks 09:15-11:05 daily. Changing it changes what S9 SEES and "
      f"is a decision, not a cleanup.")
check("and it still reports why it declined",
      '"reason": "not enough 5m candles"' in open(
          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "ta_elliott.py")).read())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all ta skip-observability checks passed")
