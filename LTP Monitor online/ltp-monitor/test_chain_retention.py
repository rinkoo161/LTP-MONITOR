#!/usr/bin/env python3
"""test_chain_retention.py — v59.0 item 18.

The failure this guards against is not "the prune crashed". It is the
prune QUIETLY DESTROYING the thing it was changed to preserve — thinning
tier 2 down to nothing, or tier 3 losing a session entirely. Both would
look like a successful maintenance run in the log.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_chain_retention")

import history

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


NOW = int(time.time())
DAY = 86400
CFG = {"chain_tier1_days": 90, "chain_tier2_days": 730,
       "chain_tier2_interval_sec": 300}

c = history._conn()
c.execute("DELETE FROM chain_snapshots WHERE symbol='ZZRET'")
rows = []
# tier 1 (10 days old): 60s cadence for one hour
for i in range(60):
    rows.append((NOW - 10 * DAY + i * 60, 100.0 + i))
# tier 2 (200 days old): 60s cadence for one hour -> should thin to ~12
for i in range(60):
    rows.append((NOW - 200 * DAY + i * 60, 200.0 + i))
# tier 3 (900 days old): two distinct sessions, 60 rows each -> 1 each.
# ANCHORED to 09:30 IST, not to "now minus 900 days": tier 3 groups by IST
# session date, and an unanchored one-hour window straddles IST midnight
# whenever the suite happens to run late, splitting one session into two
# and failing on the wall clock rather than on the code.
for d in (900, 901):
    base_day = ((NOW - d * DAY) // DAY) * DAY + 4 * 3600   # 04:00 UTC = 09:30 IST
    for i in range(60):
        rows.append((base_day + i * 60, 300.0 + i))
for ts, ltp in rows:
    c.execute("INSERT OR REPLACE INTO chain_snapshots"
              "(symbol,strike,leg,ts,ltp) VALUES(?,?,?,?,?)",
              ("ZZRET", 24000, "ce", ts, ltp))
c.commit()


def count(lo, hi):
    return c.execute("SELECT COUNT(*) FROM chain_snapshots WHERE symbol='ZZRET' "
                     "AND ts>=? AND ts<?", (lo, hi)).fetchone()[0]


before_t1 = count(NOW - 90 * DAY, NOW)
res = history.prune_chain_snapshots(cfg=CFG)
check("returns a tiered report", res.get("mode") == "tiered", str(res))

check("tier 1 untouched", count(NOW - 90 * DAY, NOW) == before_t1 == 60,
      f"{count(NOW - 90*DAY, NOW)} rows kept of 60")

t2 = count(NOW - 730 * DAY, NOW - 90 * DAY)
check("tier 2 thinned to the 5-min grid", 10 <= t2 <= 14,
      f"{t2} rows kept of 60 (one hour at 300s => ~12)")
check("tier 2 is not emptied", t2 > 0)

t3 = count(0, NOW - 730 * DAY)
check("tier 3 keeps one row per session", t3 == 2,
      f"{t3} rows kept of 120 across 2 sessions")

# The columns item 16 needs must survive, not just the row.
r = c.execute("SELECT ltp FROM chain_snapshots WHERE symbol='ZZRET' "
              "AND ts<? ORDER BY ts LIMIT 1", (NOW - 730 * DAY,)).fetchone()
check("surviving rows keep their data", r and r[0] is not None, str(r))

# Idempotent: a second run must not keep eating rows.
history.prune_chain_snapshots(cfg=CFG)
check("second run is a no-op",
      count(NOW - 730 * DAY, NOW - 90 * DAY) == t2 and count(0, NOW - 730 * DAY) == t3,
      "retention must converge, not erode the archive daily")

# Legacy hard delete still available for callers that mean it.
res2 = history.prune_chain_snapshots(days=5)
check("explicit days= still hard-deletes", res2.get("mode") == "hard_delete",
      str(res2))
check("hard delete removed the old rows", count(0, NOW - 5 * DAY) == 0)

# The 5-day default must no longer be what the agent runs.
ag = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.py")).read()
check("agent no longer passes a retention day count",
      "prune_chain_snapshots()" in ag and
      "prune_chain_snapshots(days)" not in ag,
      "otherwise the archive keeps getting deleted nightly")

c.execute("DELETE FROM chain_snapshots WHERE symbol='ZZRET'")
c.commit(); c.close()

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all chain-retention checks passed")
