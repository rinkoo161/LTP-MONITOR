#!/usr/bin/env python3
"""test_notional_costs.py — live P&L must use the real cost model.

The Futures Research page had already measured what the flat model
costs, on the SAME 325 trades with the SAME signals and exits:

    flat fee_per_lot=40    +Rs 63,531   "PROFITABLE — would have deployed"
    notional-aware model   -Rs 31,000   "FAIL — no edge above costs"

and its cost readout puts the OPTIONS understatement at Rs 26-47 per
round trip per symbol, because the flat model omits the bid-ask spread
entirely — the largest single component for an ATM index option.

`options_costs.py` and `futures_costs.py` already existed and were used
by the promotion gate and that research page. They were NOT used by the
live P&L or the backtester, so every recorded figure was overstated.
This wires the live path to them rather than adding a third model.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_notional_costs")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = config.load()

print("1) the real cost EXCEEDS the flat model on every symbol")
flat = cfg.get("fee_per_lot", 40) * 2
for sym, prem in (("NIFTY", 150.0), ("BANKNIFTY", 780.0),
                  ("FINNIFTY", 390.0), ("SENSEX", 300.0)):
    real = agents.realistic_fees("option", sym, 1, prem, prem * 1.05, cfg, legs=1)
    check(f"{sym:10} real > flat", real > flat,
          f"flat Rs {flat}, real Rs {real:.0f} — understated by Rs {real-flat:.0f}")

print("\n2) a 2-leg spread costs more than a single leg")
one = agents.realistic_fees("option", "NIFTY", 1, 150.0, 140.0, cfg, legs=1)
two = agents.realistic_fees("option", "NIFTY", 1, 150.0, 140.0, cfg, legs=2)
check("2 legs cost more than 1", two > one, f"{one:.0f} vs {two:.0f}")
check("and by roughly a factor of two", 1.6 < two / one < 2.6,
      f"ratio {two/one:.2f} — every leg is opened AND closed, so a spread "
      f"crosses the bid-ask FOUR times")

print("\n3) cost scales with LOTS")
l1 = agents.realistic_fees("option", "NIFTY", 1, 150.0, 140.0, cfg)
l3 = agents.realistic_fees("option", "NIFTY", 3, 150.0, 140.0, cfg)
check("3 lots cost ~3x one lot", abs(l3 - 3 * l1) < 2,
      f"{l1:.0f} -> {l3:.0f}")

print("\n4) it FALLS BACK rather than making a trade look free")
bad = agents.realistic_fees("option", "NIFTY", 1, None, None, cfg)
check("a missing premium still charges something", bad > 0, str(bad))
check("and the fallback is the flat model, not zero", bad >= flat,
      f"{bad:.0f} — a cost model that returns 0 on bad input would make "
      f"every such trade look profitable")

print("\n5) it reuses the EXISTING models, it does not add a third")
AG = open(os.path.join(HERE, "agents.py")).read()
body = AG.split("def realistic_fees(")[1]
body = body[:body.index("\ndef ")]
check("options path calls options_costs", "options_costs" in body)
check("futures path calls futures_costs", "futures_costs" in body)
check("no statutory rates are re-declared here",
      "stt" not in body.lower() and "gst" not in body.lower(),
      "a second copy of the tax table would drift from options_costs.py "
      "— the failure this codebase has had with the session check, the "
      "news regexes and the OI quadrant classifier")

print("\n6) every LIVE exit path uses it — options, spreads AND futures")
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]
flat_sites = [l for l in _code
              if re.search(r'fees\s*=\s*round\(cfg\.get\("fee_per_lot"', l)]
check("no live exit still computes fees from the flat model",
      not flat_sites, f"{flat_sites}")
check("three exit paths call realistic_fees",
      sum(1 for l in _code if "fees = realistic_fees(" in l) == 3,
      "single-leg options, spreads and futures")

print("\n7) the BACKTESTER agrees with live")
BT = open(os.path.join(HERE, "backtester.py")).read()
check("spread replays use the same helper",
      BT.count("_ag.realistic_fees(") >= 2,
      "a replay costing trades differently from live is the v59.36 "
      "failure again, in the one dimension that decides profitability")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all notional-cost checks passed")
