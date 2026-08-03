"""v53 hygiene round tests:
  1. config.save() warns loudly (activity.log) on dropped/unregistered
     keys instead of silently discarding them.
  2. LearningAgent wires history.prune_chain_snapshots() to an actual
     once-per-day scheduler.

Run:  python3 test_v53_hygiene.py
"""
import os
import sys
import time
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config, deletes rows")
import config
import history
import agents
from agents import Bus

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) config.save() warns loudly on a dropped key")
# Truncate activity.log's tail marker so we can look for a fresh line
# rather than matching a stale one from an earlier run.
marker = f"__test_marker_{int(time.time()*1000)}__"
config.save({marker: "should be dropped", "paper_mode": True})
time.sleep(0.05)
found = False
if os.path.exists(config._LOG_FILE):
    tail = open(config._LOG_FILE).readlines()[-20:]
    found = any("DROPPED" in l and marker in l for l in tail)
check("a dropped key produces a loud [config] DROPPED line in activity.log",
      found, "checked last 20 lines of activity.log")

d = config.load()
check("the dropped key was NOT actually persisted",
      marker not in d)

config.save({"paper_mode": True})   # a normal save with nothing dropped
tail_before = len(open(config._LOG_FILE).readlines()) if os.path.exists(config._LOG_FILE) else 0
config.save({"paper_mode": True})
tail_after = len(open(config._LOG_FILE).readlines()) if os.path.exists(config._LOG_FILE) else 0
check("a clean save (nothing dropped) does NOT write a warning line",
      tail_after == tail_before, f"{tail_before} -> {tail_after}")

print("\n2) snapshot retention wired to LearningAgent's daily cycle")
SYM = "V53TEST"
conn = history._conn()
conn.execute("DELETE FROM chain_snapshots WHERE symbol=?", (SYM,))
conn.commit()
old_ts = int(time.time()) - 10 * 86400   # 10 days old — outside the 5-day default
recent_ts = int(time.time()) - 1 * 86400
history.upsert_chain_snapshot(SYM, old_ts, [{"strike": 100, "ce": {"ltp": 1}, "pe": {"ltp": 1}}])
history.upsert_chain_snapshot(SYM, recent_ts, [{"strike": 100, "ce": {"ltp": 1}, "pe": {"ltp": 1}}])
before = conn.execute("SELECT COUNT(*) FROM chain_snapshots WHERE symbol=?", (SYM,)).fetchone()[0]
check("fixture seeded 4 rows (1 strike x 2 legs x 2 timestamps)",
      before == 4, f"got {before}")

bus = Bus()
bus.set("closed_trades", [])
bus.set("chain_prune_done", None)   # force the prune branch to run
ag = agents.LearningAgent(bus, {})
real_market_open = agents.market_open
agents.market_open = lambda: False   # keep the journal half of cycle() a no-op
try:
    ag.cycle()
finally:
    agents.market_open = real_market_open

after = conn.execute("SELECT COUNT(*) FROM chain_snapshots WHERE symbol=?", (SYM,)).fetchone()[0]
# v59.0 item 18 INVERTED this assertion. It used to require the 10-day-old
# row to be GONE. That 5-day hard delete is exactly why the historical
# premiums needed to reprice the replays (item 16) do not exist, so the
# 10-day row must now SURVIVE — it is inside tier 1 (90 days), where
# nothing is thinned at all.
check("cycle() no longer deletes rows merely for being older than 5 days",
      after == 4, f"before={before} after={after} (expect all 4 — tier 1 is "
                  f"untouched for 90 days; a 5-day delete is the item-18 bug)")
# 2026-08-04 — the AGENT stamps this with its own "today" during cycle(),
# and the assertion recomputed "today" afterwards. A run straddling
# midnight compares 08-04 against 08-05 and fails. Found by running the
# whole suite with the clock shifted to 23:59:30, which reproduced it
# twice while the same suite is green at 11:00 — the second instance of
# this class today, after test_oi_composite. Accepting either side keeps
# what the check is actually for ("was it stamped with the current day",
# not "which day is it") and still fails on None or an unrelated date.
_prune_days = {
    agents.now_ist().strftime("%Y-%m-%d"),
    (agents.now_ist() - _dt.timedelta(days=1)).strftime("%Y-%m-%d"),
}
check("chain_prune_done marked for today (won't re-run this cycle)",
      bus.get("chain_prune_done") in _prune_days,
      str(bus.get("chain_prune_done")))

# second cycle same day must NOT re-run the prune (idempotent daily gate)
history.upsert_chain_snapshot(SYM, old_ts, [{"strike": 100, "ce": {"ltp": 1}, "pe": {"ltp": 1}}])
agents.market_open = lambda: False
try:
    ag.cycle()
finally:
    agents.market_open = real_market_open
after2 = conn.execute("SELECT COUNT(*) FROM chain_snapshots WHERE symbol=?", (SYM,)).fetchone()[0]
check("re-running cycle() the SAME day does not prune again "
      "(daily gate, not every-cycle)",
      after2 == 4, f"expected the re-inserted old snapshot's 2 rows to "
                   f"survive alongside the recent snapshot's 2 rows: got {after2}")

print("\n2b) tiering is what enforces retention now, not deletion")
conn.execute("DELETE FROM chain_snapshots WHERE symbol=?", (SYM,))
conn.commit()
# Two rows a minute apart, 200 days old — inside tier 2, where the grid is
# 5 minutes, so one of them must go and one must stay.
# SNAPPED to a 300s boundary on purpose: unaligned, the pair straddles a
# bucket edge roughly one run in five and BOTH rows legitimately survive.
# That would be a test failing on the wall clock, not on the code.
t2a = ((int(time.time()) - 200 * 86400) // 300) * 300
history.upsert_chain_snapshot(SYM, t2a, [{"strike": 100, "ce": {"ltp": 1}, "pe": {"ltp": 1}}])
history.upsert_chain_snapshot(SYM, t2a + 60, [{"strike": 100, "ce": {"ltp": 2}, "pe": {"ltp": 2}}])
history.prune_chain_snapshots()
kept = conn.execute("SELECT COUNT(*) FROM chain_snapshots WHERE symbol=?", (SYM,)).fetchone()[0]
check("tier 2 thins a 60s cadence to the 5-min grid", kept == 2,
      f"expect 2 (CE+PE at one grid point) from 4 rows: got {kept}")
conn.execute("DELETE FROM chain_snapshots WHERE symbol=?", (SYM,))
conn.commit()

conn.execute("DELETE FROM chain_snapshots WHERE symbol=?", (SYM,))
conn.commit()

print("\n3) retention days respects config")
config.save({"chain_snapshot_retention_days": 1})
check("chain_snapshot_retention_days round-trips",
      config.load().get("chain_snapshot_retention_days") == 1)
config.save({"chain_snapshot_retention_days":
            config.DEFAULTS["chain_snapshot_retention_days"]})   # restore default

print("\n4) prune failure is logged loudly, not swallowed")
bus2 = Bus()
bus2.set("closed_trades", [])
bus2.set("chain_prune_done", None)
ag2 = agents.LearningAgent(bus2, {})
real_prune = history.prune_chain_snapshots
history.prune_chain_snapshots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
agents.market_open = lambda: False
try:
    ag2.cycle()   # must not raise
finally:
    history.prune_chain_snapshots = real_prune
    agents.market_open = real_market_open
logged = any("chain_snapshots prune FAILED" in line for line in bus2.feed)
check("a prune exception is caught, logged loudly, and does not crash cycle()",
      logged, f"feed had {len(bus2.feed)} lines")
check("chain_prune_done NOT marked when the prune failed "
      "(so it retries next cycle instead of giving up for the day)",
      bus2.get("chain_prune_done") is None, str(bus2.get("chain_prune_done")))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
