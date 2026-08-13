#!/usr/bin/env python3
"""test_partial_day_integrity.py — v59.81, the 2026-08-13 host-sleep incident.

The machine slept 11:52 -> 18:25 (3.6 h of live market). Two consequences,
both silent, both fixed here:

  1. The day was recorded at 243 bars against a 1,078-1,391 norm, with a
     hole in the middle. Excluded only while it was "today"; the NEXT day
     it would have replayed as a full session and counted as one full
     independent day toward the promotion gate's 10-day minimum.
  2. On wake the EOD square-off closed an open spread using quotes frozen
     6.5 hours earlier and booked a clean-looking net of Rs 12.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_partial_day_integrity")

import agents
import backtester as bt
import config
import history

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- 1. day-quality filter ---------------------------------------------
# Real shape of the incident: healthy days ~1000+, the slept day 243.
# NOTE the dates are deliberately NOT today: `_completed_days` drops
# today first, so a fixture dated today would be excluded for the wrong
# reason and the coverage filter would never be exercised. (The first
# version of this test used the real incident date — which was that
# day — and four assertions failed for exactly that reason.)
DAYS = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
        "2026-07-07", "2026-07-08", "2026-07-09"]
COV = {"2026-07-01": 1000, "2026-07-02": 962, "2026-07-03": 1100,
       "2026-07-04": 1391, "2026-07-07": 1078, "2026-07-08": 1278,
       "2026-07-09": 243}          # the slept day (real shape: 243 bars)

_orig_cov, _orig_load = history.day_bar_coverage, config.load
_base = config.load()
logs = []
try:
    history.day_bar_coverage = lambda sym, kind="opt": dict(COV)
    config.load = lambda: {**_base, "partial_day_min_coverage_pct": 60}
    kept = bt._completed_days(list(DAYS), "NIFTY", "opt", logs.append)
    check("the under-recorded day is EXCLUDED from replays",
          "2026-07-09" not in kept, f"kept {kept}")
    check("every healthy day survives",
          len(kept) == 6 and "2026-07-08" in kept, f"{len(kept)} kept")
    check("the exclusion is LOGGED with the numbers, not silent",
          logs and "2026-07-09" in logs[0] and "243" in logs[0],
          logs[0][:110] if logs else "no log line")

    # Backwards compatibility: no symbol => pre-v59.81 behaviour exactly.
    check("without a symbol the filter is inert (old callers unchanged)",
          bt._completed_days(list(DAYS)) == DAYS)

    # A thin archive must stay replayable rather than vanish.
    history.day_bar_coverage = lambda sym, kind="opt": {d: 50 for d in DAYS}
    check("a uniformly thin archive is NOT emptied (would read as 'no signals')",
          len(bt._completed_days(list(DAYS), "NIFTY")) == len(DAYS))

    # Too few days to have a norm => don't guess.
    history.day_bar_coverage = lambda sym, kind="opt": {"2026-07-01": 1000,
                                                        "2026-07-02": 5}
    check("with <3 days there is no median to judge against, so nothing drops",
          bt._completed_days(["2026-07-01", "2026-07-02"], "NIFTY") ==
          ["2026-07-01", "2026-07-02"])

    # The filter is a QUALITY check and must never break a replay.
    def _boom(*a, **k):
        raise RuntimeError("db gone")
    history.day_bar_coverage = _boom
    check("a coverage-lookup failure degrades to 'keep everything', not a crash",
          bt._completed_days(list(DAYS), "NIFTY") == DAYS)
finally:
    history.day_bar_coverage, config.load = _orig_cov, _orig_load

# today is still excluded regardless
import datetime
_today = datetime.datetime.now(agents.IST).strftime("%Y-%m-%d")
check("today is still excluded (the original guarantee)",
      _today not in bt._completed_days(DAYS + [_today]))

# --- 2. stale EOD fills are marked, not booked as fact ------------------
cfg = {**_base, "eod_max_price_age_sec": 900}
import time
now = time.time()
check("a fresh price books silently (no note)",
      agents.stale_fill_note(now - 60, cfg, now) == "")
note = agents.stale_fill_note(now - 6.5 * 3600, cfg, now)     # the real case
check("a 6.5-hour-old price is marked UNVERIFIED",
      "UNVERIFIED" in note and "390m" in note, note)
check("the note says the P&L is indicative rather than a real fill",
      "indicative" in note, note)
check("a missing timestamp is also UNVERIFIED, not assumed fresh",
      "UNVERIFIED" in agents.stale_fill_note(None, cfg, now))
check("the boundary is inclusive (exactly at the limit still books)",
      agents.stale_fill_note(now - 900, cfg, now) == "")

# --- wired into all three square-off paths -----------------------------
ag = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
check("stale_fill_note is wired into 3 square-off paths (option/spread/future)",
      ag.count("stale_fill_note(") >= 4,      # 1 def + 3 call sites
      f"{ag.count('stale_fill_note(')} occurrences")
for key in ("eod_max_price_age_sec", "partial_day_min_coverage_pct"):
    check(f"'{key}' registered in DEFAULTS", key in config.DEFAULTS)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all partial-day integrity checks passed")
