"""Strategy 7 (SG-EMA, v51) tests: gates, structural stop, graceful
degradation, structure-break exit, config round-trip, and the parity
invariant (every S7 signal implies an ungated ema_mtf signal on the same
bar — the one-directional relationship the Pine oracle checks).

Run:  python3 test_strategy7.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pa_strategies as pa
import structure
import config

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def candles(closes, t0=1753500000, step=60, wick=1.5):
    return [{"time": t0 + i * step, "open": c, "high": c + wick,
             "low": c - wick, "close": c} for i, c in enumerate(closes)]


def cross_up_series(n=80, base=23800.0):
    """Downtrend then a sharp reversal that forces a 5/13 cross UP on the
    final bar, with enough amplitude for confirmed ZigZag pivots."""
    closes = []
    for i in range(n - 12):
        closes.append(base - i * 2 + math.sin(i / 5) * base * 0.004)
    for i in range(12):
        closes.append(closes[-1] + 14)
    return candles(closes)


def mtf_bull(n=30, base=23800.0):
    return candles([base + i * 3 for i in range(n)], step=300)


print("1) gate behaviour")
c5 = mtf_bull()
c15 = mtf_bull()
# Find a window where the ungated cross fires ON THE FINAL BAR — the
# strategy evaluates "did a cross just happen", so a fixed-length window
# only fires if the cross lands exactly at its end. Deriving the window
# from a sweep makes the fixture robust instead of luck-dependent.
full_series = cross_up_series(200)
c1 = None
for end in range(30, len(full_series)):
    w = full_series[:end]
    if pa.evaluate("ema_mtf", w, c5, c15,
                   params={"fast": 5, "slow": 13, "mtf_confirm": 1,
                           "max_trades_per_day": 99}):
        c1 = w
        break
assert c1, "no firing window found in sweep — fixture generator broken"
base = pa.evaluate("ema_mtf", c1, c5, c15,
                   params={"fast": 5, "slow": 13, "mtf_confirm": 1,
                           "max_trades_per_day": 5})
check("ungated ema_mtf fires (precondition)", bool(base),
      (base or {}).get("why"))

pivots = structure.zigzag_series(c1)
confirmed = [p for p in pivots if p.get("structure")]
d = base and base["dir"]
bull_bias = {"score": 45, "label": "bullish"}
bear_bias = {"score": -45, "label": "bearish"}
agree_bias = bull_bias if (d or 1) > 0 else {"score": -45, "label": "bearish"}
oppose_bias = bear_bias if (d or 1) > 0 else {"score": 45, "label": "bullish"}

ev, gates = pa.evaluate_sg_ema(c1, c5, c15, pivots=pivots, ai_bias=agree_bias)
check("test data produces confirmed pivots", len(confirmed) >= 1,
      f"{[p['structure'] for p in confirmed]}")
last_struct = confirmed[-1]["structure"] if confirmed else None
ok_structs = ("HH", "HL") if (d or 1) > 0 else ("LH", "LL")
if last_struct in ok_structs:
    check("bullish structure + bullish bias -> signal", bool(ev),
          f"gates={gates}")
    check("all gates recorded as passed",
          gates["cross"] is True and gates["structure"] is True
          and gates["ai_bias"] is True, str(gates))
else:
    check("adverse structure blocks the signal (gate=False)",
          ev is None and gates["structure"] is False, f"last={last_struct}")

ev2, gates2 = pa.evaluate_sg_ema(c1, c5, c15, pivots=pivots,
                                 ai_bias=oppose_bias)
check("opposing AI bias blocks (or structure already blocked)",
      ev2 is None, f"gates={gates2}")

print("\n2) graceful degradation — missing inputs SKIP, never block")
ev3, gates3 = pa.evaluate_sg_ema(c1, c5, c15, pivots=[], ai_bias=None)
check("no pivots + no bias -> gates skipped, signal allowed through",
      bool(ev3) and "skipped" in str(gates3["structure"])
      and "skipped" in str(gates3["ai_bias"]), str(gates3))
ev4, gates4 = pa.evaluate_sg_ema(c1, c5, c15, pivots=pivots,
                                 ai_bias={"score": 5, "label": "neutral"})
check("neutral bias -> skipped, not blocked",
      "skipped" in str(gates4["ai_bias"]), str(gates4["ai_bias"]))

print("\n3) structural stop")
if ev3:
    # with no pivots the stop must be ema_mtf's fallback
    check("no pivots -> EMA-separation fallback stop",
          "fallback" in ev3["why"], ev3["why"])
ev5, _ = pa.evaluate_sg_ema(c1, c5, c15, pivots=pivots, ai_bias=None)
if ev5:
    lows = [p["price"] for p in confirmed if p["type"] == "low"]
    if lows:
        check("structural stop at/below last confirmed low pivot",
              ev5["stop_spot"] <= lows[-1] + 0.01,
              f"stop={ev5['stop_spot']} pivot={lows[-1]}")
        risk = ev5["entry_spot"] - ev5["stop_spot"]
        check("target = rr_target x structural risk (2.0)",
              abs((ev5["t1_spot"] - ev5["entry_spot"]) - 2.0 * risk) < 0.01,
              f"risk={risk:.1f} t1_dist={ev5['t1_spot']-ev5['entry_spot']:.1f}")

print("\n4) parity invariant: S7 signal => ema_mtf signal on same data")
# Sweep many windows; wherever S7 fires, ungated ema_mtf must also fire.
violations = 0
fires_s7 = fires_base = 0
full = cross_up_series(200)
for end in range(30, len(full)):
    w = full[:end]
    b = pa.evaluate("ema_mtf", w, c5, c15,
                    params={"fast": 5, "slow": 13, "mtf_confirm": 0,
                            "max_trades_per_day": 99})
    sv, _ = pa.evaluate_sg_ema(w, c5, c15,
                               params={"mtf_confirm": 0,
                                       "max_trades_per_day": 99},
                               pivots=structure.zigzag_series(w),
                               ai_bias=None)
    if sv:
        fires_s7 += 1
        if not b:
            violations += 1
    if b:
        fires_base += 1
check("every S7 fire has a base ema_mtf fire on the same bar",
      violations == 0, f"s7={fires_s7} base={fires_base} violations={violations}")
check("base fires >= S7 fires (gates only ever REMOVE signals)",
      fires_base >= fires_s7, f"base={fires_base} s7={fires_s7}")

print("\n5) structure-break exit (ExecutionAgent hook)")
import agents
from agents import ExecutionAgent, Bus
bus = Bus()
ag = ExecutionAgent(bus, {})
SYM = "NIFTY"
exited = {}
ag.exit = lambda reason, symbol=None: exited.update({"reason": reason,
                                                     "symbol": symbol}) or {"ok": True}
# a long S7 position entered at candle-time T; then an LL pivot confirms after T
# c1 is sweep-derived and can be short — anchor mid-window, not at a
# fixed index that may not exist.
entry_idx = len(c1) // 2
entry_time = c1[entry_idx]["time"]
pos = {"symbol": SYM, "setup": "sg_ema", "entry_ts": entry_time,
       "leg": "CE", "signal": "BUY_CE", "entry": 100, "stoploss": 85,
       "target1": 130, "target2": 160, "strike": 23800, "qty": 75,
       "lots": 1, "pnl": 0, "ltp": 100, "t1_hit": False}
# Candles whose zigzag CONFIRMS adverse pivots after entry. The zigzag's
# default deviation is 0.5% (~119 pts at these levels), so the swings
# must clearly exceed it: legs of ~400 down / ~160 up produce LH + LL.
downs = []
px = c1[entry_idx]["close"]
for leg in range(4):
    for _ in range(16):
        px -= 25
        downs.append(px)
    for _ in range(8):
        px += 20
        downs.append(px)
down = candles(downs, t0=entry_time + 60)
bus.set(f"pa_candles:{SYM}", {"c1": c1[:entry_idx + 1] + down, "c5": c5, "c15": c15,
                              "ts": time.time()})
pivs = [p for p in structure.zigzag_series(c1[:entry_idx + 1] + down)
        if p.get("structure") and p["time"] > entry_time]
check("adverse pivot exists after entry (precondition)",
      any(p["structure"] in ("LH", "LL") for p in pivs),
      f"{[p['structure'] for p in pivs]}")
ag._monitor_one(pos)
check("structure-break exit fired with the pivot in the reason",
      "structure break" in str(exited.get("reason", "")),
      str(exited.get("reason")))

print("\n6) config round-trip for every spec key")
keys = ["strategy7_enabled", "s7_ema_fast", "s7_ema_slow", "s7_mtf_confirm",
        "s7_require_structure", "s7_require_ai_bias", "s7_min_ai_bias",
        "s7_structural_stop_buffer_pct", "s7_rr_target",
        "s7_max_trades_per_day", "s7_auto_deploy", "s7_markers_enabled",
        "s7_show_rejected_markers"]
missing = [k for k in keys if k not in config.DEFAULTS]
check("all 13 spec keys registered in DEFAULTS", not missing, str(missing))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
