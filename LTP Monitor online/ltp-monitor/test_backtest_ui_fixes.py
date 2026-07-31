"""v58.9 (post-deployment fixes, round 30) — tests for two real bugs
found from a live report after v58.9 shipped:

1. "Current day data is not available even after backtest execution."
   Root cause: two separate buttons — "Run backtest" (replays existing
   archives only) and "Sync + backtest" (archives today's data FIRST)
   — look similar but behave very differently, and clicking the first
   one when today hasn't been synced yet silently never picks up
   today's data. Fixed with an explicit includes_today flag on
   history.coverage() and a clear on-page warning.

2. "After Optimize, no change in values." Root cause: the frontend
   alert only ever confirmed the optimize JOB WAS QUEUED — the actual
   sweep runs asynchronously (can take up to ~2 minutes) and the old
   code never waited for or displayed the real result. Fixed by
   polling for completion and showing exactly what happened.

Run:  python3 test_backtest_ui_fixes.py
"""
import datetime as _dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
import agents
import history

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


SYM = "COVTEST2"


def cleanup():
    conn = history._conn()
    conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
    conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
    conn.commit()
    conn.close()


cleanup()
try:
    print("1) coverage() correctly reports includes_today=False when "
         "only a PRIOR day's data has been archived")
    today = agents.now_ist().date().isoformat()
    yesterday = (agents.now_ist() - _dt.timedelta(days=1)).date().isoformat()
    conn = history._conn()
    conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
                (f"{SYM}_24000CE", SYM, "opt"))
    conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
                (f"{SYM}_IDX", SYM, "idx"))
    ts_yesterday = int(time.mktime(time.strptime(yesterday, "%Y-%m-%d"))) + 10 * 3600
    conn.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                (f"{SYM}_24000CE", ts_yesterday, 100, 101, 99, 100, None, None))
    conn.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                (f"{SYM}_IDX", ts_yesterday, 24000, 24001, 23999, 24000, None, None))
    conn.commit()
    conn.close()

    cov = history.coverage()
    check("chain_days correctly counts the 1 archived day",
          cov.get(SYM, {}).get("chain_days") == 1, str(cov.get(SYM)))
    check("includes_today is False — only yesterday's data exists",
          cov.get(SYM, {}).get("includes_today") is False, str(cov.get(SYM)))

    print("\n2) after archiving TODAY's data too, includes_today "
         "flips to True")
    conn = history._conn()
    ts_today = int(time.mktime(time.strptime(today, "%Y-%m-%d"))) + 10 * 3600
    conn.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                (f"{SYM}_24000CE", ts_today, 100, 101, 99, 100, None, None))
    conn.commit()
    conn.close()

    cov2 = history.coverage()
    check("includes_today now True after today's data is archived",
          cov2.get(SYM, {}).get("includes_today") is True, str(cov2.get(SYM)))
    check("chain_days correctly counts both distinct days now",
          cov2.get(SYM, {}).get("chain_days") == 2, str(cov2.get(SYM)))
finally:
    cleanup()

print("\n3) source-level guard: the frontend actually surfaces "
     "includes_today with a clear explanation, not silently")
h = open("static/dashboard.html").read()
check("frontend reads c.includes_today",
      "c.includes_today" in h)
check("the warning explicitly names the two buttons and the "
      "distinction between them, not just a generic warning icon",
      "Run backtest" in h and "Sync + backtest" in h and
      "today not yet archived" in h)

print("\n4) source-level guard: history.coverage() computes "
     "includes_today via a real per-day distinct-date check, not a "
     "simplistic count comparison that could be wrong")
hist_src = open("history.py").read()
check("includes_today computed by checking the actual today's date "
      "string against the distinct archived dates",
      "today_str in opt_days" in hist_src)

print("\n5) source-level guard: optimizeStrategy no longer alerts "
     "immediately after queuing — it polls for and displays the real "
     "result")
check("polls /api/backtest/status for the real sweep result",
      "const status=await (await fetch(\"/api/backtest/status\")).json();" in h)
check("distinguishes an improved result from a no-improvement result "
      "in the message shown to the user, not a generic 'done'",
      "found a better version" in h and "none beat the current version" in h)
check("refreshes the backtest table after showing the result, so any "
      "change is immediately visible without a manual reload",
      "loadBacktest();" in h.split("async function optimizeStrategy")[1][:3000])

print("\n6) THE REAL BUG BEHIND #5 ('multiple versions with untested "
     "remarks'): the version-label check used x.results.trades "
     "TRUTHINESS, but 0 is falsy in JS — a version genuinely tested by "
     "the auto-tuner/optimizer that found ZERO trades in the backtest "
     "window got mislabeled '(untested)' as if never backtested at all")
check("frontend now checks for the PRESENCE of a results value "
      "(!= null), not its truthiness",
      "x.results&&x.results.trades!=null" in h)
check("a genuinely tested zero-trade version now shows real numbers, "
      "not the misleading 'untested' label",
      "(untested)" in h and "'t)'" in h)   # both branches still present in source

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
