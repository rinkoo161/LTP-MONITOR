"""v58.9 — tests for the pre-v50 weekend-keepalive candle prune, per
the roadmap's own framing ("cosmetic, low priority... a one-time
offline prune would reclaim space and remove the read-filter
dependency").

This is a DESTRUCTIVE operation on persisted data — every test in this
file runs against an ISOLATED TEMPORARY database (history.DB is
monkeypatched for the duration and always restored, even on failure),
never against the shared sandbox database, to guarantee zero risk to
any real or other-test data regardless of what this feature does.

Also verifies a deduplication fix made alongside this: app.py's
_in_market_session and the new shared agents.in_market_session are
confirmed to produce byte-identical results, not just "close enough" —
this project already found and fixed one real "two copies silently
drift" bug this session (news_engine.py/news_macro_agent.py's
duplicate regexes); this feature deliberately avoided repeating that
mistake by moving the definition to one shared location instead of
writing a second one for history.py to use.

Run:  python3 test_candle_prune.py
"""
import datetime as _dt
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
import agents
import app as app_module
import history

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) deduplication check: app.py's _in_market_session and the new "
     "shared agents.in_market_session are byte-identical, not a second "
     "parallel definition that could silently drift")
weekday_market = int(_dt.datetime(2026, 7, 27, 10, 0, tzinfo=agents.IST).timestamp())
weekend = int(_dt.datetime(2026, 7, 25, 12, 0, tzinfo=agents.IST).timestamp())
after_hours = int(_dt.datetime(2026, 7, 27, 20, 0, tzinfo=agents.IST).timestamp())
for label, ts in [("weekday market hours", weekday_market), ("weekend", weekend),
                 ("weekday after hours", after_hours)]:
    a = app_module._in_market_session(ts)
    b = agents.in_market_session(ts)
    check(f"{label}: app.py wrapper matches the shared function exactly",
          a == b, f"app={a}, shared={b}")

real_db = history.DB
tmpdir = tempfile.mkdtemp()
history.DB = os.path.join(tmpdir, "test_history.db")

try:
    print("\n2) dry-run count correctly identifies contaminated rows "
         "across MULTIPLE security_ids, without touching the table")
    rows = [
        ("NIFTY_SPOT_1m", weekday_market, 100, 101, 99, 100.5, None, None),      # legit
        ("NIFTY_SPOT_1m", weekday_market + 60, 100.5, 101.5, 99.5, 101, None, None),  # legit
        ("NIFTY_SPOT_1m", weekend, 100, 100, 100, 100, None, None),              # contaminated
        ("NIFTY_SPOT_1m", after_hours, 100, 100, 100, 100, None, None),         # contaminated
        ("BANKNIFTY_SPOT_1m", weekday_market, 57000, 57100, 56900, 57050, None, None),  # legit
        ("BANKNIFTY_SPOT_1m", weekend, 57000, 57000, 57000, 57000, None, None),  # contaminated
    ]
    c = history._conn()
    c.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()

    dry = history.prune_non_market_session_candles(dry_run=True, log=lambda m: None)
    check("total_rows correctly counted", dry["total_rows"] == 6, str(dry["total_rows"]))
    check("non_market_session_rows correctly counted (3 contaminated)",
          dry["non_market_session_rows"] == 3, str(dry["non_market_session_rows"]))
    check("breakdown by security_id is correct (2 NIFTY, 1 BANKNIFTY)",
          dry["by_security_id"] == {"NIFTY_SPOT_1m": 2, "BANKNIFTY_SPOT_1m": 1},
          str(dry["by_security_id"]))
    check("dry_run=True made zero changes to the table",
          dry["deleted"] == 0 and dry["dry_run"] is True)

    c2 = history._conn()
    still_six = c2.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    c2.close()
    check("all 6 rows genuinely still present after the dry run "
          "(the safe default really doesn't touch anything)",
          still_six == 6, str(still_six))

    print("\n3) actual prune (dry_run=False) deletes EXACTLY the "
         "contaminated rows, leaving legitimate rows for BOTH "
         "security_ids untouched")
    real = history.prune_non_market_session_candles(dry_run=False, log=lambda m: None)
    check("deleted count matches the dry-run prediction exactly",
          real["deleted"] == 3, str(real["deleted"]))

    c3 = history._conn()
    remaining = c3.execute(
        "SELECT security_id, ts FROM candles ORDER BY security_id, ts").fetchall()
    c3.close()
    check("exactly 3 rows remain", len(remaining) == 3, str(remaining))
    check("the remaining rows are EXACTLY the legitimate weekday-"
          "market-hours ones, for BOTH security_ids — nothing "
          "legitimate was touched",
          set(remaining) == {("BANKNIFTY_SPOT_1m", weekday_market),
                            ("NIFTY_SPOT_1m", weekday_market),
                            ("NIFTY_SPOT_1m", weekday_market + 60)},
          str(remaining))

    print("\n4) running the prune AGAIN on the now-clean table is a "
         "safe no-op (nothing left to delete, doesn't error)")
    again = history.prune_non_market_session_candles(dry_run=False, log=lambda m: None)
    check("zero additional rows deleted on a second run",
          again["deleted"] == 0, str(again["deleted"]))

    print("\n5) API endpoints: dry-run status, confirm-gate refusal, "
         "and confirmed deletion — all via real HTTP calls")
    c4 = history._conn()
    c4.execute("DELETE FROM candles")   # clean slate for this section specifically
    c4.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", [
        ("NIFTY_SPOT_1m", weekday_market, 100, 101, 99, 100.5, None, None),
        ("NIFTY_SPOT_1m", weekend, 100, 100, 100, 100, None, None)])
    c4.commit(); c4.close()

    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)

    r_status = client.get("/api/history/prune-candles-status")
    d_status = r_status.json()
    check("status endpoint reports the real current count via HTTP",
          d_status.get("ok") is True and d_status.get("non_market_session_rows") == 1,
          str(d_status))

    r_no_confirm = client.post("/api/history/prune-candles")
    check("POST without ?confirm=true is correctly refused, not "
          "silently deleting anything",
          "error" in r_no_confirm.json() and "confirm=true" in r_no_confirm.json()["error"],
          str(r_no_confirm.json()))

    c5 = history._conn()
    unchanged = c5.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    c5.close()
    check("the unconfirmed POST genuinely changed nothing (still 2 rows)",
          unchanged == 2, str(unchanged))

    r_confirmed = client.post("/api/history/prune-candles?confirm=true")
    d_confirmed = r_confirmed.json()
    check("confirmed POST actually deletes via a real HTTP call",
          d_confirmed.get("ok") is True and d_confirmed.get("deleted") == 1,
          str(d_confirmed))

finally:
    history.DB = real_db
    shutil.rmtree(tmpdir, ignore_errors=True)

print("\n6) source-level guard: history.py's prune functions actually "
     "use the shared agents.in_market_session, not a reimplemented copy")
hist_src = open("history.py").read()
check("count_non_market_session_candles uses the shared function",
      "agents.in_market_session(ts)" in hist_src)
check("no separate market-session logic reimplemented in history.py",
      "weekday() >= 5" not in hist_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
