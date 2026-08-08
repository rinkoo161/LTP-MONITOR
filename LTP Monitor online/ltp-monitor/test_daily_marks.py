#!/usr/bin/env python3
"""test_daily_marks.py — "once per day" must not mean "once per restart".

2026-08-08. LearningAgent's daily maintenance was gated on
`self.bus.get("chain_prune_done") != today`. The Bus is IN-MEMORY, so
the marker died with the process: the job ran on every boot.

From activity.log, while v59.53..v59.58 were being deployed:

    chain_snapshots retention ran NINE times between 00:01 and 00:20

each scanning a 752,254-row table in a 470 MB database, each holding the
write lock long enough to push other writers past their 30s
busy_timeout —

    [00:17:58] market_data ⚠ NIFTY: futures OI archive FAILED
               (RuntimeError: database is locked)

— and each thinning nothing at all (`tier2_thinned: 0, tier3_thinned: 0`).
The "database is locked" distribution in the log tracks restart
frequency, not load: 15 on 2026-07-26, none for eleven days, a cluster
today.

The IV backfill added in v59.53 had the identical flaw, because it
followed the existing convention without questioning it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_daily_marks")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import subprocess

import daily_marks as dm

HERE = os.path.dirname(os.path.abspath(__file__))

print("1) a mark survives the process that made it")
check("unmarked to begin with", not dm.done("unit_key", "2026-08-08"))
dm.mark("unit_key", "2026-08-08")
check("marked in this process", dm.done("unit_key", "2026-08-08"))
# The whole point: a NEW interpreter, i.e. what a restart actually is.
out = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,%r); import daily_marks as d;"
     "print(d.done('unit_key','2026-08-08'))" % HERE],
    capture_output=True, text=True, env=dict(os.environ))
check("STILL marked in a brand-new process", out.stdout.strip() == "True",
      f"stdout={out.stdout.strip()!r} stderr={out.stderr.strip()[:120]!r} "
      f"— an in-memory marker is why the prune ran nine times in 20 min")

print("\n2) it still rolls over to the next day")
check("a different stamp is not marked", not dm.done("unit_key", "2026-08-09"),
      "a marker that never expires would stop maintenance running at all")

print("\n3) a corrupt marker file re-runs rather than wedging")
open(dm.PATH, "w").write("{not json")
check("corrupt file reads as empty", dm.all_marks() == {})
check("and therefore reports NOT done", not dm.done("unit_key", "2026-08-08"),
      "falling back to 'nothing has run' re-runs an idempotent job — "
      "the pre-2026-08-08 behaviour. Failing the other way would stop "
      "retention forever and silently")
dm.mark("unit_key", "2026-08-08")
check("and writing recovers the file", dm.done("unit_key", "2026-08-08"))

print("\n4) writes are atomic (no torn file on a crash)")
src = open(os.path.join(HERE, "daily_marks.py")).read()
check("mark() writes via a temp file and os.replace",
      "os.replace(" in src and '".tmp"' in src,
      "a half-written marker file would be read as corrupt and re-run "
      "the very job this exists to stop re-running")

print("\n5) it does NOT live in the contended database")
check("marks are a JSON file, not a history.db table",
      dm.PATH.endswith(".json"),
      f"{dm.PATH} — the problem being fixed IS contention on history.db; "
      f"a marker needing the write lock to record 'I finished writing' "
      f"would contend for the resource it protects")

print("\n6) both maintenance gates actually use it")
AG = open(os.path.join(HERE, "agents.py")).read()
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]
for key in ("chain_prune_done", "iv_backfill_done"):
    gated = any(f'daily_marks.done("{key}"' in l for l in _code)
    marked = any(f'daily_marks.mark("{key}"' in l for l in _code)
    check(f"{key} is gated on daily_marks", gated)
    check(f"{key} is recorded to daily_marks", marked)
    check(f"{key} no longer GATES on the in-memory bus",
          not any(f'self.bus.get("{key}")' in l for l in _code),
          "the bus may still be SET for introspection, but it must not "
          "be what decides whether the job runs")

print("\n7) the remaining bus-keyed markers are known, not forgotten")
# journal_done and weekly_risk_done have the same in-memory flaw but
# different semantics — re-running the EOD journal after a restart is a
# behaviour question, not a lock question, so they were deliberately
# NOT changed. This check exists so the next person finds them.
for key in ("journal_done", "weekly_risk_done"):
    still_bus = any(f'self.bus.get("{key}")' in l for l in _code)
    check(f"{key} is still bus-keyed (known, deliberately unchanged)",
          still_bus,
          "if this fails someone fixed it — update this test and the "
          "ROADMAP note rather than deleting the check")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all daily-marks checks passed")
