"""v58.9 (item 9) — tests for Liquidity-sweep/FVG confluence layered
ON TOP OF the existing OI-wall spread selection, per the roadmap's own
framing. The OI wall stays the PRIMARY mechanism (which strike to
sell); this is an OPTIONAL additional confirmation layer
(spread_require_liquidity_confluence, default off — a genuine new
entry requirement, not a bug fix).

Three new structure.py primitives:
  - detect_liquidity_sweeps: a candle's wick breaks a prior CONFIRMED
    swing high/low (the zigzag_series pivots already used for
    Strategy 7), but its close comes back inside — the classic
    "stop hunt then reverse" pattern.
  - detect_fair_value_gaps: the standard 3-candle imbalance pattern.
  - wall_confluence: checks whether either pattern exists near a given
    OI-wall level, in the direction that reinforces the trade thesis.

Run:  python3 test_liquidity_fvg_confluence.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
import config
import strategies as slib
import structure as st

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def build_sweep_candles():
    """Hand-verifiable: down-leg confirms a pivot low ~99.5, up-leg
    confirms a pivot high ~112.5, then a final candle wicks BELOW the
    confirmed low (98.5 < 99.5) but CLOSES back above it (100.8) — a
    textbook bullish liquidity sweep of that exact level."""
    candles = []
    t = 1000
    for price in [115, 112, 108, 104, 100, 101]:
        candles.append({"time": t, "open": price + 0.5, "high": price + 1,
                        "low": price - 0.5, "close": price})
        t += 60
    for price in [103, 106, 109, 112, 111]:
        candles.append({"time": t, "open": price - 0.5, "high": price + 0.5,
                        "low": price - 1, "close": price})
        t += 60
    for price in [108, 104, 101, 100.5]:
        candles.append({"time": t, "open": price + 0.5, "high": price + 1,
                        "low": price - 0.5, "close": price})
        t += 60
    candles.append({"time": t, "open": 100.3, "high": 100.5, "low": 98.5, "close": 100.8})
    return candles


print("1) liquidity sweep detection: a textbook bullish sweep of a "
     "confirmed swing low is correctly identified")
candles = build_sweep_candles()
pivots = st.zigzag_series(candles, deviation_pct=1.0)
check("the down-leg correctly confirms a swing low pivot around 99.5",
      any(p["type"] == "low" and abs(p["price"] - 99.5) < 0.1 for p in pivots),
      str(pivots))
sweeps = st.detect_liquidity_sweeps(candles, pivots)
check("exactly one bullish sweep detected", len(sweeps) == 1 and
      sweeps[0]["type"] == "bullish_sweep", str(sweeps))
check("the swept level matches the confirmed pivot (99.5), not a "
      "different or approximate value",
      abs(sweeps[0]["swept_level"] - 99.5) < 0.01, str(sweeps[0]))

print("\n2) a candle that wicks past a level but does NOT close back "
     "inside it is correctly NOT flagged as a sweep (price broke down "
     "and stayed down — a genuine breakdown, not a stop-hunt reversal)")
no_reversal_candles = candles[:-1] + [
    {"time": candles[-1]["time"], "open": 98.0, "high": 98.5, "low": 96.0, "close": 96.5}]
sweeps2 = st.detect_liquidity_sweeps(no_reversal_candles, pivots)
check("no sweep detected when the close stays below the swept level",
      len(sweeps2) == 0, str(sweeps2))

print("\n3) Fair Value Gap detection: bullish gap (candle1.high < "
     "candle3.low) correctly identified")
bullish_fvg_candles = [
    {"time": 100, "open": 100, "high": 102, "low": 99, "close": 101},
    {"time": 160, "open": 103, "high": 108, "low": 102.5, "close": 107},
    {"time": 220, "open": 108, "high": 110, "low": 105, "close": 109},
]
gaps = st.detect_fair_value_gaps(bullish_fvg_candles)
check("one bullish FVG detected with the correct gap bounds",
      len(gaps) == 1 and gaps[0]["type"] == "bullish_fvg" and
      gaps[0]["gap_low"] == 102 and gaps[0]["gap_high"] == 105, str(gaps))

print("\n4) bearish FVG (candle1.low > candle3.high) correctly identified")
bearish_fvg_candles = [
    {"time": 100, "open": 110, "high": 111, "low": 108, "close": 109},
    {"time": 160, "open": 107, "high": 107.5, "low": 102, "close": 103},
    {"time": 220, "open": 102, "high": 105, "low": 100, "close": 101},
]
gaps2 = st.detect_fair_value_gaps(bearish_fvg_candles)
check("one bearish FVG detected with the correct gap bounds",
      len(gaps2) == 1 and gaps2[0]["type"] == "bearish_fvg" and
      gaps2[0]["gap_low"] == 105 and gaps2[0]["gap_high"] == 108, str(gaps2))

print("\n5) normal overlapping candles produce zero false-positive gaps")
no_gap_candles = [
    {"time": 100, "open": 100, "high": 102, "low": 99, "close": 101},
    {"time": 160, "open": 101, "high": 102.5, "low": 100, "close": 101.5},
    {"time": 220, "open": 101.5, "high": 102, "low": 100.5, "close": 101},
]
check("no gaps detected in ordinary overlapping price action",
      st.detect_fair_value_gaps(no_gap_candles) == [])

print("\n6) a filled FVG is correctly marked (a later candle trades "
     "back through the gap)")
filled_candles = bullish_fvg_candles + [
    {"time": 280, "open": 108, "high": 109, "low": 101, "close": 103}]
gaps3 = st.detect_fair_value_gaps(filled_candles)
check("the gap is correctly marked as filled once price returns to it",
      len(gaps3) == 1 and gaps3[0]["filled"] is True, str(gaps3))

print("\n7) wall_confluence: CONFIRMS when a matching sweep exists near "
     "the exact wall level")
r1 = st.wall_confluence(candles, level=99.5, direction="support")
check("confirmed when the wall matches the confirmed swept level",
      r1["confirmed"] is True, str(r1))

print("\n8) wall_confluence: does NOT confirm for an unrelated, distant "
     "level (no false positive from an irrelevant pattern elsewhere)")
r2 = st.wall_confluence(candles, level=50.0, direction="support")
check("not confirmed for a level far from any detected pattern",
      r2["confirmed"] is False, str(r2))

print("\n9) wall_confluence: does NOT confirm when checked in the "
     "WRONG direction (a bullish sweep doesn't confirm a resistance "
     "wall)")
r3 = st.wall_confluence(candles, level=99.5, direction="resistance")
check("not confirmed when direction doesn't match the pattern type",
      r3["confirmed"] is False, str(r3))

print("\n10) reasons are ALWAYS populated, even when not confirmed — "
     "never a silent empty result")
check("reasons list is non-empty even for the non-confirmed case",
      len(r2["reasons"]) > 0, str(r2["reasons"]))

print("\n11) integration: strategies.evaluate() accepts the optional "
     "candles parameter with zero behavior change for existing "
     "callers that don't pass it (backward compatible)")


def make_analysis(spot=100.0, wall=95.0):
    strikes = [{"strike": float(s), "ce": {"ltp": max(0.5, spot - s + 3), "security_id": f"{s}c"},
               "pe": {"ltp": max(0.5, s - spot + 3), "security_id": f"{s}p"}}
              for s in range(80, 121)]
    return {"symbol": "NIFTY", "spot": spot, "strikes": strikes,
           "signal_lines": {"S": [{"level": wall, "strength": 70, "color": "x"}], "R": []}}


regime = {"regime": "rangebound"}
_before = config.load()
try:
    config.save({"spread_require_liquidity_confluence": False})
    r_no_candles_param = slib.evaluate("bull_put_spread", make_analysis(), regime)
    r_with_none = slib.evaluate("bull_put_spread", make_analysis(), regime, candles=None)
    check("calling without the new parameter at all behaves identically "
          "to explicitly passing candles=None (true backward "
          "compatibility, not just 'happens not to crash')",
          r_no_candles_param == r_with_none, "results match")

    print("\n12) integration: confluence gate correctly BLOCKS entry "
         "with a clear reason when required but not found")
    config.save({"spread_require_liquidity_confluence": True})
    flat_candles = [{"time": 1000 + i * 60, "open": 100, "high": 100.5,
                     "low": 99.5, "close": 100} for i in range(20)]
    r_blocked = slib.evaluate("bull_put_spread", make_analysis(wall=95.0),
                             regime, candles=flat_candles)
    check("blocked (not eligible) when confluence is required but absent",
          r_blocked.get("eligible") is False)
    check("the rejection reason clearly explains why",
          any("no liquidity sweep or FVG found" in r for r in r_blocked.get("reasons", [])),
          str(r_blocked.get("reasons")))

    print("\n13) integration: confluence check correctly passes THROUGH "
         "(does not itself block) when a genuine matching pattern "
         "exists near the wall — proven by the 'confluence:' reason "
         "appearing and the function proceeding past that check")
    r_confirmed = slib.evaluate("bull_put_spread", make_analysis(spot=105.0, wall=99.5),
                                regime, candles=candles)
    check("the confluence check found and recorded a real match "
          "(reasons contains a 'confluence:' entry, proving the gate "
          "did not block a genuinely confirmed pattern)",
          any(r.startswith("confluence:") for r in r_confirmed.get("reasons", [])),
          str(r_confirmed.get("reasons")))
finally:
    config.save(_before)

print("\n14) config hygiene: new keys registered on both DEFAULTS and "
     "SettingsIn, defaulting to off (opt-in, doesn't silently change "
     "existing spread behavior)")
check("spread_require_liquidity_confluence defaults to False",
      config.DEFAULTS.get("spread_require_liquidity_confluence") is False)
check("spread_liquidity_proximity_pct registered",
      config.DEFAULTS.get("spread_liquidity_proximity_pct") == 0.3)
app_src = open("app.py").read()
check("both declared on SettingsIn",
      "spread_require_liquidity_confluence: bool" in app_src and
      "spread_liquidity_proximity_pct: float" in app_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
