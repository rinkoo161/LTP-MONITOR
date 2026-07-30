"""v54 — Strategies-page consolidated table backend tests: the enriched
/api/strategies/{symbol} payload (regime confluence/confidence, AI bias,
current_positions attribution) and the spread live/disabled status
fields, all via the real FastAPI TestClient.

Run:  python3 test_strategies_table.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import app
import config
import agents

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)
SYM = "NIFTY"


def seed_analysis():
    app.pilot.bus.set(f"analysis:{SYM}", {
        "symbol": SYM, "spot": 23800, "atm": 23800,
        "strikes": [{"strike": 23800, "ce": {"ltp": 120}, "pe": {"ltp": 115}}],
        "signal_lines": {"R": [], "S": []}})


print("1) enriched payload fields present")
seed_analysis()
app.pilot.bus.set(f"regime:{SYM}", {"regime": "trending-down", "confidence": 62,
                                    "confluence": "mixed-bear",
                                    "allowed_signals": ["BUY_PE"],
                                    "session_date": agents.now_ist().strftime("%Y-%m-%d"),
                                    "stale": False})
app.pilot.bus.set(f"bias:{SYM}", {"bias": "Neutral", "confidence": 12})
app.pilot.bus.set("spreads", {}); app.pilot.bus.set("positions", {})
app.pilot.bus.set("futures_positions", {})
d = client.get(f"/api/strategies/{SYM}").json()
check("no error", d.get("error") is None, str(d.get("error")))
check("regime_confidence present", d.get("regime_confidence") == 62)
check("regime_confluence present", d.get("regime_confluence") == "mixed-bear")
check("ai_bias present", d.get("ai_bias") == {"bias": "Neutral", "confidence": 12})
check("current_positions present with all 9 keys",
      set(d.get("current_positions", {}).keys()) ==
      {"bull_put_spread", "bear_call_spread", "mtf_confluence", "sg_ema",
       "futures_signal", "orb", "vwap_pullback", "ema_mtf", "momentum_buy"})
check("spreads carry live_enabled/manually_disabled fields",
      all("live_enabled" in s and "manually_disabled" in s for s in d["strategies"]),
      str(d["strategies"]))

print("\n2) current_positions attribution — each open position maps to "
      "its OWN strategy only")
app.pilot.bus.set("spreads", {f"{SYM}:bull_put_spread:23700": {
    "symbol": SYM, "strategy": "bull_put_spread", "credit": 25.5, "pnl": 340, "legs": []}})
app.pilot.bus.set("positions", {SYM: {"symbol": SYM, "setup": "sg_ema",
                                      "entry": 120, "pnl": -40}})
app.pilot.bus.set("futures_positions", {SYM: {"symbol": SYM, "side": "LONG",
                                              "entry": 23800, "pnl": 150}})
d = client.get(f"/api/strategies/{SYM}").json()
cp = d["current_positions"]
check("bull_put_spread attributed correctly",
      cp["bull_put_spread"] and cp["bull_put_spread"]["credit"] == 25.5)
check("bear_call_spread correctly EMPTY (different spread key prefix)",
      cp["bear_call_spread"] is None)
check("sg_ema attributed correctly (via the `setup` tag)",
      cp["sg_ema"] and cp["sg_ema"]["entry"] == 120)
check("mtf_confluence correctly EMPTY (position belongs to sg_ema, not mtf)",
      cp["mtf_confluence"] is None, str(cp["mtf_confluence"]))
check("futures_signal attributed correctly",
      cp["futures_signal"] and cp["futures_signal"]["side"] == "LONG")

# swap the tag and confirm attribution follows it, not a hardcoded name
app.pilot.bus.set("positions", {SYM: {"symbol": SYM, "setup": "mtf_confluence",
                                      "entry": 100, "pnl": 5}})
d = client.get(f"/api/strategies/{SYM}").json()
cp = d["current_positions"]
check("re-tagging the SAME position dict to mtf_confluence moves attribution there",
      cp["mtf_confluence"] is not None and cp["sg_ema"] is None,
      f"mtf={cp['mtf_confluence']} sg_ema={cp['sg_ema']}")

print("\n3) spread status fields reflect the persisted version file")
import backtester
v = backtester.load_versions()
# Snapshot whatever was really there before overwriting it for the test
# — a real user's container could have genuine accumulated backtest
# history for NIFTY by the time this test runs again, and a blind
# pop() at the end (this test's first draft) would silently DESTROY
# that instead of restoring it. None means "no prior entry existed".
_prior_nifty_entry = v.get("bull_put_spread", {}).get("symbols", {}).get(SYM)
v.setdefault("bull_put_spread", {}).setdefault("symbols", {})[SYM] = {
    "active": 1, "versions": [{"v": 1, "params": {}, "created": "x"}],
    "live_enabled": True, "manually_disabled": False}
backtester.save_versions(v)
d = client.get(f"/api/strategies/{SYM}").json()
row = next(s for s in d["strategies"] if s["name"] == "bull_put_spread")
check("live_enabled reflects the version file (True)", row["live_enabled"] is True)
check("manually_disabled reflects the version file (False)", row["manually_disabled"] is False)
v["bull_put_spread"]["symbols"][SYM]["manually_disabled"] = True
backtester.save_versions(v)
d = client.get(f"/api/strategies/{SYM}").json()
row = next(s for s in d["strategies"] if s["name"] == "bull_put_spread")
check("manually_disabled flips to True when the version file says so",
      row["manually_disabled"] is True)
# Restore exactly what was there before (or remove the key entirely if
# nothing was), rather than assuming it's always safe to delete.
if _prior_nifty_entry is None:
    v["bull_put_spread"]["symbols"].pop(SYM, None)
else:
    v["bull_put_spread"]["symbols"][SYM] = _prior_nifty_entry
backtester.save_versions(v)
check("real pre-existing version data (if any) was restored, not lost",
      backtester.load_versions().get("bull_put_spread", {}).get("symbols", {}).get(SYM)
      == _prior_nifty_entry)

print("\n4) strategy_docs.py covers every consolidated-table strategy")
import strategy_docs
for key in ("bull_put_spread", "bear_call_spread", "sg_ema", "mtf_confluence", "futures_signal"):
    doc = strategy_docs.DOCS.get(key)
    check(f"'{key}' has a docs entry with title/entry/exit/params",
          bool(doc and doc.get("title") and doc.get("entry") and
              doc.get("exit") and "params" in doc),
          str(list(doc.keys())) if doc else "MISSING")

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
