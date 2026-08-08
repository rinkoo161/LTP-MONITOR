#!/usr/bin/env python3
"""test_iv_tenor.py — an IV series must say what tenor it was taken at.

2026-08-09. `daily_atm_iv` stored (symbol, date, atm_iv) and nothing
else, so a reading taken 5 days from expiry and one taken 28 days out
were stored identically and compared as if equal. The difference between
them is term structure, not volatility.

It is not hypothetical. Measured on the first 36 rows:

    NIFTY      dte 2..7    (weekly only)
    BANKNIFTY  dte 2..28   26-day spread
    FINNIFTY   dte 2..28   26-day spread

Candidate A's entry gate (atm_iv - rv20 >= 3.0) reads that series.

TRUE CONSTANT-MATURITY INTERPOLATION IS NOT POSSIBLE HERE and no amount
of code changes that: it needs two expiries priced on the SAME day, and
`broker_adapter.option_chain()` fetches only `_nearest_expiry`. Measured:
0 of 40 archived days carry a second expiry. What IS possible is making
the series self-describing so a consumer can compare like with like.

AND THE MEASURED CONTAMINATION IS SMALL. near-vs-far gap is -0.74 vol
points on BANKNIFTY and +0.04 on FINNIFTY, an order of magnitude under
the 3.0 threshold. So this deliberately does NOT impose a hard tenor
band by default — that would discard roughly half of BANKNIFTY and
FINNIFTY to correct 0.7 vol points. The capability is provided; the
policy is left to the caller, on evidence.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_iv_tenor")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import history

HERE = os.path.dirname(os.path.abspath(__file__))

print("1) the series records its tenor")
cols = [r[1] for r in history._conn().execute("PRAGMA table_info(daily_atm_iv)")]
check("daily_atm_iv has days_to_expiry", "days_to_expiry" in cols, str(cols))

history.upsert_daily_atm_iv("TESTSYM", "2026-08-03", 12.0, 5)
history.upsert_daily_atm_iv("TESTSYM", "2026-08-04", 13.0, 6)
history.upsert_daily_atm_iv("TESTSYM", "2026-08-05", 20.0, 28)
history.upsert_daily_atm_iv("TESTSYM", "2026-08-06", 99.0)      # tenor unknown

rows = history.daily_atm_iv_rows("TESTSYM")
check("tenor round-trips", [r["days_to_expiry"] for r in rows] == [5, 6, 28, None],
      str([r["days_to_expiry"] for r in rows]))

print("\n2) a banded query returns ONLY comparable readings")
band = history.get_daily_atm_iv_history("TESTSYM", tenor_band=(2, 10))
check("the 28-day reading is excluded", 20.0 not in band, str(band))
check("the 5- and 6-day readings are kept", band == [12.0, 13.0], str(band))
# NOTE this one is guaranteed by SQL, not by our WHERE clause: NULL
# BETWEEN 2 AND 10 evaluates to NULL, which is falsy, so the row is
# excluded whether or not the explicit "IS NOT NULL" is present. The
# assertion is kept because the BEHAVIOUR matters, but mutating that
# clause away is correctly NOT detected — it is redundant, not
# load-bearing, and claiming otherwise would overstate the test.
check("and the UNKNOWN-tenor reading is excluded too", 99.0 not in band,
      f"{band} — an unlabelled reading cannot be shown to be comparable, "
      f"so it must not be silently treated as if it were")

print("\n3) the default is UNCHANGED, so no live reading moves silently")
allrows = history.get_daily_atm_iv_history("TESTSYM")
check("no band -> every row, tenor ignored (pre-2026-08-09 behaviour)",
      allrows == [12.0, 13.0, 20.0, 99.0], str(allrows))
check("risk_engine.iv_percentile's caller is not forced into a band",
      "tenor_band" not in open(os.path.join(HERE, "agents.py")).read()
      .split("get_daily_atm_iv_history(")[1][:60],
      "changing what an existing live caller receives, without being "
      "asked, would move a reading nobody requested")

print("\n4) the producer records the tenor it used")
RE = open(os.path.join(HERE, "risk_engine.py")).read()
body = RE.split("def backfill_iv_history(")[1]
body = body[:body.index("\ndef ")]
check("backfill_iv_history passes a tenor to upsert",
      "upsert_daily_atm_iv(symbol, day, atm_iv, _tenor)" in body,
      "a producer that writes an unlabelled row reintroduces the "
      "problem one day at a time")

print("\n5) the tenor question is answerable WITH A NUMBER")
rep = history.iv_tenor_report("TESTSYM")
check("iv_tenor_report reports the spread", rep.get("dte_spread") == 23,
      str(rep))
check("and the near-vs-far level gap", rep.get("term_structure_gap") is not None,
      f"{rep} — the size of the contamination is the whole question; "
      f"without it the decision is argued rather than measured")
check("an empty symbol degrades cleanly",
      history.iv_tenor_report("NOSUCHSYM").get("available") is False)

print("\n6) no hard band is imposed by default")
HS = open(os.path.join(HERE, "history.py")).read()
sig = HS.split("def get_daily_atm_iv_history(")[1].split(")")[0]
check("tenor_band defaults to None", "tenor_band=None" in sig, sig)
check("and the reasoning is recorded, not just the behaviour",
      "0.7" in HS or "-0.74" in HS,
      "the measured gap is what justifies NOT discarding half the "
      "sample; a future reader must be able to check that")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all IV tenor checks passed")
