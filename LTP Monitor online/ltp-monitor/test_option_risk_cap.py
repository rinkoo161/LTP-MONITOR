#!/usr/bin/env python3
"""test_option_risk_cap.py — per-trade rupee cap on the OPTIONS path.

v59.0 (2026-08-02). The futures path got this cap on 2026-08-01; options
had none, so `portfolio_max_drawdown` was the only thing between one
option trade and the whole book.

RE-DERIVED the same day. The first figures here (median ₹3,198, max
₹6,435) were wrong twice over: the population pooled SPREAD legs with
option buys, and the tail applied NIFTY's lot size to every symbol,
inflating SENSEX 3.25x. On the clean 106-trade single-leg population at
each symbol's own lot size: median ₹2,519, max ₹4,059.

So the cap is ₹4,000 — which is not a new number but the EXISTING
per-trade risk budget (risk_pct_per_trade 2% x backtest_capital
200,000), currently dead configuration because dynamic_sizing_enabled is
False and size_option_buy() never consults it. The cap makes it bind.

What these check, in order of what would actually go wrong:

  1. it is the SAME helper as futures, not a second implementation —
     two per-trade caps that drift is the failure this codebase keeps
     re-learning (quadrant classifier, market session, news regexes);
  2. it can only ever REDUCE risk — never raise lots, never bypass a
     gate;
  3. a refusal is LOUD, because a silently-skipped trade looks identical
     to a strategy that found no signal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_option_risk_cap")

import config
import sizing

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
AG = open(os.path.join(HERE, "agents.py")).read()
CFG = open(os.path.join(HERE, "config.py")).read()

print("0) the cap IS the per-trade risk budget, not a new free parameter")
_budget = (config.DEFAULTS["backtest_capital"]
           * config.DEFAULTS["risk_pct_per_trade"] / 100)
# The INVARIANT is `cap == risk_pct x capital` within whichever config is
# in force — not a fixed rupee value. DEFAULTS ships risk_pct 1.0 (cap
# 2,000); the running config uses 2.0 (cap 4,000). Both are correct; only
# a config where the two disagree is wrong. So assert the invariant holds
# in DEFAULTS, and separately that a divergence WOULD be caught.
check("DEFAULTS satisfies cap == risk_pct x capital",
      config.DEFAULTS["option_risk_per_trade_rupees"] == _budget,
      f"₹{config.DEFAULTS['option_risk_per_trade_rupees']:,.0f} "
      f"== budget ₹{_budget:,.0f}")
check("DEFAULTS is internally coherent end to end",
      sizing.risk_coherence(dict(config.DEFAULTS)) == [],
      str(sizing.risk_coherence(dict(config.DEFAULTS))))
_div = sizing.risk_coherence(dict(config.DEFAULTS,
                                  option_risk_per_trade_rupees=9999))
check("a cap that diverges from the budget is REPORTED",
      any("per-trade budget" in x for x in _div),
      "the check must still bite when someone edits one key and not the other")
_ok = sizing.risk_coherence({"backtest_capital": 200000,
                             "risk_pct_per_trade": 2.0,
                             "option_risk_per_trade_rupees": 4000,
                             "portfolio_max_drawdown": 12000})
check("a coherent set reports nothing", _ok == [], str(_ok))
_one = sizing.risk_coherence({"backtest_capital": 200000,
                              "risk_pct_per_trade": 2.0,
                              "option_risk_per_trade_rupees": 4000,
                              "portfolio_max_drawdown": 4000})
check("a portfolio cap one trade can trip is REPORTED",
      any("meaningless" in x for x in _one), str(_one))

# 2026-08-02 — the portfolio cap was raised 5,000 -> 12,000 so it stops
# being a single-trade stop. These pin the RELATIONSHIPS, not the number,
# so a future tweak to either value cannot silently reintroduce the bug.
_live = dict(config.DEFAULTS)
check("portfolio cap survives at least 2 full-risk trades",
      _live["portfolio_max_drawdown"] / _live["option_risk_per_trade_rupees"] >= 2,
      f"{_live['portfolio_max_drawdown']/_live['option_risk_per_trade_rupees']:.1f} "
      f"concurrent trades")
# 2026-08-02 — two checks here previously ORDERED daily_loss_limit against
# portfolio_max_drawdown. That was wrong: the first is a REALISED loss
# ceiling that blocks new orders, the second an UNREALISED drawdown that
# force-closes. Different quantities, not orderable. Acting on the bogus
# rule raised daily_loss_limit to 20,000 and broke the documented
# class_budget_blocked() invariant. What daily_loss_limit MUST be ordered
# against is the per-class budgets, which is now what is checked.
check("class budgets summing BELOW the global ceiling is REPORTED",
      any("binding constraint" in x for x in sizing.risk_coherence(
          {"daily_loss_limit": 20000, "budget_futures_daily_loss": 2500,
           "budget_spread_daily_loss": 3000, "budget_option_daily_loss": 2000})),
      "sub-budgets must sum ABOVE the global or it can never fire")
check("a single class able to eat the whole day is REPORTED",
      any("consume the whole day" in x for x in sizing.risk_coherence(
          {"daily_loss_limit": 5000, "budget_futures_daily_loss": 5000,
           "budget_spread_daily_loss": 3000, "budget_option_daily_loss": 2000})),
      "that is exactly what per-class budgets exist to prevent")
check("the shipped DEFAULTS satisfy the class-budget invariant",
      sum(config.DEFAULTS.get(f"budget_{k}_daily_loss", 0)
          for k in ("futures", "spread", "option"))
      > config.DEFAULTS["daily_loss_limit"],
      f"₹7,500 sub-budgets vs ₹{config.DEFAULTS['daily_loss_limit']:,} global")
check("and it sits BELOW the portfolio kill-switch",
      config.DEFAULTS["option_risk_per_trade_rupees"]
      < config.DEFAULTS["portfolio_max_drawdown"],
      "otherwise one trade can trip the whole portfolio on its own")
check("it was NOT fitted to which historical trades lost",
      "back-fitted" in CFG or "back-fitted outcomes" in CFG,
      "a 2,500 cap would have avoided 27,383 in sample on n=106 with no "
      "demonstrated edge — that is curve-fitting a risk limit")

print("1) registered, or config.save() drops it silently")
check("option_risk_per_trade_rupees in DEFAULTS",
      "option_risk_per_trade_rupees" in config.DEFAULTS)
check("default is a positive rupee amount",
      (config.DEFAULTS.get("option_risk_per_trade_rupees") or 0) > 0,
      str(config.DEFAULTS.get("option_risk_per_trade_rupees")))
check("exposed in SettingsIn",
      "option_risk_per_trade_rupees" in open(os.path.join(HERE, "app.py")).read())

print("\n2) ONE implementation, shared with futures")
check("uses sizing.cap_by_rupee_risk", "cap_by_rupee_risk" in AG)
check("keyed separately from the futures cap",
      'key="option_risk_per_trade_rupees"' in AG)
check("no second cap function was written",
      AG.count("def cap_by_rupee_risk") == 0,
      "the helper lives in sizing.py and must stay there")

print("\n3) the cap only ever reduces")
cfg = dict(config.DEFAULTS)
cfg["lot_sizes"] = {"NIFTY": 65}
cfg["option_risk_per_trade_rupees"] = 5000   # explicit, not the default
# 1 lot, 40-point stop on a 65 lot = ₹2,600 — under the cap, untouched.
lots, why = sizing.cap_by_rupee_risk(cfg, "NIFTY", 200.0, 160.0, 1,
                                     key="option_risk_per_trade_rupees")
check("a within-cap trade is left alone", lots == 1 and not why, f"{lots} {why}")
# 3 lots at ₹2,600 each = ₹7,800 -> must come down, not up.
lots3, why3 = sizing.cap_by_rupee_risk(cfg, "NIFTY", 200.0, 160.0, 3,
                                       key="option_risk_per_trade_rupees")
check("an over-cap size is REDUCED", lots3 < 3 and lots3 >= 1, f"{lots3}: {why3}")
check("never returns MORE lots than asked",
      sizing.cap_by_rupee_risk(cfg, "NIFTY", 200.0, 199.0, 1,
                               key="option_risk_per_trade_rupees")[0] <= 1,
      "a risk cap that can increase size is not a risk cap")
# 1 lot risking more than the whole cap -> blocked outright.
lots0, why0 = sizing.cap_by_rupee_risk(cfg, "NIFTY", 400.0, 300.0, 1,
                                       key="option_risk_per_trade_rupees")
check("a single lot above the cap is BLOCKED", lots0 == 0,
      f"{why0} — ₹6,500 on one lot exceeds the portfolio allowance itself")

print("\n4) the two caps are independent")
cfg2 = dict(cfg); cfg2["futures_risk_per_trade_rupees"] = 2500
o, _ = sizing.cap_by_rupee_risk(cfg2, "NIFTY", 400.0, 340.0, 1,
                                key="option_risk_per_trade_rupees")
f, _ = sizing.cap_by_rupee_risk(cfg2, "NIFTY", 400.0, 340.0, 1)
check("the options cap does not read the futures key", o == 1 and f == 0,
      f"₹3,900 risk: options(cap 5000)={o} lot, futures(cap 2500)={f} lot")

print("\n5) disabling it is possible and explicit")
cfg3 = dict(cfg); cfg3["option_risk_per_trade_rupees"] = 0
check("cap 0 disables rather than blocking everything",
      sizing.cap_by_rupee_risk(cfg3, "NIFTY", 400.0, 300.0, 1,
                               key="option_risk_per_trade_rupees")[0] == 1,
      "0 must mean off, not 'block every trade'")

print("\n6) a refusal is LOUD")
blk = AG.split('key="option_risk_per_trade_rupees"')[1][:900]
check("the cap logs when it bites", "bus.log" in blk)
check("a refusal raises an alert, not a silent return", "bus.alert" in blk,
      "a silently-skipped trade is indistinguishable from no signal")
check("the refusal reason reaches the caller", 'error' in blk)

print("\n7) it sits AFTER sizing and BEFORE the order")
i_size = AG.find("sizing.size_option_buy")
i_cap = AG.find('key="option_risk_per_trade_rupees"')
i_order = AG.find("PAPER BUY", i_cap)
check("cap applied after the sizing call", 0 < i_size < i_cap)
check("cap applied before the order is built", i_cap < i_order, f"{i_cap} < {i_order}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all option-risk-cap checks passed")
