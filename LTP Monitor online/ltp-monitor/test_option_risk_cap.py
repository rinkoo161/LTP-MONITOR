#!/usr/bin/env python3
"""test_option_risk_cap.py — per-trade rupee cap on the OPTIONS path.

v59.0 (2026-08-02). The futures path got this cap on 2026-08-01; options
had none, so `portfolio_max_drawdown` was the only thing between one
option trade and the whole book — and measured against 500 real trades,
a single 1-lot trade already risks a median ₹3,198 against a ₹5,000
portfolio cap, with a maximum of ₹6,435. That maximum is the case this
closes: one trade exceeding the entire portfolio allowance makes the
portfolio cap meaningless exactly when it is needed.

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
cfg["option_risk_per_trade_rupees"] = 5000
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
