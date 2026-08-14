#!/usr/bin/env python3
"""test_planned_lots_sync.py — v59.85.

The risk gate's daily-loss check used cfg["lots_per_trade"] (5) while
ExecutionAgent._place sized with size_option_buy() and then applied
cap_by_rupee_risk(). Live, 2026-08-14 09:21:

    ✗ daily loss limit (risking ₹13,292, day P&L ₹0)

5 lots x ₹2,658 — but the per-trade cap would have placed 3, risking
₹7,974, comfortably inside the ₹10,000 limit. The gate refused a trade
on a loss that could not occur, and did it by double-counting the very
cap that runs immediately after it.

`planned_option_lots()` now answers that question by CALLING the same
chain. Because it mirrors _place rather than being called by it, this
file pins the two together: if either side changes its sizing chain,
this fails rather than letting them drift apart silently.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_planned_lots_sync")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


SRC = open("agents.py").read()
_p = SRC.index("def _place(self, job, manual=False):")
PLACE = SRC[_p:SRC.index("\n    def ", _p + 10)]
_h = SRC.index("def planned_option_lots(")
HELPER = SRC[_h:SRC.index("\ndef ", _h + 10)]
check("both bodies sliced", len(PLACE) > 500 and len(HELPER) > 300,
      f"place={len(PLACE)} helper={len(HELPER)}")
# Strip comments before asserting on what _place DOES: the comment
# explaining the collapse names the very functions it no longer calls,
# and matched them.
PLACE_CODE = "\n".join(l for l in PLACE.split("\n")
                       if not l.lstrip().startswith("#"))

print("\n1) there is exactly ONE sizing chain, and _place calls it")
# v59.87 — this section used to assert the two chains MIRRORED each
# other, because planned_option_lots was a copy of _place's logic. The
# 14-Aug journal review asked for one authoritative risk calculation,
# so they were collapsed. The invariant is now stronger: _place must
# NOT contain a chain of its own.
for fn in ("deployed_capital", "size_by_atr_risk", "size_option_buy",
           "cap_by_rupee_risk"):
    check(f"planned_option_lots owns {fn}", fn in HELPER)
    check(f"_place does NOT re-derive {fn}", fn not in PLACE_CODE,
          "a second copy is the drift this collapse removes")
check("_place gets its lots from the shared helper",
      "planned_option_lots(" in PLACE)
check("the cap targets the OPTION key, not the futures default",
      HELPER.count('key="option_risk_per_trade_rupees"') == 1,
      "cap_by_rupee_risk defaults to futures_risk_per_trade_rupees")
check("the mtf_confluence branch survives the collapse",
      '"mtf_confluence"' in HELPER)
check("the risk gate uses the same helper",
      SRC.count("planned_option_lots(") >= 3,
      "definition + risk gate + execution")

print("\n2) the daily RISK BUDGET check no longer assumes lots_per_trade")
# Anchor on the f-string, not the bare phrase — the phrase also appears
# in the comment above the check (quoting the live log line), so
# index() found the comment and scanned the wrong window entirely.
_d = SRC.index('f"{DAILY_BUDGET_LABEL}: trade risk')
_ctx = SRC[_d - 900:_d + 300]
check("the anchor found the CODE, not the comment", "check(" in _ctx)
check("it sizes from planned_option_lots", "planned_option_lots" in _ctx)
check("it no longer multiplies by lots_per_trade",
      'cfg["lots_per_trade"]' not in _ctx,
      "that is the 5-vs-3 mismatch this fixes")
check("the limit itself is unchanged", 'cfg.get("daily_loss_limit", 5000)' in _ctx,
      "this must not become a loosening of the limit")

print("\n3) it reproduces the live 2026-08-14 numbers")
CFG = {**config.DEFAULTS, "lot_sizes": {"NIFTY": 65}, "lots_per_trade": 5,
       "dynamic_sizing_enabled": True, "risk_pct_per_trade": 5.0,
       "backtest_capital": 200000, "max_lots_per_trade": 10,
       "option_risk_per_trade_rupees": 10000, "daily_loss_limit": 10000}


class _Bus:
    def get(self, k, d=None):
        return {} if k in ("positions", "spreads") else d


SIG = {"signal": "BUY_PE", "strike": 24300, "entry": 130.45,
       "stoploss": 89.55, "target1": 212.2, "confidence": 71}
lots, why, _cap = agents.planned_option_lots(CFG, _Bus(), "NIFTY", SIG)
risk = (SIG["entry"] - SIG["stoploss"]) * 65 * max(1, lots)
check("the real signal sizes to 3 lots, not 5", lots == 3, f"{lots} lots — {why}")
check("its risk is inside the daily limit", risk < 10000, f"₹{risk:,.0f}")
check("the old arithmetic would have breached it",
      (SIG["entry"] - SIG["stoploss"]) * 65 * 5 > 10000,
      f"₹{(SIG['entry'] - SIG['stoploss']) * 65 * 5:,.0f} at 5 lots")

print("\n4) it still refuses what the per-trade cap genuinely refuses")
_tight = {**CFG, "option_risk_per_trade_rupees": 500}
lots2, why2, _cap2 = agents.planned_option_lots(_tight, _Bus(), "NIFTY", SIG)
check("an unaffordable trade sizes to 0", lots2 == 0, f"{lots2} — {why2}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
