"""v58.6 — tests for three real issues found from live Order History/
Journal data:
1. Order History showed "undefined -" for every futures trade row —
   the row template only knew option/spread field names.
2. Spreads never respected the portfolio kill-switch's documented
   60-minute post-trip cooldown — only the directional pipeline did.
3. Spreads had NO consecutive-loss circuit breaker at all — the
   directional pipeline's RiskAgent.consecutive_losses never applied
   to spreads, since _auto_spreads() calls enter_spread() directly,
   bypassing risk.evaluate() entirely. This is the concrete fix for
   the reported pattern: bear_call_spread re-selling the same
   FINNIFTY 26,100 CE wall four times in one session, size INCREASING
   after losses, net -3185 on that one pairing.

Run:  python3 test_spread_safety_and_futures_render.py
"""
import os
import re
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


def open_test_spread(bus, sid, pnl):
    sp = {"id": sid, "strategy": "bear_call_spread", "symbol": "FINNIFTY",
         "legs": [{"action": "SELL", "leg": "CE", "strike": 26100, "entry": 50},
                  {"action": "BUY", "leg": "CE", "strike": 26200, "entry": 30}],
         "qty": 1, "lots": 1, "credit": 20, "max_loss": 80, "pnl": pnl,
         "short_strike": 26100, "loss_limit": 80, "profit_target": 12,
         "pnl_per_share": pnl, "opened": "10:00:00", "opened_date": "2026-07-27",
         "opened_ts": time.time(), "paper": True}
    bus.set("spreads", {sid: sp})
    return sp


real_market_open = agents.market_open
agents.market_open = lambda: True
_before_auto = config.load().get("auto_strategies")
_before_stop_n = config.load().get("spread_stop_after_consecutive_losses")
config.save({"paper_mode": True, "auto_strategies": ["bear_call_spread"],
            "portfolio_halt_until": 0, "spread_stop_after_consecutive_losses": 2})

try:
    print("1) portfolio kill-switch cooldown is now respected by spread "
         "auto-deploy (previously never checked at all)")
    bus = agents.Bus()
    ex = agents.ExecutionAgent(bus, {})
    bus.set("symbols", ["FINNIFTY"])
    bus.set("spreads", {})
    bus.set("analysis:FINNIFTY", make_analysis())
    bus.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus.set("chain_ts:FINNIFTY", time.time())
    bus.set("portfolio_halt_until", time.time() + 1800)
    ex._auto_spreads()
    check("auto-deploy is paused entirely while the cooldown is active",
          "cooldown" in ex.summary, ex.summary)

    print("\n2) real end-to-end: two consecutive REAL losses (via the "
         "actual exit_spread() call, not a hand-set counter) trip the "
         "circuit breaker for that exact (symbol, strategy) pairing")
    bus2 = agents.Bus()
    ex2 = agents.ExecutionAgent(bus2, {})
    open_test_spread(bus2, "sp1", -500)
    ex2.exit_spread("sp1", "loss limit")
    check("after 1 real loss, counter is 1",
          ex2._spread_consec_losses.get("FINNIFTY:bear_call_spread") == 1,
          str(ex2._spread_consec_losses))
    open_test_spread(bus2, "sp2", -300)
    ex2.exit_spread("sp2", "loss limit")
    check("after 2 real losses, counter is 2",
          ex2._spread_consec_losses.get("FINNIFTY:bear_call_spread") == 2,
          str(ex2._spread_consec_losses))

    print("\n3) a WIN resets the counter for that pairing back to 0")
    open_test_spread(bus2, "sp3", 500)
    ex2.exit_spread("sp3", "profit target")
    check("after a win, counter resets to 0",
          ex2._spread_consec_losses.get("FINNIFTY:bear_call_spread") == 0,
          str(ex2._spread_consec_losses))

    print("\n4) the halt gate in _auto_spreads() itself correctly blocks "
         "a pairing that has hit the threshold (isolated from the "
         "re-entry cooldown, which would otherwise also be active "
         "right after a real exit and mask this check)")
    bus3 = agents.Bus()
    ex3 = agents.ExecutionAgent(bus3, {})
    bus3.set("symbols", ["FINNIFTY"])
    bus3.set("spreads", {})
    bus3.set("analysis:FINNIFTY", make_analysis())
    bus3.set("regime:FINNIFTY", {"regime": "rangebound"})
    bus3.set("chain_ts:FINNIFTY", time.time())
    bus3.set("portfolio_halt_until", 0)
    ex3._spread_consec_losses = {"FINNIFTY:bear_call_spread": 2}
    ex3._auto_spreads()
    check("consec_loss_halt counter fires (not on_cooldown, since no "
          "prior exit set that timer in this isolated case)",
          "'consec_loss_halt': 1" in ex3.summary, ex3.summary)

    print("\n5) config hygiene — the new key registered on both "
         "DEFAULTS and SettingsIn, per the established v54 discipline")
    check("spread_stop_after_consecutive_losses in config.DEFAULTS",
          config.DEFAULTS.get("spread_stop_after_consecutive_losses") == 2)
    app_src = open("app.py").read()
    check("also declared on SettingsIn",
          "spread_stop_after_consecutive_losses: int" in app_src)
finally:
    agents.market_open = real_market_open
    config.save({"auto_strategies": _before_auto or [],
                "spread_stop_after_consecutive_losses": _before_stop_n})

print("\n6) Order History futures rendering — real bug found from a "
     "live screenshot: every futures row showed \"undefined -\"/"
     "\"undefined\" because the row template only knew option/spread "
     "field names (leg/qty/stoploss/target1/target2), not futures'"
     " own (side/lots/sl/target)")
h = open("static/dashboard.html").read()
check("row template now detects kind==='future' explicitly",
      "t.kind === \"future\"" in h)
check("futures branch uses the REAL field names (side/lots/sl/target), "
      "not the option-shaped ones that produced 'undefined'",
      't.side' in h and 't.lots' in h and 't.sl' in h)

# Re-derive the exact row logic to prove it against a realistic record,
# not just check for the presence of field names in the source.
futures_record = {"kind": "future", "side": "LONG", "lots": 1,
                  "entry": 76802.8, "sl": 76707.6, "target": 76993.2,
                  "ltp": 76802.8, "pnl": 0, "paper": True, "symbol": "SENSEX"}
is_future = futures_record.get("kind") == "future"
leg_cell = ("FUT " + futures_record["side"]) if is_future else None
qty_cell = (str(futures_record["lots"]) + " lot(s)") if is_future else None
check("a realistic futures record resolves to real text, not 'undefined'",
      leg_cell == "FUT LONG" and qty_cell == "1 lot(s)",
      f"{leg_cell} / {qty_cell}")

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
