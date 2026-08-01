"""v58.76 — one reader for two journal shapes.

The journal holds two record shapes. Options and spreads use
`stoploss` / `target1` / `qty`; a futures trade is the live position
dict, so the same facts are `initial_sl` / `sl` / `target` and
`lots` x `lot_size`. Both are COMPLETE.

A reader that knows only one shape sees None and concludes the data is
missing — which is exactly what happened on 2026-08-01: the forensic
analysis read `stoploss`, found None on all 19 futures trades, and
reported that futures records "cannot reconstruct risk". They always
could; `sl` was 57266.82 in the same record. The replay then fell back
to scraping stop prices out of the exit-reason TEXT, which worked for
only 3 of 19 trades — so a conclusion about 19 trades rested on 3.

That is this project's recurring failure in its purest form: reading a
SHAPE rather than the MEANING. `trade_risk_fields()` is the single
reader, and these checks hold it to both shapes.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("reads the trade journal")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents

FUT = {"symbol": "BANKNIFTY", "kind": "future", "side": "LONG", "lots": 8,
       "lot_size": 30, "entry": 57338.0, "sl": 57266.82, "initial_sl": 57260.83,
       "target": 57479.48, "ltp": 57264.0, "pnl": -18240.0, "atr_at_entry": 51.4}
OPT = {"symbol": "NIFTY", "leg": "CE", "strike": 24350.0, "qty": 375, "lots": 5,
       "entry": 102.45, "stoploss": 96.0, "target1": 163.2, "ltp": 96.85,
       "pnl": -2400.0}
SPREAD = {"symbol": "BANKNIFTY", "leg": "SPREAD", "strategy": "bear_call_spread",
          "qty": 150, "lots": 5, "entry": 79.1, "stoploss": -79.1,
          "target1": 27.68, "ltp": -3.5, "pnl": -360.0}

print("1) a futures record resolves completely")
f = agents.trade_risk_fields(FUT)
check("classified as futures", f["kind"] == "futures", f["kind"])
check("entry", f["entry"] == 57338.0)
check("stop comes from initial_sl, NOT the ratcheted sl",
      f["stop"] == 57260.83,
      "sl may have been moved by the trail; sizing used the entry stop")
check("target", f["target"] == 57479.48)
check("qty is lots x lot_size", f["qty"] == 240, str(f["qty"]))
check("side is read, not inferred", f["side"] == "LONG")
_risk = abs(f["entry"] - f["stop"]) * f["qty"]
check("risk is now computable end to end", round(_risk) == 18521, f"₹{_risk:,.0f}")

print("\n2) an option record resolves through the SAME function")
o = agents.trade_risk_fields(OPT)
check("classified as option", o["kind"] == "option", o["kind"])
check("stop from stoploss", o["stop"] == 96.0)
check("target from target1", o["target"] == 163.2)
check("qty as stored", o["qty"] == 375)
check("side inferred from the P&L sign (options store none)",
      o["side"] == "LONG", str(o["side"]))

print("\n3) spreads too")
s = agents.trade_risk_fields(SPREAD)
check("classified as spread", s["kind"] == "spread", s["kind"])
# 2026-08-02 — this asserted `s["stop"] == -79.1`, i.e. it PINNED the bug.
# A negative stop price is impossible for anything tradeable; what was
# stored was a P&L-per-share floor in a field meaning price. The reader
# now converts legacy spread rows onto the spread-value basis the writer
# uses: value at the stop = credit + loss_limit = 79.1 + 79.1.
check("legacy negative stop is converted to a real price",
      s["stop"] == 158.2, f"{s['stop']} (79.1 credit + 79.1 loss limit)")
check("and the reconstructed risk equals the original loss limit",
      abs((s["stop"] - s["entry"]) - 79.1) < 1e-9,
      "this is what the field is FOR — sizing must be recoverable")
check("target converted onto the same basis",
      s["target"] == round(79.1 - 27.68, 2), str(s["target"]))
check("qty as stored", s["qty"] == 150)

print("\n4) the specific bug: reading the wrong key must not look like missing data")
check("the futures record HAS a stop under its own name",
      FUT.get("stoploss") is None and FUT.get("initial_sl") is not None,
      "reading 'stoploss' here returns None on complete data")
check("the normaliser finds it anyway",
      agents.trade_risk_fields(FUT)["stop"] is not None)

print("\n5) real journal coverage")
import json
p = os.path.expanduser("~/.ltp-monitor/trades.jsonl")
if os.path.exists(p):
    rows = [json.loads(l) for l in open(p) if l.strip()]
    fut = [t for t in rows if agents.trade_class(t) == "futures"]
    got = [t for t in fut if agents.trade_risk_fields(t)["stop"] is not None]
    print(f"     {len(got)} of {len(fut)} futures trades resolve a stop")
    check("the normaliser resolves a stop for most real futures trades",
          not fut or len(got) >= len(fut) // 2,
          f"{len(got)}/{len(fut)} — older records predate initial_sl")
else:
    check("no live journal in an isolated store — nothing to sample", True)

print("\n6) degenerate input never raises")
for bad in ({}, {"entry": None}, {"kind": "future"}, {"leg": "CE"}):
    try:
        agents.trade_risk_fields(bad)
        ok = True
    except Exception as e:
        ok = False
        print("    raised:", e)
    check(f"handles {str(bad)[:26]}", ok)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
