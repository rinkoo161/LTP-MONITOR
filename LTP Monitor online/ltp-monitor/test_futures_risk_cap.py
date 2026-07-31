"""v58.73 — the per-trade futures risk cap must bind on EVERY entry path.

From the 2026-07-30 journal, against a daily_loss_limit of ₹20,000:

    BANKNIFTY  8 lots   -₹18,240   91% of the DAILY limit in ONE trade
    FINNIFTY   4 lots   -₹15,840   79%
    FINNIFTY   6 lots   -₹12,840   64%

19 futures trades lost ₹73,115 that session; the spread book made
₹1,202. `futures_risk_per_trade_rupees` was already set to ₹2,500 and
`sizing.cap_by_rupee_risk()` already existed — but the cap was applied
inside `sizing.size_future()`, which is one of THREE entry paths.
/api/futures/enter passes request lots straight through and never
reached it.

Two structural points this pins down:

  1. A limit enforced per-caller is not a limit. It now lives in
     `enter_future()`, the single function all three paths call.
  2. The stop was computed ~40 lines BELOW the order placement, so size
     could not be checked against the risk it implied until the position
     already existed. Sizing a position you have already bought is not
     sizing. The geometry now precedes the order.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config and opens paper futures positions")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents, config, sizing

CAP = 2500
config.save({"futures_risk_per_trade_rupees": CAP, "paper_mode": True,
             "futures_live_enabled": False, "backtest_capital": 5_000_000,
             "margin_per_lot_future": 110000, "futures_stop_mode": "pct",
             "futures_sl_pct": 0.4})


def agent(symbol="BANKNIFTY", ltp=57338.0):
    bus = agents.Bus()
    ex = agents.ExecutionAgent(bus, {"get_chain": lambda s: None,
                                     "orders_factory": lambda: None})
    bus.set(f"future_ohlc:{symbol}", {"close": ltp})
    return bus, ex


def risk_of(cfg, symbol, ltp):
    """Rupee risk of one lot at the CONFIGURED stop — read it from cfg
    rather than restating 0.4, or the helper silently disagrees with the
    code under test the moment a section changes the setting."""
    lot = cfg["lot_sizes"].get(symbol, 75)
    sl_pct = cfg.get("futures_sl_pct", 0.4)
    sl = round(ltp * (1 - sl_pct / 100), 2)
    return abs(ltp - sl) * lot


cfg = config.load()
real_open = agents.market_open
agents.market_open = lambda: True
try:
    print("1) the exact 2026-07-30 trade: BANKNIFTY 8 lots")
    per_lot = risk_of(cfg, "BANKNIFTY", 57338.0)
    print(f"     risk/lot at a 0.4% stop = ₹{per_lot:,.0f}; cap = ₹{CAP:,}")
    bus, ex = agent()
    r = ex.enter_future("BANKNIFTY", "LONG", lots=8)
    took = (bus.get("futures_positions") or {}).get("BANKNIFTY")
    if per_lot > CAP:
        check("refused outright — one lot already breaches the cap",
              "error" in r and "per-trade risk cap" in str(r.get("error")),
              str(r)[:90])
        check("and no position was opened", took is None)
    else:
        check("sized down to what the cap allows",
              took and took["lots"] * per_lot <= CAP, str(took))

    print("\n2) a stop tight enough to permit size gets sized DOWN, not refused")
    # 24000 with a 0.4% stop on a 75-lot: 96pts x 75 = ₹7,200/lot -> still
    # over. Use a symbol/stop where several lots fit under the cap.
    config.save({"futures_sl_pct": 0.02})   # 4.8 pts on NIFTY -> ₹360/lot
    cfg2 = config.load()
    per_lot2 = risk_of(cfg2, "NIFTY", 24000.0)
    bus2, ex2 = agent("NIFTY", 24000.0)
    r2 = ex2.enter_future("NIFTY", "LONG", lots=50)
    pos2 = (bus2.get("futures_positions") or {}).get("NIFTY")
    allowed = int(CAP // per_lot2)
    check(f"50 lots reduced to the cap's allowance ({allowed})",
          pos2 and pos2["lots"] == allowed,
          f"risk/lot ₹{per_lot2:,.0f}, got {pos2 and pos2['lots']}")
    check("resulting risk is within the cap",
          pos2 and pos2["lots"] * per_lot2 <= CAP,
          f"₹{(pos2 or {}).get('lots', 0) * per_lot2:,.0f}")
    check("the reduction is logged, not silent",
          any("sized DOWN" in l for l in bus2.feed),
          next((l[-60:] for l in bus2.feed if "sized DOWN" in l), "NOTHING LOGGED"))

    print("\n3) the cap is off when set to 0 (explicit opt-out only)")
    config.save({"futures_risk_per_trade_rupees": 0})
    bus3, ex3 = agent("NIFTY", 24000.0)
    r3 = ex3.enter_future("NIFTY", "LONG", lots=3)
    pos3 = (bus3.get("futures_positions") or {}).get("NIFTY")
    check("with the cap disabled the requested size stands",
          pos3 and pos3["lots"] == 3, str(pos3 and pos3.get("lots")))
    config.save({"futures_risk_per_trade_rupees": CAP})
finally:
    agents.market_open = real_open

print("\n4) it binds at the shared choke point, before any order")
code = [l.split("#", 1)[0] for l in open("agents.py").read().splitlines()]
i = next(n for n, l in enumerate(code) if "def enter_future" in l)
window = code[i:i + 190]
def line_of(needle):
    return next((n for n, l in enumerate(window) if needle in l), 10**6)
check("the cap is applied inside enter_future()",
      line_of("_sz.cap_by_rupee_risk(") < 10**6)
check("stop geometry is computed BEFORE the cap",
      line_of("_sl_px = round") < line_of("_sz.cap_by_rupee_risk("))
check("the cap runs BEFORE the live order is placed",
      line_of("_sz.cap_by_rupee_risk(") < line_of("orders.place("))
check("...and before the margin check, so size is final by then",
      line_of("_sz.cap_by_rupee_risk(") < line_of("insufficient margin"))

print("\n5) every entry path goes through it")
ag = open("agents.py").read()
ap = open("app.py").read()
check("auto-deploy path calls enter_future", "self.enter_future(sym, ev[\"side\"]" in ag)
check("/api/futures/enter calls enter_future", "ex.enter_future(body.symbol.upper()" in ap)
check("/api/futures/manual_deploy calls enter_future",
      "ex.enter_future(sym, ev[\"side\"], ev[\"lots\"])" in ap)
check("no path constructs a futures position without it",
      ag.count('"kind": "future"') == 1, str(ag.count('"kind": "future"')))

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
