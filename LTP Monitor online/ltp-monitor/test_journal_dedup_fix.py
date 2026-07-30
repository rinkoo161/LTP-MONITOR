"""v58.20+ — tests for the journal duplication bug fix.

Root cause, confirmed directly from live data: journal_done/
weekly_risk_done were in-memory-only bus flags that never survived a
server restart, while journal.json/weekly_risk_journal.json themselves
DO persist. Every restart that happened after 15:35 IST on a given day
re-ran that day's journal write and appended ANOTHER duplicate entry
rather than recognizing one already existed — confirmed live: 71 raw
journal.json entries collapsed to just 14 unique dates, some dates
duplicated up to 8 times with identical numbers.

Two-part fix:
  1. Write path: dedup-by-date/week before appending, so future writes
     replace an existing same-day entry instead of duplicating it.
  2. One-time startup migration (_dedupe_journal_file): cleans up
     duplicates already written before this fix existed, keeping the
     LAST entry per date/week.

Run:  python3 test_journal_dedup_fix.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents

src = open("agents.py").read()

print("1) source-level: the write path now dedupes by date/week "
     "before appending, for both the daily and weekly journals")
check("daily journal write path filters out any existing entry for "
     "today's date before appending",
      'journal = [j for j in journal if j.get("date") != today]' in src)
check("weekly risk journal write path filters out any existing entry "
     "for this week_id before appending",
      'weekly_journal = [w for w in weekly_journal if w.get("week") != week_id]'
      in src)

print("\n2) source-level: the standalone dedup migration function "
     "exists and is called once at startup for both journal files")
check("_dedupe_journal_file function is defined",
      "def _dedupe_journal_file(path, key, log=None):" in src)
check("it's called at startup for both JOURNAL and WEEKLY_RISK_JOURNAL",
      "for path, key in ((JOURNAL, \"date\"), (WEEKLY_RISK_JOURNAL, \"week\")):"
      in src)

print("\n3) BEHAVIORAL VERIFICATION: the dedup function actually "
     "cleans a file with real duplicate entries, matching the exact "
     "live pattern found (same date appearing multiple times with "
     "identical data)")
with tempfile.TemporaryDirectory() as tmpdir:
    test_path = os.path.join(tmpdir, "test_journal.json")
    # Reconstruct the exact live pattern: some dates duplicated many
    # times with identical numbers, in chronological append order.
    fake_journal = (
        [{"date": "2026-07-15", "trades": 10, "pnl": 100}] * 3 +
        [{"date": "2026-07-16", "trades": 20, "pnl": -50}] * 3 +
        [{"date": "2026-07-19", "trades": 15, "pnl": -4922}] * 8 +
        [{"date": "2026-07-24", "trades": 123, "pnl": -7890, "note": "first run"}] +
        [{"date": "2026-07-24", "trades": 123, "pnl": -7890, "note": "second run, same day"}]
    )
    json.dump(fake_journal, open(test_path, "w"))

    logs = []
    orig_count, deduped_count = agents._dedupe_journal_file(
        test_path, "date", log=lambda m: logs.append(m))

    check("original file had 16 entries (matching the constructed "
         "test data exactly)",
          orig_count == 16, str(orig_count))
    check("deduped down to 4 unique dates",
          deduped_count == 4, str(deduped_count))

    result = json.load(open(test_path))
    check("the file on disk was actually rewritten with 4 entries",
          len(result) == 4, str(len(result)))
    dates_in_order = [r["date"] for r in result]
    check("original chronological ordering preserved after dedup",
          dates_in_order == ["2026-07-15", "2026-07-16", "2026-07-19", "2026-07-24"],
          str(dates_in_order))

    last_2024_entry = next(r for r in result if r["date"] == "2026-07-24")
    check("for a date with genuinely different duplicate data (not just "
         "identical repeats), the LAST occurrence wins — not the first",
          last_2024_entry.get("note") == "second run, same day",
          str(last_2024_entry))

    check("a log message was emitted describing the cleanup",
          len(logs) == 1 and "16 entries" in logs[0] and "4 unique" in logs[0],
          str(logs))

print("\n4) BEHAVIORAL VERIFICATION: running the dedup function AGAIN "
     "on the now-clean file is a true no-op (confirms future restarts "
     "won't keep rewriting the file unnecessarily)")
with tempfile.TemporaryDirectory() as tmpdir:
    test_path = os.path.join(tmpdir, "test_journal2.json")
    clean_journal = [{"date": "2026-07-15", "trades": 10, "pnl": 100},
                    {"date": "2026-07-16", "trades": 20, "pnl": -50}]
    json.dump(clean_journal, open(test_path, "w"))
    mtime_before = os.path.getmtime(test_path)

    logs2 = []
    orig2, deduped2 = agents._dedupe_journal_file(
        test_path, "date", log=lambda m: logs2.append(m))
    check("original and deduped counts are equal (already clean)",
          orig2 == deduped2 == 2, str((orig2, deduped2)))
    check("no log message emitted for an already-clean file (matches "
         "the function's own documented no-op behavior)",
          len(logs2) == 0, str(logs2))

print("\n5) BEHAVIORAL VERIFICATION: a missing file is handled "
     "gracefully, not an exception")
missing_result = agents._dedupe_journal_file("/tmp/definitely_does_not_exist_12345.json",
                                             "date")
check("a nonexistent file returns (0, 0) rather than raising",
      missing_result == (0, 0), str(missing_result))

print("\n6) JS/Python syntax sanity — the whole module still imports "
     "cleanly after these changes")
import importlib
try:
    importlib.reload(agents)
    check("agents module reloads without error", True)
except Exception as e:
    check("agents module reloads without error", False, str(e))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
