#!/usr/bin/env python3
"""test_tier0_costs.py — v59.68, third-eye Tier 0 (measurement integrity).

Every check here EXECUTES the cost path. The futures cost bug survived a
string-match test (`"futures_costs" in body`) for exactly the reason the
review gave: the call was present in the source and dead at runtime.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_tier0_costs")

import agents
import config
import futures_costs
import options_costs
import restate_costs

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


cfg = config.load()

# --- futures costs actually reach the notional model --------------------
fell_back = []
r = agents.realistic_costs("future", "NIFTY", 1, 25000.0, 25050.0, cfg,
                           log=fell_back.append)
check("futures round trip uses the NOTIONAL model, not the flat fallback",
      r.get("model") == "notional" and not fell_back,
      f"{r} (the old call charged ₹80 here)")
b = futures_costs.breakdown("NIFTY", 25000.0, 25050.0, lots=1, cfg=cfg,
                            lot=(cfg.get("lot_sizes") or {}).get("NIFTY", 75))
check("agents' futures charge matches futures_costs.breakdown",
      abs(r["total"] - round(b["statutory_rupees"] + b["items"]["slippage"], 0)) <= 1,
      f"{r['total']} vs breakdown {b['statutory_rupees'] + b['items']['slippage']:.0f}")
check("futures cost scales with lots",
      agents.realistic_costs("future", "NIFTY", 3, 25000.0, 25050.0,
                             cfg)["total"] > 2.5 * r["total"])

# A genuinely broken model must still fall back — loudly and labelled.
_orig = futures_costs.breakdown
futures_costs.breakdown = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    logged = []
    rf = agents.realistic_costs("future", "NIFTY", 1, 25000.0, 25050.0, cfg,
                                log=logged.append)
finally:
    futures_costs.breakdown = _orig
check("broken model falls back, is labelled, and logs",
      rf.get("model") == "flat-fallback" and logged and rf["total"] > 0,
      str(rf))

# --- spread exit premium: one definition, no STT rebate -----------------
check("spread_exit_value: loser -> exit VALUE, not the (negative) pnl",
      agents.spread_exit_value(150.0, -60.0) == 210.0)
check("spread_exit_value: winner", agents.spread_exit_value(150.0, 40.0) == 110.0)
check("spread_exit_value floors at 0", agents.spread_exit_value(150.0, 200.0) == 0.0)
check("spread_exit_value tolerates None", agents.spread_exit_value(None, None) == 0.0)
buggy = options_costs.cost_round_trip(150.0, -60.0, 65, legs=2, cfg=cfg)["total"]
fixed = options_costs.cost_round_trip(150.0, agents.spread_exit_value(150.0, -60.0),
                                      65, legs=2, cfg=cfg)["total"]
check("the corrected exit premium charges MORE on a loser (rebate gone)",
      fixed > buggy, f"₹{fixed:.0f} vs buggy ₹{buggy:.0f}")
bt_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtester.py")).read()
check("both backtester spread replays use the shared definition",
      bt_src.count("_ag.spread_exit_value(") >= 2,
      f"found {bt_src.count('_ag.spread_exit_value(')}")

# --- opt_* rates are registered and genuinely config-driven -------------
OPT_KEYS = ("opt_brokerage_per_order", "opt_stt_sell_pct",
            "opt_exchange_txn_pct", "opt_sebi_turnover_pct",
            "opt_stamp_duty_pct", "opt_gst_pct", "opt_halfspread_points")
missing = [k for k in OPT_KEYS if k not in config.DEFAULTS]
check("all 7 opt_* cost keys are in config.DEFAULTS (survive save)",
      not missing, f"missing: {missing}")
wide = dict(cfg, opt_halfspread_points=1.0)
narrow = dict(cfg, opt_halfspread_points=0.5)
cw = options_costs.cost_round_trip(150.0, 140.0, 65, legs=1, cfg=wide)
cn = options_costs.cost_round_trip(150.0, 140.0, 65, legs=1, cfg=narrow)
check("opt_halfspread_points actually moves the charged spread",
      abs(cw["spread"] - 2 * cn["spread"]) < 1e-6,
      f"{cn['spread']:.0f} -> {cw['spread']:.0f}")

# --- restate(): pure, auditable, refuses to invent ----------------------
rows = [
    {"symbol": "BANKNIFTY", "side": "SHORT", "leg": None, "lots": 2,
     "lot_size": 30, "entry": 57083.6, "ltp": 57116.4,
     "gross_pnl": -1968.0, "fees": 120, "slippage": None, "pnl": -2088.0,
     "mfe": 500, "mae": -2100},
    {"symbol": "FINNIFTY", "leg": "SPREAD", "lots": 2, "qty": 120,
     "entry": 35.3, "ltp": -12.0,                      # a LOSER (pnl/share)
     "gross_pnl": -1440.0, "fees": 198.0, "slippage": 240.0, "pnl": -1878.0},
    {"symbol": "BANKNIFTY", "side": "SHORT", "leg": None, "lots": 1,
     "lot_size": 30, "entry": 56988.2, "ltp": None,    # no exit price
     "gross_pnl": -60.0, "fees": 60, "pnl": -120.0},
    {"symbol": "NIFTY", "leg": "CE", "strike": 25000, "entry": 100.0,
     "ltp": 110.0, "gross_pnl": 650.0, "fees": 135.0, "slippage": 65.0,
     "pnl": 450.0},                                    # single-leg: untouched
]
new, st = restate_costs.restate(rows, cfg=cfg)
check("restates the futures and spread rows only",
      st["futures"] == 1 and st["spreads"] == 1 and st["skipped"] == 2)
fut = new[0]
check("futures fees jump from flat to notional",
      fut["fees"] > 500 and fut["slippage"] > 0
      and fut["cost_model"] == restate_costs.STAMP_MODEL_FUT,
      f"fees {fut['fees']}, slip {fut['slippage']}")
check("gross/mfe/mae untouched; pnl = gross - new costs",
      fut["gross_pnl"] == -1968.0 and fut["mfe"] == 500
      and fut["pnl"] == round(-1968.0 - fut["fees"] - fut["slippage"], 0))
check("previous numbers kept for audit",
      fut["restated_v5968_from"]["fees"] == 120
      and fut["restated_v5968_from"]["pnl"] == -2088.0)
spr = new[1]
check("spread loser recomputed via exit value (costs went UP)",
      spr["fees"] + spr["slippage"] > 198.0 + 240.0
      and spr["cost_model"] == restate_costs.STAMP_MODEL_SPR,
      f"costs {spr['fees'] + spr['slippage']:.0f} vs 438")
check("the no-exit-price row is flagged, not invented",
      "restate_v5968_error" not in new[0]
      and new[2].get("fees") == 60 and new[2].get("pnl") == -120.0)
check("single-leg option row untouched",
      new[3] == rows[3])
check("input rows are not mutated",
      rows[0]["fees"] == 120 and "restated_v5968_from" not in rows[0])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tier-0 cost checks passed")
