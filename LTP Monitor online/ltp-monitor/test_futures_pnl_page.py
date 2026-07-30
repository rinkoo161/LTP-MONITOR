"""v58.8 — tests for a real bug found from a live report: futures
positions were completely absent from /api/trades — both the Open
Position(s) display on the P&L page AND the Unrealized P&L calculation
itself silently excluded any open futures position's pnl. Same class
of gap already fixed for spreads on 2026-07-24, never caught when
futures positions were added later.

Run:  python3 test_futures_pnl_page.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import app

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)

print("1) a real open futures position now appears in /api/trades")
app.pilot.bus.set("futures_positions", {"NIFTY": {
    "symbol": "NIFTY", "kind": "future", "side": "LONG", "lots": 1,
    "lot_size": 75, "entry": 24012.9, "sl": 23916.85, "target": 24205.0,
    "ltp": 24017.1, "pnl": 307.5, "margin": 110000, "opened": "12:23:03",
    "opened_date": "2026-07-27", "paper": True}})
app.pilot.bus.set("closed_trades", [])
app.pilot.bus.set("positions", {})
app.pilot.bus.set("spreads", {})

d = client.get("/api/trades").json()
check("response includes an 'open_futures' key",
      "open_futures" in d)
check("the actual position data is present, not empty",
      d.get("open_futures", {}).get("NIFTY", {}).get("pnl") == 307.5,
      str(d.get("open_futures")))

print("\n2) Unrealized P&L now includes the futures position's pnl "
     "(previously silently excluded — a real stats-accuracy bug, not "
     "just a display gap)")
check("stats.unrealized_pnl includes the futures pnl (307.5)",
      d["stats"]["unrealized_pnl"] == 307.5, str(d["stats"]["unrealized_pnl"]))

print("\n3) with a spread ALSO open, unrealized P&L correctly sums "
     "across all three position types (options + spreads + futures)")
app.pilot.bus.set("positions", {"NIFTY": {"pnl": 100}})
app.pilot.bus.set("spreads", {"sp1": {"pnl": -50}})
d2 = client.get("/api/trades").json()
check("unrealized = option(100) + spread(-50) + futures(307.5) = 357.5",
      abs(d2["stats"]["unrealized_pnl"] - 357.5) < 0.01,
      str(d2["stats"]["unrealized_pnl"]))

print("\n4) no open futures position -> the key is present but empty, "
     "not missing or erroring")
app.pilot.bus.set("futures_positions", {})
app.pilot.bus.set("positions", {})
app.pilot.bus.set("spreads", {})
d3 = client.get("/api/trades").json()
check("open_futures is an empty dict when nothing is open",
      d3.get("open_futures") == {}, str(d3.get("open_futures")))
check("unrealized_pnl is 0 with nothing open",
      d3["stats"]["unrealized_pnl"] == 0, str(d3["stats"]["unrealized_pnl"]))

print("\n5) frontend renders the new futures table")
h = open("static/dashboard.html").read()
check("dashboard reads d.open_futures", "d.open_futures" in h)
check("renders side (LONG/SHORT), lots, entry, SL, target, margin, P&L",
      all(s in h for s in ["f.side", "f.lots", "f.entry", "f.sl", "f.target",
                           "f.margin", "f.pnl"]))

# cleanup
app.pilot.bus.set("futures_positions", {})

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
