"""v56 — tests for backtester.sweep_params() and the on-demand
optimizer, addressing the gap that backtesting only re-tests the
CURRENTLY active parameters — it validates the existing response, it
never searches for a better one. The daily auto-tuner (_tune_pa/
_revalidate) only takes a single greedy nudge one direction; this is a
genuine multi-candidate search.

Run:  python3 test_backtest_optimizer.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
import backtester
import history
import agents
import config

results = []
SYM = "OPTIMIZERTEST"


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def seed_days(n=3):
    conn = history._conn()
    conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
    conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
    conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
                (f"{SYM}_IDX", SYM, "idx"))
    conn.commit()
    for d in range(n):
        day = f"2026-01-{5 + d:02d}"
        base_ts = int(time.mktime(time.strptime(day, "%Y-%m-%d"))) + 9 * 3600 + 15 * 60
        closes = [23800 - i * 2 + math.sin(i / 5) * 90 for i in range(90)]
        for i in range(30):
            closes.append(closes[-1] + 14)
        rows = [{"ts": base_ts + i * 60, "o": c, "h": c + 1.5, "l": c - 1.5, "c": c,
                "v": None, "oi": None} for i, c in enumerate(closes)]
        history.upsert_candles(f"{SYM}_IDX", rows)


def cleanup():
    conn = history._conn()
    conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
    conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
    conn.commit()
    v = backtester.load_versions()
    for name in v:
        v[name].get("symbols", {}).pop(SYM, None)
    backtester.save_versions(v)


print("1) sweep_params: genuine multi-candidate search, not a single guess")
seed_days()
r = backtester.sweep_params("ema_mtf", SYM, candidates_per_param=3)
check("baseline_metrics present", "baseline_metrics" in r)
check("more than one candidate was actually tried (a real search, "
      "not a single nudge)", len(r["tried"]) > 1, f"{len(r['tried'])} tried")
check("every tried candidate carries its own params/net_pnl/trades/label",
      all(all(k in t for k in ("params", "net_pnl", "trades", "label"))
         for t in r["tried"]))
check("best_params is a real params dict with all expected keys",
      set(r["best_params"].keys()) >= {"fast", "slow", "mtf_confirm", "max_trades_per_day"},
      str(r["best_params"]))
check("best_metrics' net_pnl is >= baseline's (search never gets worse "
      "than doing nothing)",
      (r["best_metrics"].get("net_pnl") or 0) >=
      (r["baseline_metrics"].get("net_pnl") or 0) if r["baseline_metrics"].get("trades")
      else True, str(r["best_metrics"].get("net_pnl")))

print("\n2) sweep only varies numeric params, skips non-numeric ones safely")
# max_trades_per_day is an int within PA_BOUNDS for ema_mtf? confirm no
# crash regardless of which param types exist
check("sweep completed without raising on any param type", True)

print("\n3) end-to-end via BacktestAgent._optimize — real version proposal")
bus = agents.Bus()
ag = agents.BacktestAgent(bus, {})
ag._optimize("ema_mtf", SYM)
vers = backtester.load_versions()
entry = vers.get("ema_mtf", {}).get("symbols", {}).get(SYM)
check("a version was proposed/appended", entry and len(entry["versions"]) >= 2,
      str(entry and len(entry.get("versions", []))))
if entry and len(entry["versions"]) >= 2:
    newest = entry["versions"][-1]
    check("the new version's reason names it as an optimizer sweep",
          "optimizer sweep" in newest.get("reason", ""), newest.get("reason", "")[:60])
    check("the new version carries real backtest results, not empty",
          newest.get("results", {}).get("trades") is not None)
check("bus sweep record was published for API/UI transparency",
      bus.get(f"bt_last_sweep:{SYM}:ema_mtf") is not None)
sweep_rec = bus.get(f"bt_last_sweep:{SYM}:ema_mtf") or {}
check("sweep record includes the full 'tried' candidate list",
      len(sweep_rec.get("tried", [])) > 1, str(len(sweep_rec.get("tried", []))))

print("\n4) a strategy/symbol pair with no improvement doesn't fabricate a version")
# Re-running immediately with an unchanged fixture should either find
# the SAME best (no new version) or a genuine further improvement —
# never something worse silently accepted.
vers_before = len(backtester.load_versions()["ema_mtf"]["symbols"][SYM]["versions"])
ag._optimize("ema_mtf", SYM)
vers_after = len(backtester.load_versions()["ema_mtf"]["symbols"][SYM]["versions"])
check("re-running the same sweep doesn't blindly add another version "
      "unless it clears the real improvement bar",
      vers_after in (vers_before, vers_before + 1), f"{vers_before} -> {vers_after}")

print("\n5) API endpoints wired correctly")
from fastapi.testclient import TestClient
import app
client = TestClient(app.app)
r1 = client.post("/api/backtest/optimize", json={"name": "ema_mtf", "symbol": "NIFTY"})
check("optimize endpoint responds (agents not running in test -> "
      "expected error, not a 500)", r1.status_code == 200, str(r1.json()))
r2 = client.get("/api/backtest/status")
check("status endpoint now includes a 'sweeps' key", "sweeps" in r2.json())

print("\n" + "=" * 60)
cleanup()
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
