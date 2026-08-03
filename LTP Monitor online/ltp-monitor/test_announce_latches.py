#!/usr/bin/env python3
"""test_announce_latches.py — a success message whose absence is
ambiguous is not a message.

2026-08-03, roadmap B4. The futures OI archive announced "archive
active" behind `if not getattr(self, "_foi_archive_ok", False)` — once
per PROCESS, then silence forever. It fired on 31 July and never again.
Because it can only ever fire once per restart, its absence across every
LATER restart read as "still fine" rather than "has not run since", and
the archive was dead for a session before anyone noticed.

Two shapes of the same root cause — a per-process boolean standing in
for state that changes over time:

  SUCCESS latch   announce once, then never. Absence means BOTH "working"
                  and "stopped three days ago".
  FAILURE latch   warn once, then never. The same fault on Monday and on
                  Friday is one line, and a relapse after a recovery is
                  none.

Both now go through `should_log_throttled`, the throttle that already
existed — extending the shared one rather than growing a third
near-copy, which is the standing rule here and the reason the market
session check, the news regexes and the OI quadrant classifier were each
collapsed to one definition.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_announce_latches")

import agents

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "agents.py")).read()
CODE = "\n".join(l for l in SRC.split("\n") if not l.strip().startswith("#"))


class Obj:
    pass


print("1) no once-per-process latch survives in agents.py")
import re
latches = re.findall(r'if(?: \w+ and)? not getattr\(self, "(_[a-z_]+)", False\)', CODE)
check("none remain", not latches, f"found {latches}")
for dead in ("_foi_archive_ok", "_fut_archive_announced",
             "_daily_ohlc_write_failed"):
    check(f"{dead} is gone from live code", dead not in CODE)

print("\n2) the SUCCESS announce is once per DAY, not once per process")
o = Obj()
first = agents.should_log_throttled(o, "_foi_archive_daily", "all",
                                    "2026-08-04", window=86400)
again = agents.should_log_throttled(o, "_foi_archive_daily", "all",
                                    "2026-08-04", window=86400)
check("the first write of the day announces", first)
check("a second write the same day stays quiet", not again,
      "one line a session, not one per 3s cycle")
nextday = agents.should_log_throttled(o, "_foi_archive_daily", "all",
                                      "2026-08-05", window=86400)
check("the next day announces again", nextday,
      "THIS is the property that was missing — a missing line now "
      "unambiguously means the archive did not write today")

print("\n3) it is wired that way at both archive call sites")
check("the OI archive uses the daily throttle",
      '"_foi_archive_daily"' in CODE and "window=86400" in CODE)
check("the OHLCV archive uses it too, keyed per symbol",
      '"_fut_archive_daily"' in CODE)
check("both pass the DATE as the reason",
      CODE.count('now_ist().strftime("%Y-%m-%d"),') >= 1
      or CODE.count("now_ist().strftime('%Y-%m-%d'),") >= 1,
      "the date being the reason is what makes a new day always log")

print("\n4) the FAILURE latch re-reports instead of firing once ever")
o2 = Obj()
e1 = "OperationalError: database is locked"
check("the first failure reports",
      agents.should_log_throttled(o2, "_daily_ohlc_fail", "NIFTY", e1))
check("an identical failure moments later does not spam",
      not agents.should_log_throttled(o2, "_daily_ohlc_fail", "NIFTY", e1),
      "the 90s cycle must not produce a line every 90s")
check("a DIFFERENT failure reports immediately",
      agents.should_log_throttled(o2, "_daily_ohlc_fail", "NIFTY",
                                  "OSError: disk full"),
      "a changed error is new information, not a repeat")
check("and a different symbol is tracked separately",
      agents.should_log_throttled(o2, "_daily_ohlc_fail", "BANKNIFTY", e1),
      "one symbol failing must not mask another")
# The recurrence property the old latch lacked: after the window, the
# SAME error reports again, so a fault that persists for days is visible
# on each of those days rather than only on the first.
o3 = Obj()
agents.should_log_throttled(o3, "_recur", "NIFTY", e1, window=600)
o3._recur["NIFTY"] = (e1, time.time() - 601)
check("the same error recurs after the window",
      agents.should_log_throttled(o3, "_recur", "NIFTY", e1, window=600),
      "a fault on Monday and the same fault on Friday used to be ONE line")

print("\n5) per-connection warn flags in app.py are NOT this bug")
APP = open(os.path.join(HERE, "app.py")).read()
check("levels_warned is reset per connection, not per process",
      "levels_warned = False" in APP,
      "a local reset each time is a legitimate warn-once-per-connection")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all announce-latch checks passed")
