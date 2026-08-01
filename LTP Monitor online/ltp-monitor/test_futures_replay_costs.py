"""v59.0 Phase A §4.4 — costed replay on a hand-checkable series, and the
synthetic sanity that decides whether a negative result means anything.

The spec is explicit: "a perfectly trending series must make S11
profitable and a perfectly mean-reverting one must make S12 profitable.
If those fail, the harness is broken, not the strategy." Every strategy
lost money on the real data, so this test is what separates "these
strategies do not work" from "my harness does not work". Without it the
Phase A finding is unsupportable.
"""
import os, sys, math, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("reads candles and config")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import futures_replay as fr, futures_costs as fc, futures_strategies as fs

print("1) cost arithmetic on a hand-checkable trade")
LOT = 65
cost = fc.cost_round_trip("NIFTY", 24000, 24100, 1, lot=LOT)
print(f"     1 lot, 24000 -> 24100, lot {LOT}: cost ₹{cost:,.2f} "
      f"({cost/LOT:.2f} pts)")
check("cost is 8-11 points per round trip at this notional",
      8 <= cost / LOT <= 11, f"{cost/LOT:.2f}")
gross_pts = 100.0
net_pts = gross_pts - cost / LOT
check("a 100-point win nets ~90 points after cost",
      88 <= net_pts <= 92, f"{net_pts:.2f}")
check("net is ALWAYS below gross — costs are never optional",
      net_pts < gross_pts)

print("\n2) the replay applies cost to every trade, not to the total")
def mkbars(fn, n=375, base=24000.0, vol=1000):
    ts = int(dt.datetime(2026, 6, 15, 9, 15).timestamp())
    out = []
    for i in range(n):
        px = fn(i, base)
        out.append({"ts": ts + i * 60, "o": px, "h": px + 3, "l": px - 3,
                    "c": px, "v": vol})
    return out

import types
def run_on(bars_by_day, name, params=None, require_volume=False):
    """Drive replay_futures against injected sessions."""
    real = fr.load_sessions
    fr.load_sessions = lambda *a, **k: bars_by_day
    try:
        return fr.replay_futures("NIFTY", name, params, lot=LOT,
                                 require_volume=require_volume)
    finally:
        fr.load_sessions = real

trend = [(f"2026-06-{d:02d}", mkbars(lambda i, b: b + i * 1.5))
         for d in range(1, 21)]
r = run_on(trend, "s11_momentum")
m = r["metrics"]
if m.get("trades"):
    per = [t["gross"] - t["net"] for t in r["trades"]]
    check("every trade carries a positive cost", all(c > 0 for c in per),
          f"min ₹{min(per):.0f}")
    check("cost total equals the sum of per-trade costs",
          abs(m["cost_total"] - sum(t["cost"] for t in r["trades"])) < 0.01)
else:
    check("trending fixture produced trades", False, "0 trades")

print("\n3) SYNTHETIC SANITY — a broken harness fails here, not on real data")
r_trend = run_on(trend, "s11_momentum")
mt = r_trend["metrics"]
print(f"     S11 on a purely trending series: {mt.get('trades')} trades, "
      f"expectancy {mt.get('expectancy_points', 0):+.2f} pts, "
      f"win {mt.get('win_rate', 0):.0f}%")
check("S11 is PROFITABLE on a perfectly trending series",
      mt.get("expectancy_points", 0) > 0,
      f"{mt.get('expectancy_points', 0):+.3f} pts — if negative the harness is broken")

revert = [(f"2026-06-{d:02d}",
           mkbars(lambda i, b: b + math.sin(i / 6.0) * 60))
          for d in range(1, 21)]
r_rev = run_on(revert, "s12_vwap_reversion")
mr = r_rev["metrics"]
print(f"     S12 on a purely mean-reverting series: {mr.get('trades')} trades, "
      f"expectancy {mr.get('expectancy_points', 0):+.2f} pts, "
      f"win {mr.get('win_rate', 0):.0f}%")
check("S12 is PROFITABLE on a perfectly mean-reverting series",
      mr.get("expectancy_points", 0) > 0,
      f"{mr.get('expectancy_points', 0):+.3f} pts — if negative the harness is broken")

print("\n4) a bar spanning both stop and target is charged as the STOP")
px, why = fr._exit_price({"h": 110, "l": 90, "c": 100}, "LONG", 95, 105)
check("stop wins the ambiguous bar", why == "stop" and px == 95, f"{why} @ {px}")
px2, why2 = fr._exit_price({"h": 110, "l": 99, "c": 100}, "LONG", 95, 105)
check("a clean target bar still books the target", why2 == "target", str(why2))

print("\n5) metrics are computed on NET, never gross")
src = open("futures_replay.py").read()
check("expectancy uses net_points", 'nets = [t["net_points"] for t in trades]' in src)
check("win rate uses net", 't["net"] > 0' in src)
check("cost drag is reported", '"cost_drag_pct"' in src)
check("target-reach rate is computed from exit_reason",
      't["exit_reason"] == "target"' in src)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
