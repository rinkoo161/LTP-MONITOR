#!/usr/bin/env python3
"""test_spread_stop_basis.py — a stop price must be a stop price.

2026-08-02. `exit_spread()` wrote `stoploss = -loss_limit` and
`target1 = +profit_target`: correct VALUES in fields that mean PRICE
everywhere else in this codebase. 385 of 500 journal rows therefore
carried a negative "stop price", which nothing tradeable can have, and
any consumer doing `entry - stoploss` got nonsense. It produced a bogus
"median stop = 200% of premium" and contaminated the per-trade risk
figure the options cap was calibrated against.

What these check:

  1. the WRITER now stores a real spread-value price, positive and on
     the correct side of entry for a short;
  2. the READER still understands the ~385 legacy rows, because they are
     history and cannot be rewritten;
  3. an OPTION buy is not touched by either path — the legacy conversion
     is the kind of "helpful" normalisation that quietly corrupts the
     population it was not meant for.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_spread_stop_basis")

import agents

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "agents.py")).read()

print("1) the WRITER stores a price, not a P&L")
check("no longer writes a negative stop",
      '"stoploss": -sp["loss_limit"]' not in SRC,
      "a negative stop price is impossible for anything tradeable")
check("writes credit + loss_limit (the spread value at the stop)",
      'sp["credit"] + sp["loss_limit"]' in SRC)
check("writes credit - profit_target (the value at the target)",
      'sp["credit"] - sp["profit_target"]' in SRC)
check("labels the basis explicitly", '"stop_basis": "spread_value"' in SRC,
      "so no future reader has to infer it from a sign")
check("keeps the P&L numbers under names that say what they are",
      '"loss_limit_per_share"' in SRC and '"profit_target_per_share"' in SRC,
      "nothing is lost, and neither can be misread")
check("records the side", '"side": "SHORT"' in SRC,
      "a stop ABOVE entry only reads correctly if the side is known")

print("\n2) geometry: a credit spread's stop is ABOVE its entry")
CREDIT, LOSS, TGT = 40.0, 60.0, 24.0
stop_px = CREDIT + LOSS
tgt_px = CREDIT - TGT
check("stop price is positive", stop_px > 0, f"{stop_px}")
check("stop is above entry (short loses as value rises)",
      stop_px > CREDIT, f"stop {stop_px} vs entry {CREDIT}")
check("target is below entry", tgt_px < CREDIT, f"target {tgt_px}")
check("risk per share equals the loss limit",
      abs((stop_px - CREDIT) - LOSS) < 1e-9,
      "the whole point: entry-to-stop must reconstruct the real risk")

print("\n3) the READER still understands legacy rows")
legacy = {"leg": "SPREAD", "strategy": "bull_put_spread", "entry": CREDIT,
          "stoploss": -LOSS, "target1": TGT, "qty": 75, "ltp": 5.0, "pnl": 100}
r = agents.trade_risk_fields(legacy)
check("legacy negative stop is converted to a price", r["stop"] == stop_px,
      f"{r['stop']} (expected {stop_px})")
check("legacy target is converted too", r["target"] == tgt_px,
      f"{r['target']} (expected {tgt_px})")
check("reconstructed risk matches the original loss limit",
      abs((r["stop"] - r["entry"]) - LOSS) < 1e-9)

print("\n4) NEW rows pass through untouched")
new = dict(legacy, stoploss=stop_px, target1=tgt_px, stop_basis="spread_value")
r2 = agents.trade_risk_fields(new)
check("a stop_basis row is not re-converted", r2["stop"] == stop_px,
      f"{r2['stop']} — double conversion would silently double the risk")
check("its target is not re-converted", r2["target"] == tgt_px, str(r2["target"]))

print("\n5) OPTION buys are untouched by the legacy path")
opt = {"leg": "CE", "strategy": "momentum_confluence", "entry": 146.2,
       "stoploss": 83.75, "target1": 292.4, "qty": 75, "ltp": 150.0, "pnl": 40}
r3 = agents.trade_risk_fields(opt)
check("option stop unchanged", r3["stop"] == 83.75, str(r3["stop"]))
check("option target unchanged", r3["target"] == 292.4, str(r3["target"]))
check("classified as an option, not a spread",
      agents.trade_class(opt) == "option", agents.trade_class(opt))
# The dangerous near-miss: an option whose target sits below entry (a
# ratcheted or degenerate signal) must NOT be rewritten as a spread.
odd = dict(opt, target1=100.0)
check("an option with target < entry is still not converted",
      agents.trade_risk_fields(odd)["target"] == 100.0,
      "the conversion is gated on kind=='spread', not on the sign alone")

print("\n6) no journal row can now carry an impossible stop")
r4 = agents.trade_risk_fields({"leg": "SPREAD", "strategy": "bear_call_spread",
                               "entry": 30.0, "stoploss": -45.0,
                               "target1": 18.0, "qty": 30, "ltp": 2.0,
                               "pnl": -50})
check("bear call legacy row also converts", r4["stop"] == 75.0, str(r4["stop"]))
check("every reconstructed stop is positive",
      all(x["stop"] > 0 for x in (r, r2, r3, r4)))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all spread-stop-basis checks passed")
