"""v58.9 (part 3) — tests for a real gap found from a data-driven
review: the only existing limit on spread exposure was a COUNT
(max_concurrent_spreads) — with dynamic sizing on, the system could
keep opening spreads up to that count as long as margin allowed,
potentially committing most of total capital to spreads regardless of
how many that represents, leaving little room for directional trades
even when they DO clear the regime/confluence gates.

This is deliberately a purely RISK-REDUCING addition (a new ceiling,
never encourages more risk-taking) — distinct from the separate,
genuine risk-appetite question of whether the regime/confluence gates
themselves should be loosened to allow more directional trades, which
is NOT addressed here since that's a values judgment, not a bug fix.

Run:  python3 test_spread_capital_cap.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
import agents
import config

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def make_analysis():
    return {"symbol": "FINNIFTY", "spot": 25988.85,
           "strikes": [{"strike": s, "ce": {"ltp": 50}, "pe": {"ltp": 50}}
                      for s in range(25800, 26300, 50)],
           "signal_lines": {"R": [{"level": 26100, "strength": 70, "color": "x"}],
                            "S": [{"level": 25800, "strength": 60, "color": "x"}]}}


print("1) the new key is registered on both DEFAULTS and SettingsIn, "
     "per the established discipline")
check("max_spread_capital_pct in config.DEFAULTS", "max_spread_capital_pct" in config.DEFAULTS)
app_src = open("app.py").read()
check("also declared on SettingsIn", "max_spread_capital_pct: float" in app_src)

real_market_open = agents.market_open
agents.market_open = lambda: True
_before = config.load()
config.save({"paper_mode": True, "auto_strategies": ["bear_call_spread"],
            "portfolio_halt_until": 0, "backtest_capital": 1000000,
            "margin_per_lot_spread": 85000, "max_spread_capital_pct": 60.0,
            "max_concurrent_spreads": 20})

try:
    print("\n2) spread margin already at/above the cap blocks NEW entries "
         "entirely, regardless of how few individual spreads that "
         "represents (the point: it's about capital fraction, not count)")
    bus = agents.Bus()
    ex = agents.ExecutionAgent(bus, {})
    bus.set("symbols", ["FINNIFTY"])
    # 8 lots * 85000 = 680,000 = 68% of 1,000,000 -> over the 60% cap,
    # via just ONE existing spread (well under the count cap of 20)
    bus.set("spreads", {"sp1": {"strategy": "bear_call_spread",
                                "symbol": "SENSEX", "lots": 8}})
    bus.set("analysis:FINNIFTY", make_analysis())
    bus.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus.set("chain_ts:FINNIFTY", time.time())
    ex._auto_spreads()
    check("blocked via capital_concentration, not max_concurrent (only "
          "1 of 20 spread slots used)",
          "'capital_concentration': 1" in ex.summary and
          "'max_concurrent': 0" in ex.summary, ex.summary)

    print("\n3) well under the cap proceeds to normal evaluation "
         "(the fix doesn't block everything)")
    bus2 = agents.Bus()
    ex2 = agents.ExecutionAgent(bus2, {})
    bus2.set("symbols", ["FINNIFTY"])
    # 1 lot * 85000 = 8.5% of capital -> well under 60%
    bus2.set("spreads", {"sp1": {"strategy": "bear_call_spread",
                                 "symbol": "SENSEX", "lots": 1}})
    bus2.set("analysis:FINNIFTY", make_analysis())
    bus2.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus2.set("chain_ts:FINNIFTY", time.time())
    ex2._auto_spreads()
    check("not blocked by the capital cap at 8.5% deployed",
          "'capital_concentration': 0" in ex2.summary, ex2.summary)

    print("\n4) zero spreads open at all is never blocked by this check "
         "(0% deployed is always under any positive cap)")
    bus3 = agents.Bus()
    ex3 = agents.ExecutionAgent(bus3, {})
    bus3.set("symbols", ["FINNIFTY"])
    bus3.set("spreads", {})
    bus3.set("analysis:FINNIFTY", make_analysis())
    bus3.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus3.set("chain_ts:FINNIFTY", time.time())
    ex3._auto_spreads()
    check("not blocked with zero spreads open",
          "'capital_concentration': 0" in ex3.summary, ex3.summary)

    print("\n5) the cap is genuinely configurable — a stricter cap "
         "blocks at a lower deployed fraction")
    config.save({"max_spread_capital_pct": 5.0})
    bus4 = agents.Bus()
    ex4 = agents.ExecutionAgent(bus4, {})
    bus4.set("symbols", ["FINNIFTY"])
    bus4.set("spreads", {"sp1": {"strategy": "bear_call_spread",
                                 "symbol": "SENSEX", "lots": 1}})   # 8.5%
    bus4.set("analysis:FINNIFTY", make_analysis())
    bus4.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus4.set("chain_ts:FINNIFTY", time.time())
    ex4._auto_spreads()
    check("a 5% cap correctly blocks at 8.5% deployed (same scenario "
          "that passed under the 60% cap in check 3)",
          "'capital_concentration': 1" in ex4.summary, ex4.summary)
finally:
    agents.market_open = real_market_open
    config.save(_before)

print("\n6) THE REAL BUG FOUND DURING A FULL REVIEW PASS: within a SINGLE "
     "cycle, the capital-concentration check must reflect spreads "
     "already entered EARLIER in that same cycle, not a stale snapshot "
     "taken once at the top — otherwise multiple symbols could each "
     "individually pass against the pre-cycle state and cumulatively "
     "blow past the cap")


def make_credit_analysis(sym, spot=105.0, wall=100.0):
    strikes = []
    for s in range(80, 121):
        pe_ltp = max(0.5, 20 - (spot - s) * 0.5)
        ce_ltp = max(0.5, 20 - (s - spot) * 0.5)
        strikes.append({"strike": float(s), "ce": {"ltp": ce_ltp, "security_id": f"{sym}{s}c"},
                        "pe": {"ltp": pe_ltp, "security_id": f"{sym}{s}p"}})
    return {"symbol": sym, "spot": spot, "strikes": strikes,
           "signal_lines": {"R": [], "S": [{"level": wall, "strength": 70, "color": "x"}]}}


config.save({"paper_mode": True, "auto_strategies": ["bull_put_spread"],
            "portfolio_halt_until": 0, "backtest_capital": 200000,
            "margin_per_lot_spread": 85000, "max_spread_capital_pct": 60.0,
            "max_concurrent_spreads": 20, "dynamic_sizing_enabled": False,
            "lots_per_trade": 1,
            "lot_sizes": {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65}})
real_market_open_2 = agents.market_open
agents.market_open = lambda: True
try:
    bus5 = agents.Bus()
    ex5 = agents.ExecutionAgent(bus5, {})
    bus5.set("positions", {})
    bus5.set("spreads", {})   # start from ZERO deployed
    bus5.set("symbols", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
        bus5.set(f"analysis:{sym}", make_credit_analysis(sym))
        bus5.set(f"regime:{sym}", {"regime": "rangebound"})
        bus5.set(f"chain_ts:{sym}", time.time())
    ex5._auto_spreads()
    spreads_after = bus5.get("spreads", {})
    entered_symbols = {sp["symbol"] for sp in spreads_after.values()}
    total_margin = sum(sp.get("margin_used", 0) for sp in spreads_after.values())
    check("exactly NIFTY and BANKNIFTY entered (each individually under "
          "60% at the time they were checked), FINNIFTY correctly "
          "excluded (would have pushed cumulative deployment well past "
          "60%, and the check correctly saw that FRESH state)",
          entered_symbols == {"NIFTY", "BANKNIFTY"}, str(entered_symbols))
    check("total deployed margin reflects exactly the two entries that "
          "were allowed, not a third that should have been blocked",
          total_margin == 170000, str(total_margin))
finally:
    agents.market_open = real_market_open_2
    config.save(_before)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
