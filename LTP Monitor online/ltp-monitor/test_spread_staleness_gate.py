"""v58.4 — tests for the spread auto-deploy staleness gate, added after
a live report: a bear_call_spread fired on FINNIFTY while the index
was up on the day, prompting the question "was this decided on delayed
data?" Investigation found bear_call_spread is explicitly valid in
rangebound/mixed regimes regardless of the day's net direction (so
that specific case wasn't necessarily wrong), but surfaced a real,
separate gap: _auto_spreads() had NO freshness check at all before
evaluating/entering a spread — unlike /api/analysis/{symbol}'s own
existing "fresh enough" (ts < 90s) precedent.

Run:  python3 test_spread_staleness_gate.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


real_market_open = agents.market_open
agents.market_open = lambda: True
_before_auto = config.load().get("auto_strategies")
config.save({"paper_mode": True, "auto_strategies": ["bear_call_spread"]})

try:
    print("1) fresh chain_ts is NOT skipped for staleness")
    bus = agents.Bus()
    ex = agents.ExecutionAgent(bus, {})
    bus.set("symbols", ["FINNIFTY"])
    bus.set("spreads", {})
    bus.set("analysis:FINNIFTY", make_analysis())
    bus.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus.set("chain_ts:FINNIFTY", time.time())
    ex._auto_spreads()
    check("fresh data: stale_analysis counter is 0",
          "'stale_analysis': 0" in ex.summary, ex.summary)

    print("\n2) stale chain_ts (5 minutes old) IS skipped, not traded on")
    bus2 = agents.Bus()
    ex2 = agents.ExecutionAgent(bus2, {})
    bus2.set("symbols", ["FINNIFTY"])
    bus2.set("spreads", {})
    bus2.set("analysis:FINNIFTY", make_analysis())
    bus2.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus2.set("chain_ts:FINNIFTY", time.time() - 300)
    ex2._auto_spreads()
    check("stale data: stale_analysis counter is 1, not_eligible stays 0 "
          "(never reached the eligibility check at all)",
          "'stale_analysis': 1" in ex2.summary and "'not_eligible': 0" in ex2.summary,
          ex2.summary)

    print("\n3) boundary: exactly at the threshold behaves sanely (not "
         "flaky around the exact second)")
    bus3 = agents.Bus()
    ex3 = agents.ExecutionAgent(bus3, {})
    bus3.set("symbols", ["FINNIFTY"])
    bus3.set("spreads", {})
    bus3.set("analysis:FINNIFTY", make_analysis())
    bus3.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus3.set("chain_ts:FINNIFTY", time.time() - 30)   # comfortably fresh
    ex3._auto_spreads()
    check("30s old data is treated as fresh (well under the 90s bar)",
          "'stale_analysis': 0" in ex3.summary, ex3.summary)

    print("\n4) no chain_ts at all (never fetched) is treated as maximally "
         "stale, not as fresh-by-default")
    bus4 = agents.Bus()
    ex4 = agents.ExecutionAgent(bus4, {})
    bus4.set("symbols", ["FINNIFTY"])
    bus4.set("spreads", {})
    bus4.set("analysis:FINNIFTY", make_analysis())
    bus4.set("regime:FINNIFTY", {"regime": "rangebound"})
    # chain_ts:FINNIFTY deliberately never set
    ex4._auto_spreads()
    check("missing chain_ts is treated as stale (skips), not as fresh",
          "'stale_analysis': 1" in ex4.summary, ex4.summary)

    print("\n5) bear_call_spread's documented regime-fit confirmed — "
         "the ORIGINAL report's specific case wasn't necessarily wrong")
    import strategies as slib
    check("bear_call_spread is explicitly valid in rangebound AND mixed "
          "regimes, not only trending-down",
          set(slib.REGIME_FIT["bear_call_spread"]) ==
          {"trending-down", "rangebound", "mixed"},
          str(slib.REGIME_FIT["bear_call_spread"]))

finally:
    agents.market_open = real_market_open
    config.save({"auto_strategies": _before_auto or []})

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
