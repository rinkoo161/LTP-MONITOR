#!/usr/bin/env python3
"""test_signal_entry_price.py — the entry price must belong to the
instrument the signal names.

Observed live, 2026-08-06 12:01:03:

    SENSEX BUY_CE strike 78800  entry 123.45  sl 96.5  t1 180  t2 240
    actual SENSEX 78800 CE:  Rs 363.60
    actual SENSEX 79400 CE:  Rs 123.75   <-- the price it actually used

The model named one strike and priced a DIFFERENT one, then derived the
whole geometry from the wrong instrument. Every existing invariant
passed it, because 123.45 / 96.5 / 180 is a perfectly well-formed 2:1
trade — of some other option. The same bogus 123.45 appeared in three
consecutive signals including a BUY_PE -> BUY_CE flip with the price
levels unchanged.

The consequence downstream: the fill happens at the LIVE price (v59.29)
while the targets stay at the bogus ones, so the position is already
past target2 the moment it opens. That is the churn loop's входной
condition, and it is what the v59.31 guard was refusing all afternoon.

THREE BANDS, because "stale" and "wrong instrument" need different
answers and one threshold cannot give both.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_signal_entry_price")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import analyzer
import config

CFG = {"signal_min_rr": 2.0, "signal_entry_tolerance_pct": 10.0,
       "signal_entry_rescale_max_pct": 40.0}


def _analysis(strike, ce_ltp, pe_ltp=100.0):
    return {"strikes": [{"strike": strike,
                         "ce": {"ltp": ce_ltp}, "pe": {"ltp": pe_ltp}}],
            "spot": 78800.0}


def _sig(entry, sl, t1, t2, strike=78800.0, direction="BUY_CE"):
    return {"signal": direction, "strike": strike, "entry": entry,
            "stoploss": sl, "target1": t1, "target2": t2, "confidence": 80}


print("1) the live case — a price from another strike is REJECTED")
out, reps = analyzer.enforce_signal_invariants(
    _sig(123.45, 96.5, 180.0, 240.0), _analysis(78800.0, 363.60), cfg=CFG)
check("the signal is turned into NO_TRADE", out["signal"] == "NO_TRADE",
      f"{out['signal']} — 123.45 is 66% from the live 363.60")
check("and it says WHY, naming both prices",
      any("123.45" in r and "363.6" in r for r in reps), str(reps)[:150])
check("it is NOT silently rescaled into a plausible trade",
      not any("rescaled" in r for r in reps),
      "rescaling a wrong-instrument price launders a broken signal")

print("\n2) normal 60s staleness is LEFT ALONE")
# A real vwap_pullback signal from the same minute: 330.4 vs a live
# 363.6 is 9% — the pack is 60s old and the option moved.
out2, reps2 = analyzer.enforce_signal_invariants(
    _sig(330.4, 280.84, 429.52, 462.73), _analysis(78800.0, 363.60), cfg=CFG)
check("it still trades", out2["signal"] == "BUY_CE", out2["signal"])
check("entry is untouched", out2["entry"] == 330.4, str(out2["entry"]))
check("no rescale repair was recorded",
      not any("rescaled" in r for r in reps2), str(reps2)[:120])

print("\n3) the middle band RESCALES and preserves risk-reward exactly")
# entry 300 vs live 380 = 27%: same instrument, materially moved.
sig3 = _sig(300.0, 250.0, 400.0, 450.0)
rr_before = (sig3["target1"] - sig3["entry"]) / (sig3["entry"] - sig3["stoploss"])
out3, reps3 = analyzer.enforce_signal_invariants(
    dict(sig3), _analysis(78800.0, 380.0), cfg=CFG)
check("it still trades", out3["signal"] == "BUY_CE", out3["signal"])
check("entry becomes the LIVE price", out3["entry"] == 380.0, str(out3["entry"]))
rr_after = ((out3["target1"] - out3["entry"])
            / (out3["entry"] - out3["stoploss"]))
check("risk-reward is preserved exactly",
      abs(rr_after - rr_before) < 0.01,
      f"{rr_before:.3f} -> {rr_after:.3f}")
check("the stop is still BELOW the new entry",
      out3["stoploss"] < out3["entry"],
      f"sl={out3['stoploss']} entry={out3['entry']}")
check("targets are still ABOVE it in order",
      out3["entry"] < out3["target1"] < out3["target2"],
      f"{out3['entry']} < {out3['target1']} < {out3['target2']}")
check("the repair says what happened",
      any("rescaled" in r for r in reps3), str(reps3)[:150])

print("\n4) it reads the leg the signal actually names")
# A BUY_PE must be checked against the PE, not the CE.
out4, reps4 = analyzer.enforce_signal_invariants(
    _sig(105.0, 84.0, 147.0, 168.0, direction="BUY_PE"),
    _analysis(78800.0, ce_ltp=363.60, pe_ltp=100.0), cfg=CFG)
check("a PE priced near the PE is fine", out4["signal"] == "BUY_PE",
      f"{out4['signal']} — 105 vs a live PE of 100 is 5%")
out5, _ = analyzer.enforce_signal_invariants(
    _sig(363.0, 290.0, 509.0, 582.0, direction="BUY_PE"),
    _analysis(78800.0, ce_ltp=363.60, pe_ltp=100.0), cfg=CFG)
check("a PE priced at the CE's price is REJECTED",
      out5["signal"] == "NO_TRADE",
      "reading the wrong leg is the same class of error as the wrong strike")

print("\n5) it cannot fire when there is nothing to compare against")
out6, reps6 = analyzer.enforce_signal_invariants(
    _sig(123.45, 96.5, 180.0, 240.0), {"strikes": []}, cfg=CFG)
check("no live price -> the signal is left alone",
      out6["signal"] == "BUY_CE",
      "an absent chain must not become a reason to reject everything")
out7, _ = analyzer.enforce_signal_invariants(
    _sig(123.45, 96.5, 180.0, 240.0), _analysis(99999.0, 363.60), cfg=CFG)
check("a strike that is not in the chain -> left alone",
      out7["signal"] == "BUY_CE", str(out7["signal"]))

print("\n6) the thresholds are registered, or they vanish on save")
for k in ("signal_entry_tolerance_pct", "signal_entry_rescale_max_pct"):
    check(f"{k} in DEFAULTS", k in config.DEFAULTS,
          "config.save() silently drops unregistered keys")
check("the tolerance band is below the rescale band",
      config.DEFAULTS["signal_entry_tolerance_pct"]
      < config.DEFAULTS["signal_entry_rescale_max_pct"],
      "otherwise the middle band is empty and nothing is ever rescaled")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all signal entry-price checks passed")
