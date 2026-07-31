"""v58 — tests for the Unified AI Probability stage: one combined
number from Option Chain + Decision Engine + Institutional + Technical
+ the empirical historical estimate, closing the last piece of the
original pipeline framing that wasn't yet built (the live feedback
loop half was already done in an earlier session; v56's optimizer
separately addressed "backtest doesn't search for the best value").

Run:  python3 test_unified_probability.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
import ai_probability_engine as ape
import agents
import config

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


SIG = {"signal": "BUY_CE", "_pre_decision_confidence": 78}
DECISION = {"adjusted_confidence": 84}
PROB = {"unavailable": False, "probability_pct": 70,
       "confidence_in_estimate": "high", "sample_size": 30}
INST_AGREE = {"score_unavailable": False, "institutional_score": 65,
             "institutional_bias": "Bullish"}
TECH_AGREE = {"technical_score": 72, "technical_bias": "Strong Bullish"}

print("1) all 5 inputs available — full composite")
r = ape.unified_probability(SIG, DECISION, PROB, INST_AGREE, TECH_AGREE)
check("not unavailable", r["unavailable"] is False)
check("all 5 components present", len(r["components"]) == 5, str(r["components"]))
check("weights sum to 1.0 after renormalization",
      abs(sum(r["weights"].values()) - 1.0) < 1e-6, str(sum(r["weights"].values())))
check("result is a sensible weighted blend (between the min and max "
      "of its own components)",
      min(r["components"].values()) <= r["unified_probability_pct"] <= max(r["components"].values()),
      str(r))

print("\n2) institutional disagreement inverts magnitude, doesn't just ignore it")
inst_disagree = {"score_unavailable": False, "institutional_score": 80,
                 "institutional_bias": "Bearish"}
r2 = ape.unified_probability(SIG, DECISION, PROB, inst_disagree, TECH_AGREE)
check("a STRONG opposing institutional reading scores as inverted (20), "
      "not simply excluded or treated as neutral",
      r2["components"]["institutional"] == 20, str(r2["components"]))
check("the unified number is correctly LOWER when institutional opposes "
      "vs when it agrees",
      r2["unified_probability_pct"] < r["unified_probability_pct"],
      f"{r2['unified_probability_pct']} vs {r['unified_probability_pct']}")

print("\n3) graceful degradation — missing components excluded, not faked")
r3 = ape.unified_probability(SIG, DECISION, None, None, None)
check("missing institutional/technical/probability excluded from components",
      set(r3["components"].keys()) == {"option_chain", "decision_engine"},
      str(r3["components"]))
check("weights still sum to 1.0 after excluding 3 of 5",
      abs(sum(r3["weights"].values()) - 1.0) < 1e-6)
check("basis honestly reports 2/5 inputs used",
      "2/5" in r3["basis"], r3["basis"])

r4 = ape.unified_probability({"signal": "BUY_CE"}, None, None, None, None)
check("zero inputs available returns unavailable:True with None, not a "
      "fabricated number",
      r4["unified_probability_pct"] is None and r4["unavailable"] is True)

print("\n4) non-actionable signal (WAIT) never gets a probability")
r5 = ape.unified_probability({"signal": "WAIT"}, DECISION, PROB, INST_AGREE, TECH_AGREE)
check("a WAIT signal returns unavailable, not a fabricated score",
      r5["unified_probability_pct"] is None and r5["unavailable"] is True)

print("\n5) low-confidence historical estimate contributes LESS than a "
     "high-confidence one of the same raw percentage")
prob_low_n = {"unavailable": False, "probability_pct": 70,
             "confidence_in_estimate": "low", "sample_size": 3}
r_high = ape.unified_probability(SIG, DECISION, PROB, INST_AGREE, TECH_AGREE)
r_low = ape.unified_probability(SIG, DECISION, prob_low_n, INST_AGREE, TECH_AGREE)
check("a 'low' confidence_in_estimate gets a smaller weight than 'high' "
      "for the SAME raw probability_pct",
      r_low["weights"]["historical_probability"] < r_high["weights"]["historical_probability"],
      f"{r_low['weights']['historical_probability']} vs {r_high['weights']['historical_probability']}")

print("\n6) full end-to-end through the REAL RiskAgent.evaluate()")
bus = agents.Bus()
risk = agents.RiskAgent(bus, {})
bus.set("symbols", ["NIFTY"])
bus.set("positions", {})
bus.set("trades_today", 0)
bus.set("institutional:NIFTY", {"score_unavailable": False, "institutional_score": 70,
                                "institutional_bias": "Bullish"})
bus.set("technical:NIFTY", {"technical_score": 65, "technical_bias": "Bullish"})
bus.set("regime:NIFTY", {"regime": "trending-up"})
bus.set("closed_trades", [])
real_market_open = agents.market_open
agents.market_open = lambda: True
_before = config.load().get("learning_feedback_enabled")
config.save({"ai_decision_engine_enabled": True, "learning_feedback_enabled": False})
try:
    sig = {"signal": "BUY_CE", "confidence": 75, "entry": 100, "stoploss": 70,
          "target1": 160, "target2": 180, "reasons": []}
    job = {"symbol": "NIFTY", "signal": sig, "analysis": {"spot": 23800}}
    risk.evaluate(job)
    check("sig['unified_probability'] was attached by the real evaluate() call",
          sig.get("unified_probability", {}).get("unavailable") is False,
          str(sig.get("unified_probability")))
    check("sig['_pre_decision_confidence'] was captured before the "
          "Decision Engine overwrote sig['confidence']",
          sig.get("_pre_decision_confidence") == 75, str(sig.get("_pre_decision_confidence")))
    check("the unified number used 4/5 inputs (no historical trades seeded)",
          "4/5" in sig["unified_probability"]["basis"], sig["unified_probability"]["basis"])
finally:
    agents.market_open = real_market_open
    config.save({"learning_feedback_enabled": _before})

print("\n7) frontend wiring present")
h = open("static/dashboard.html").read()
check("dashboard reads s.unified_probability", "s.unified_probability" in h)
check("dashboard displays the combined percentage", "unified_probability_pct" in h)

print("\n8) config hygiene fix made alongside this feature: two flags "
     "that were read via cfg.get(key, True) but never registered")
check("ai_decision_engine_enabled now registered in config.DEFAULTS",
      config.DEFAULTS.get("ai_decision_engine_enabled") is True)
check("learning_feedback_enabled now registered in config.DEFAULTS",
      config.DEFAULTS.get("learning_feedback_enabled") is True)
app_src = open("app.py").read()
check("both also declared on SettingsIn (else a future Settings POST "
      "would silently drop them, the v54 lesson)",
      "ai_decision_engine_enabled: bool" in app_src and
      "learning_feedback_enabled: bool" in app_src)

print("\n9) regression guard: no duplicate top-level JS function "
     "declarations anywhere in the dashboard (2026-07-27 — a real bug:"
     " loadInstitutional() was declared twice, the later one silently "
     "shadowing the earlier one, so the ORIGINAL Feature #5 panel's "
     "own refresh logic never ran again after the newer page was "
     "added — permanently stuck on its static 'Loading...' text, not "
     "a transient timing issue as first assumed)")
import re as _re
from collections import Counter as _Counter
_h = open("static/dashboard.html").read()
_scripts = _re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', _h, _re.S)
_full = "\n".join(_scripts)
_names = _re.findall(r'^(?:async )?function (\w+)\(', _full, _re.M)
_dupes = {k: v for k, v in _Counter(_names).items() if v > 1}
check("zero duplicate function declarations in the shipped dashboard",
      len(_dupes) == 0, str(_dupes))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
