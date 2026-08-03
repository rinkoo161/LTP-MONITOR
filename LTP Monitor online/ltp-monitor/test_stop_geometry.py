#!/usr/bin/env python3
"""test_stop_geometry.py — ONE option stop rule, not four.

2026-08-02. Option stops were set in four places with two different
clamps, two different fallbacks, and one path that ignored volatility
entirely. The journal shows the result: stop widths clustered on discrete
values (43%, 30%, 14%, 5%), which no volatility-derived rule produces. A
trade's risk depended on which code path generated it.

The checks that matter here are not "does it return a number":

  1. the ATR branch is DIMENSIONALLY correct — the old one compared a
     fraction of SPOT against a fraction of PREMIUM and therefore always
     produced the lower clamp;
  2. consolidating did not silently SHIFT the risk level;
  3. the basis is reported, so "why is this stop 43%?" is answerable;
  4. no caller keeps its own copy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_stop_geometry")

import analyzer
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
AN = open(os.path.join(HERE, "analyzer.py")).read()
AG = open(os.path.join(HERE, "agents.py")).read()
CFG = {"option_stop_atr_mult": 0.5}

print("1) the ATR branch is dimensionally correct")
# NIFTY: 0.70% daily ATR on 24,300 spot = 170 index points. At delta 0.5
# and mult 0.5 that is ~42 premium points on a 146 premium = ~29%.
sl, t1, t2, m = analyzer.option_stop_geometry(146.0, CFG, atr_pct=0.70, spot=24300)
check("uses spot to translate index ATR into premium", m["basis"] == "atr")
check("lands near the observed 29%, not on a clamp",
      0.25 < m["risk_pct"] < 0.35 and not m["clamped"],
      f"{100*m['risk_pct']:.0f}%")
# The old rule: atr_pct * 2.5 / 100 = 1.75% -> below the 5% floor.
check("the OLD expression would have hit the floor",
      (0.70 * 2.5 / 100) < analyzer.STOP_BOUNDS[0],
      "which is why every symbol shows a 5% cluster")
# Doubling volatility must roughly double the stop — a real rule scales.
_, _, _, m2 = analyzer.option_stop_geometry(146.0, CFG, atr_pct=1.40, spot=24300)
check("doubling ATR roughly doubles the stop",
      1.8 < m2["risk_pct"] / m["risk_pct"] < 2.2,
      f"{100*m['risk_pct']:.0f}% -> {100*m2['risk_pct']:.0f}%")
# Same volatility, different index: the stop must not depend on the
# index's absolute level, only on vol and premium.
_, _, _, m3 = analyzer.option_stop_geometry(814.0, CFG, atr_pct=0.70, spot=57300)
# 5e-5 tolerance: risk_pct is rounded to 4 dp for the record.
check("scale-free across indices at equal vol",
      abs(m3["risk_pct"] - (0.0070 * 57300 * 0.5 * 0.5 / 814.0)) < 5e-5
      and not m3["clamped"],
      f"BANKNIFTY {100*m3['risk_pct']:.1f}%")

print("\n2) consolidating did not shift the risk level")
_, _, _, mf = analyzer.option_stop_geometry(100.0)
check("the fallback is 30%, matching what dominated the journal",
      abs(mf["risk_pct"] - 0.30) < 1e-9, f"{100*mf['risk_pct']:.0f}%")
check("NOT the 0.15 one caller used",
      analyzer.STOP_FALLBACK_PCT != 0.15,
      "a tighter fallback would be a silent risk change under a refactor")
sl_f, t1_f, _, _ = analyzer.option_stop_geometry(100.0)
check("fallback reproduces the old rule-engine numbers exactly",
      sl_f == 70.0 and t1_f == 160.0, f"sl {sl_f} t1 {t1_f}")

print("\n3) structural risk is preferred over an average")
_, _, _, ms = analyzer.option_stop_geometry(146.0, CFG, atr_pct=0.70,
                                            spot=24300, spot_risk_pts=80)
check("a structural stop wins over ATR", ms["basis"] == "structural")
check("and is translated through delta",
      abs(ms["raw_risk_pct"] - (80 * 0.5 / 146.0)) < 5e-5,
      f"{ms['raw_risk_pct']:.4f} (rounded to 4 dp for the record)")

print("\n4) the basis is always reported")
for kw in ({"atr_pct": 0.7, "spot": 24300}, {"spot_risk_pts": 50}, {},
           {"atr_pct": 0.7}):
    _, _, _, mm = analyzer.option_stop_geometry(146.0, CFG, **kw)
    check(f"basis reported for {sorted(kw) or 'no inputs'}",
          bool(mm.get("basis")) and "risk_pct" in mm, mm.get("basis"))
check("ATR without spot refuses to reuse the wrong shortcut",
      "no spot" in analyzer.option_stop_geometry(146.0, CFG, atr_pct=0.7)[3]["basis"],
      "guessing here is what produced the original defect")

print("\n5) clamping is reported, never silent")
_, _, _, mc = analyzer.option_stop_geometry(20.0, CFG, atr_pct=1.0, spot=80000)
check("an extreme case clamps", mc["clamped"] is True)
check("and the raw value is preserved for diagnosis",
      mc["raw_risk_pct"] > mc["risk_pct"], f"{mc['raw_risk_pct']:.2f} -> {mc['risk_pct']:.2f}")

print("\n5b) atr_pct must actually REACH the geometry")
# 2026-08-03. The ATR branch was dead in production: atr_pct lives in
# regime:{sym} but the analyzer read analysis.get("atr_pct"), which never
# contains it, so every option stop silently took the 30% fallback. The
# `basis` field is what exposed it — assert every live call site now
# supplies it, because a missed site fails SILENTLY back to fallback.
AG_ = open(os.path.join(HERE, "agents.py")).read()
APP_ = open(os.path.join(HERE, "app.py")).read()
check("agents.py enriches analysis with regime atr_pct",
      'atr_pct=_reg["atr_pct"]' in AG_,
      "regime:{sym} is where the regime engine writes it")
check("app.py does the same at BOTH its call sites",
      APP_.count('atr_pct=_reg["atr_pct"]') + APP_.count('atr_pct=regime["atr_pct"]') >= 2,
      "three call sites total; a missed one degrades to fallback silently")
check("the bus object is not mutated",
      "dict(analysis, atr_pct=" in AG_ and "dict(analysis, atr_pct=" in APP_,
      "analysis comes off the bus and is shared")
# The multiplier must match the TIMEFRAME of the atr_pct being fed in.
_m = config.DEFAULTS["option_stop_atr_mult"]
_sl, _, _, _mm = analyzer.option_stop_geometry(
    56.2, {"option_stop_atr_mult": _m}, atr_pct=0.079, spot=24774)
check("at median 5m volatility the ATR branch reproduces ~30%",
      0.25 < _mm["risk_pct"] < 0.35 and _mm["basis"] == "atr",
      f"{100*_mm['risk_pct']:.0f}% — consolidating must not shift the risk level")
_, _, _, _hi = analyzer.option_stop_geometry(
    56.2, {"option_stop_atr_mult": _m}, atr_pct=0.15, spot=24774)
check("and widens when volatility doubles",
      _hi["risk_pct"] > _mm["risk_pct"] * 1.5,
      f"{100*_mm['risk_pct']:.0f}% -> {100*_hi['risk_pct']:.0f}% — the whole point")

print("\n6) no caller keeps its own copy")
check("the flat 30% rule is gone from analyzer",
      "* 0.70, 1)" not in AN, "three sites used it")
check("the mtf clamp is gone from agents",
      "entry * 0.10), entry * 0.60" not in AG,
      "that site had its own [10%, 60%] bounds")
check("agents routes through the shared helper",
      AG.count("option_stop_geometry") >= 2)
check("one bounds definition", AN.count("STOP_BOUNDS = ") == 1)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all stop-geometry checks passed")
