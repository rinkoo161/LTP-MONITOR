#!/usr/bin/env python3
"""test_tier2_economics.py — v59.73, third-eye Tier 2 (economic mechanism).

The feasibility gate (designed edge ≥ min_edge_cost_ratio × round-trip
cost), its wiring into every admission path live AND replay, the
cost-covering profit-lock floor, and the mechanism pre-registration
rule in the promotion gate. All by execution.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_tier2_economics")

import config
import edge_feasibility as ef
import mechanisms
import options_costs as oc
import promotion_gate as pg
import backtester as bt
import agents

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


cfg = config.load()

# --- the core rule ------------------------------------------------------
ok, ratio = ef.feasible(400.0, 100.0, cfg)
check("4x designed edge clears the default 2x bar", ok and ratio == 4.0)
ok, _ = ef.feasible(150.0, 100.0, cfg)
check("1.5x designed edge is refused", not ok)
ok, _ = ef.feasible(1000.0, 0.0, cfg)
check("an unpriceable trade (cost 0/unknown) is refused, not waved through",
      not ok)
check("the bar is config-driven",
      ef.feasible(150.0, 100.0, {**cfg, "min_edge_cost_ratio": 1.2})[0])

# --- per-instrument designs against real cost models --------------------
ok, d = ef.spread_feasible(35.0, 0.18, 60, cfg=cfg)
check("a thin spread capture (18% of a ₹35 credit) is refused", not ok, d)
ok, d = ef.spread_feasible(60.0, 0.45, 60, cfg=cfg)
check("a rich capture on a fat credit clears", ok, d)
ok, d = ef.option_buy_feasible(100.0, 101.0, 65, cfg)
check("a 1-point option target is refused", not ok, d)
ok, d = ef.option_buy_feasible(100.0, 112.0, 65, cfg)
check("a 12-point option target clears", ok, d)
ok, d = ef.future_feasible("NIFTY", 25000.0, 25010.0, 65, 1, cfg)
check("a 10-point futures target is refused", not ok, d)
ok, d = ef.future_feasible("NIFTY", 25000.0, 25060.0, 65, 1, cfg)
check("a 60-point futures target clears", ok, d)

# --- the shared spread admission refuses infeasible designs -------------
import strategies
_analysis = {
    "symbol": "NIFTY", "spot": 25000,
    "signal_lines": {"S": [{"level": 24800}], "R": [{"level": 25200}]},
    "strikes": [
        {"strike": 24700, "pe": {"ltp": 15.0, "security_id": "1"},
         "ce": {"ltp": 320.0, "security_id": "2"}},
        {"strike": 24800, "pe": {"ltp": 60.0, "security_id": "3"},
         "ce": {"ltp": 240.0, "security_id": "4"}},
        {"strike": 24900, "pe": {"ltp": 60.0, "security_id": "5"},
         "ce": {"ltp": 170.0, "security_id": "6"}},
        {"strike": 25200, "pe": {"ltp": 210.0, "security_id": "7"},
         "ce": {"ltp": 55.0, "security_id": "8"}},
        {"strike": 25300, "pe": {"ltp": 280.0, "security_id": "9"},
         "ce": {"ltp": 32.0, "security_id": "10"}},
        {"strike": 25400, "pe": {"ltp": 350.0, "security_id": "11"},
         "ce": {"ltp": 18.0, "security_id": "12"}},
    ],
}
_regime = {"regime": "rangebound"}   # the literal REGIME_FIT actually uses
_params_thin = {"wall_gap_frac": 1.5, "credit_min_frac": 0.05,
                "profit_capture": 0.10}
_params_rich = {"wall_gap_frac": 1.5, "credit_min_frac": 0.05,
                "profit_capture": 0.45}
r_thin = strategies.evaluate("bull_put_spread", _analysis, _regime,
                             params=_params_thin)
r_rich = strategies.evaluate("bull_put_spread", _analysis, _regime,
                             params=_params_rich)
check("strategies.evaluate refuses a design whose capture is below cost",
      r_thin is not None and not r_thin.get("eligible")
      and any("feasibility bar" in x for x in r_thin.get("reasons", [])),
      str(r_thin.get("reasons", []))[-120:])
check("the same wall with a cost-clearing capture is admitted",
      r_rich is not None and r_rich.get("eligible"),
      str((r_rich or {}).get("reasons", []))[-120:])

# --- the replays apply the identical bar (live↔replay parity) -----------
CANDLES = []
for i in range(80):
    base = 100.0 + i * 0.1
    CANDLES.append({"ts": 1000 + i * 60, "open": round(base, 2),
                    "high": round(base + 0.4, 2), "low": round(base - 0.4, 2),
                    "close": round(base + 0.2, 2), "volume": 1000})

class _H:
    @staticmethod
    def index_days(sym, n=250):
        return ["2026-08-01"]
    @staticmethod
    def day_index_candles(sym, day, for_compute=False):
        return [dict(c) for c in CANDLES]

def _mk_eval(t1_gap):
    def _stub(name, c1, c5, c15, params=None, taken_today=0, precomputed=None):
        if len(c1) == 13 and taken_today == 0:
            s = c1[-1]["close"]
            return {"dir": 1, "entry_spot": s, "stop_spot": round(s - 3, 2),
                    "t1_spot": s + t1_gap, "t2_spot": s + 2 * t1_gap}
        return None
    return _stub

def _run_stub(t1_gap):
    _oh, _oe = bt.history, bt.pa.evaluate
    try:
        bt.history = _H
        bt.pa.evaluate = _mk_eval(t1_gap)
        return bt.replay_pa("NIFTY", "momentum_confluence",
                            params={"max_trades_per_day": 1})
    finally:
        bt.history, bt.pa.evaluate = _oh, _oe

# fee = fee_per_lot(40)×2 = 80; designed = gap×0.5×65. gap 1 → ₹32.5
# (0.4x, refused); gap 8 → ₹260 (3.25x, admitted).
check("replay refuses a signal whose designed edge is below cost",
      len(_run_stub(1.0)) == 0)
check("replay admits a signal whose designed edge clears the bar",
      len(_run_stub(8.0)) == 1)

# --- profit-lock floor must cover the round trip ------------------------
_mk_sp = lambda qty, floor: {
    "legs": [{"leg": "PE", "entry": 50.0, "action": "SELL"}],
    "short_strike": 24800, "width": 100, "loss_limit": 100.0,
    "profit_target": 45.0, "credit": 50.0, "qty": qty, "lots": max(1, qty // 65),
    "symbol": "NIFTY", "strategy": "bull_put_spread", "mfe": floor * qty,
    "profit_floor": floor}
_qty = 130
_floor = 2.2          # ₹286 — above the old fixed ₹250 minimum
_cost = oc.cost_round_trip(50.0, 50.0 - _floor, _qty, legs=2, cfg=cfg)["total"]
assert 250 < _floor * _qty < _cost, (
    f"fixture broke: floor amt {_floor*_qty} vs cost {_cost}")
reason = agents.spread_exit_reason(_mk_sp(_qty, _floor), 1.0, None, cfg,
                                   0, True)
check("a floor that beats ₹250 but NOT the round trip no longer exits",
      reason is None,
      f"floor ₹{_floor*_qty:.0f} < cost ₹{_cost:.0f} → held")
_floor2 = 8.0         # ₹1040 — comfortably above the round trip
_cost2 = oc.cost_round_trip(50.0, 50.0 - _floor2, _qty, legs=2, cfg=cfg)["total"]
assert _floor2 * _qty > _cost2
reason2 = agents.spread_exit_reason(_mk_sp(_qty, _floor2), 5.0, None, cfg,
                                    0, True)
check("a floor that covers its costs still locks profit",
      reason2 is not None and "profit lock" in reason2, str(reason2))

# --- mechanism pre-registration gates live promotion --------------------
good_oos = {"trades": 120, "net_pnl": 120 * 900.0, "pnl_sd": 2886,
            "pnl_sd_day": 3000.0, "days_tested": 15,
            "window": "days after 2026-07-01 (v3 adoption)"}
full = {"trades": 300, "net_pnl": 300 * 900.0, "oos": good_oos}
ok_orb, d_orb = pg.evaluate_entry("orb", "NIFTY", dict(full))
check("a strategy with an UNSTATED mechanism is denied whatever its stats",
      not ok_orb and "mechanism" in (d_orb.get("reason") or ""),
      d_orb.get("reason", ""))
ok_bps, d_bps = pg.evaluate_entry("bull_put_spread", "NIFTY", dict(full))
check("a stated risk-transfer mechanism proceeds to statistical scoring",
      ok_bps, f"headroom {d_bps.get('headroom')}")
check("the registry is honest: spreads stated, PA/futures unstated",
      mechanisms.stated("bull_put_spread")
      and mechanisms.stated("bear_call_spread")
      and not any(mechanisms.stated(n) for n in
                  ("orb", "vwap_pullback", "ema_mtf", "sg_ema",
                   "momentum_confluence", "ew_reversal", "ta_elliott",
                   "oi_composite", "s11_momentum")))
check("every strategy id has a registry entry",
      all(mechanisms.get(n) for n in
          ("bull_put_spread", "bear_call_spread", "orb", "vwap_pullback",
           "ema_mtf", "sg_ema", "momentum_confluence", "ew_reversal",
           "ta_elliott", "momentum_buy", "oi_composite",
           "s11_momentum", "s12_vwap_reversion", "s13_orb", "s14_existing")))

# --- config keys registered --------------------------------------------
for k in ("min_edge_cost_ratio", "exit_min_cost_coverage"):
    check(f"'{k}' registered in DEFAULTS", k in config.DEFAULTS)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tier-2 economics checks passed")
