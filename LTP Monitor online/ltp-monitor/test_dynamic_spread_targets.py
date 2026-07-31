"""v58.9 (part 4, item 5) — tests for dynamic, IV-based spread profit
targets, per explicit request: the flat ~10-18% capture is
conservative (keeps win rate high) but leaves real upside on the
table on days IV genuinely supports capturing more.

  - 20% on low IV
  - 30% on normal IV
  - 40-50% on elevated IV (50% only when the trend also looks stable —
    trending regime + ADX >= 25 — per the request's own wording)

Reuses risk_engine.iv_percentile(), which existed fully built and
tested but had ZERO callers anywhere in the codebase before this round
— this is that function's first actual use. Three-tier fallback:
percentile (best) -> absolute IV level -> flat configured default,
always producing something sensible rather than silently skipping IV
entirely.

Run:  python3 test_dynamic_spread_targets.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
import risk_engine
import config
import agents

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


cfg = {}

print("1) tier 1 (percentile-based): a genuinely LOW percentile reading "
     "gets the low-IV target")
# current IV of 8, against a history mostly ABOVE it -> genuinely low percentile
low_hist = [15, 16, 14, 18, 20, 17, 19, 22, 21, 16, 18, 15, 20, 19, 17,
           16, 18, 20, 15, 19, 17, 16, 18, 20]   # all well above 8
pctl_low = risk_engine.iv_percentile(8, low_hist, "24-day")
check("fixture actually produces a low percentile (<30) — sanity-check "
      "the fixture itself before testing the function",
      pctl_low["percentile"] < 30, str(pctl_low["percentile"]))
t, basis = risk_engine.dynamic_spread_profit_target_pct(cfg, 8, pctl_low, "rangebound", 15)
check("low percentile -> 20% target", t == 20.0, f"{t} | {basis}")

print("\n2) tier 1: a genuinely NORMAL percentile reading gets the "
     "normal-IV target")
normal_hist = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 15, 17, 19, 21] * 2
pctl_normal = risk_engine.iv_percentile(18, normal_hist, "28-day")
check("fixture produces a mid-range percentile (30-70)",
      30 <= pctl_normal["percentile"] <= 70, str(pctl_normal["percentile"]))
t2, basis2 = risk_engine.dynamic_spread_profit_target_pct(cfg, 18, pctl_normal, "mixed", 18)
check("normal percentile -> 30% target", t2 == 30.0, f"{t2} | {basis2}")

print("\n3) tier 1: elevated percentile + stable trend (trending regime, "
     "ADX >= 25) gets the TOP of the requested 40-50% range")
elevated_hist = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19] * 3
pctl_high = risk_engine.iv_percentile(28, elevated_hist, "30-day")
check("fixture produces a high percentile (>70)",
      pctl_high["percentile"] > 70, str(pctl_high["percentile"]))
t3, basis3 = risk_engine.dynamic_spread_profit_target_pct(
    cfg, 28, pctl_high, "trending-up", 30)
check("elevated + stable trend (trending-up, ADX 30) -> 50%",
      t3 == 50.0, f"{t3} | {basis3}")

print("\n4) tier 1: elevated percentile WITHOUT a stable trend gets the "
     "more conservative 40%, per the request's own wording "
     "('elevated AND trend is stable')")
t4, basis4 = risk_engine.dynamic_spread_profit_target_pct(
    cfg, 28, pctl_high, "rangebound", 30)
check("elevated but rangebound (not trending) -> 40%, not 50%",
      t4 == 40.0, f"{t4} | {basis4}")
t5, basis5 = risk_engine.dynamic_spread_profit_target_pct(
    cfg, 28, pctl_high, "trending-up", 15)   # trending but weak ADX
check("trending but ADX too weak (15 < 25) -> still 40%, not 50%",
      t5 == 40.0, f"{t5} | {basis5}")

print("\n5) tier 2 (absolute IV level fallback): used correctly when no "
     "percentile history exists yet (small sample size)")
thin_pctl = risk_engine.iv_percentile(28, [20, 22], "2-day (too thin)")
t6, basis6 = risk_engine.dynamic_spread_profit_target_pct(
    cfg, 28, thin_pctl, "trending-down", 26)
check("a thin percentile sample (n=2) is NOT trusted, falls through to "
      "absolute-level tier instead",
      t6 == 50.0 and "absolute level" in basis6, f"{t6} | {basis6}")
t7, basis7 = risk_engine.dynamic_spread_profit_target_pct(cfg, 10, None, None, None)
check("absolute subdued (IV<12) -> 20%", t7 == 20.0, f"{t7} | {basis7}")
t8, basis8 = risk_engine.dynamic_spread_profit_target_pct(cfg, 18, None, None, None)
check("absolute normal (12<=IV<=25) -> 30%", t8 == 30.0, f"{t8} | {basis8}")

print("\n6) tier 3 (flat fallback): no IV reading at all still returns "
     "something sensible, not a crash or None")
t9, basis9 = risk_engine.dynamic_spread_profit_target_pct(cfg, None, None, None, None)
check("no IV data at all falls back to the configured flat default",
      t9 == cfg.get("spread_profit_target_pct", 10.0) and "fixed" in basis9,
      f"{t9} | {basis9}")

print("\n7) config hygiene: new keys registered on both DEFAULTS and "
     "SettingsIn, per the established discipline; disabled by default "
     "(opt-in, doesn't silently change existing spread behavior)")
check("dynamic_spread_targets_enabled defaults to False (opt-in)",
      config.DEFAULTS.get("dynamic_spread_targets_enabled") is False)
for k in ("spread_target_low_iv_pct", "spread_target_normal_iv_pct",
         "spread_target_elevated_iv_pct", "spread_target_elevated_iv_stable_pct"):
    check(f"{k} registered in config.DEFAULTS", k in config.DEFAULTS)
app_src = open("app.py").read()
check("all five new keys declared on SettingsIn",
      all(f"{k}:" in app_src for k in (
          "dynamic_spread_targets_enabled", "spread_target_low_iv_pct",
          "spread_target_normal_iv_pct", "spread_target_elevated_iv_pct",
          "spread_target_elevated_iv_stable_pct")))

print("\n8) wired into enter_spread(): the computed target actually "
     "reaches the stored position dict, and the existing exit-check "
     "logic already reads THAT stored value (confirmed by source "
     "inspection, not assumed)")
agents_src = open("agents.py").read()
check("enter_spread computes target_pct via the new function when enabled",
      "risk_engine.dynamic_spread_profit_target_pct(" in agents_src)
check("the computed target_pct (not the flat config value directly) "
      "feeds profit_target on the stored position",
      '"profit_target": round(credit * target_pct / 100, 2)' in agents_src)
check("the basis is stored too, for later audit (why THIS trade got "
      "THIS target)",
      '"profit_target_pct": target_pct, "profit_target_basis": target_basis' in agents_src)
check("the exit-check logic already reads sp[\"profit_target\"] (the "
      "stored value) — confirms this wiring actually takes effect, "
      "not just computed and discarded",
      'if pnl_ps >= sp["profit_target"]' in agents_src)

print("\n9) full end-to-end through the REAL enter_spread() call, not "
     "just the pure function in isolation")
real_market_open = agents.market_open
agents.market_open = lambda: True
_before_cfg = config.load()
config.save({"paper_mode": True, "dynamic_spread_targets_enabled": True,
            "max_concurrent_spreads": 10, "margin_per_lot_spread": 85000,
            "backtest_capital": 1000000, "lot_sizes": {"NIFTY": 75}})
try:
    bus = agents.Bus()
    ex = agents.ExecutionAgent(bus, {})
    bus.set("positions", {})
    bus.set("spreads", {})
    bus.set("analysis:NIFTY", {"symbol": "NIFTY", "avg_iv": 28})
    bus.set("regime:NIFTY", {"regime": "trending-up", "adx": 30})
    spread_ev = {"eligible": True, "name": "bear_call_spread", "symbol": "NIFTY",
                "legs": [{"action": "SELL", "leg": "CE", "strike": 24000, "ltp": 50,
                         "security_id": "1"},
                        {"action": "BUY", "leg": "CE", "strike": 24100, "ltp": 30,
                         "security_id": "2"}],
                "credit": 20, "max_loss": 80, "width": 100, "short_strike": 24000,
                "regime": "trending-up"}
    result = ex.enter_spread(spread_ev)
    check("entry succeeds", "error" not in result, str(result))
    sp = list(bus.get("spreads", {}).values())[0] if bus.get("spreads") else {}
    check("real spread position shows the dynamic 50% target "
          "(elevated IV + stable trend), not the flat default",
          sp.get("profit_target_pct") == 50.0, str(sp.get("profit_target_pct")))
    check("profit_target itself (in rupees) reflects that percentage "
          "of the real credit (20 * 0.50 = 10.0)",
          sp.get("profit_target") == 10.0, str(sp.get("profit_target")))
finally:
    agents.market_open = real_market_open
    config.save(_before_cfg)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
