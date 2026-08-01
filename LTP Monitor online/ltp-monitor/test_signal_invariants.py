"""v58.44 — pending items 5 and 18.

ITEM 5. 18 rejections/day at "risk-reward 0.8 (need >=2.0)" plus 8/day
at "strike not OTM". ROOT CAUSE: the LLM prompt STATES "Min 1:2 RR
(target1-entry >= 2*(entry-stoploss))" and "ATM/ITM strikes only, never
OTM" — and nothing validated the reply. The rule-engine FALLBACK never
had the problem (hard-coded ltp*0.70 / ltp*1.60, strike = ATM), so the
gate was right and the generator was wrong. Same class of bug as the
already-fixed `signal` field check, one field over.

ITEM 18. Options and S7 have had a shadow journal since v51; futures
never did. The one class demonstrably losing money was the only one
whose rejected signals left no record — and after v58.39 added a rupee
cap and an ADX gate, an over-tight filter is indistinguishable from a
correctly-filtered bad trade without one.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import analyzer, config, agents
CFG = dict(config.DEFAULTS)
AN = {"atm": 24200, "strikes": [{"strike": 24200,
                                 "ce": {"ltp": 150.0}, "pe": {"ltp": 148.0}}]}

print("1) The exact live signal that was rejected")
live = {"signal": "BUY_PE", "strike": 24100, "entry": 190.35,
        "stoploss": 160.55, "target1": 213.8, "target2": 240.0}
out, rep = analyzer.enforce_signal_invariants(dict(live), AN, CFG)
check("it IS repaired, not silently passed", len(rep) > 0, f"{len(rep)} repairs")
check("strike snapped to ATM", out["strike"] == 24200)
check("premium re-read for the NEW strike", out["entry"] == 148.0,
      "190.35 was the premium for 24100 — keeping it would price a "
      "contract we are not buying")
rr = (out["target1"] - out["entry"]) / (out["entry"] - out["stoploss"])
check("risk-reward now clears the 1.95 gate", rr >= 1.95, f"rr={rr:.2f}")
check("every repair is recorded on the signal", "invariant_repairs" in out)

print("\n2) Correct signals pass through untouched")
good = {"signal": "BUY_CE", "strike": 24200, "entry": 150.0,
        "stoploss": 105.0, "target1": 240.0, "target2": 300.0}
out2, rep2 = analyzer.enforce_signal_invariants(dict(good), AN, CFG)
check("no repairs on a compliant signal", rep2 == [], str(rep2))
check("values unchanged", out2["target1"] == 240.0 and out2["strike"] == 24200)
check("WAIT is never touched",
      analyzer.enforce_signal_invariants({"signal": "WAIT"}, AN, CFG)[1] == [])
check("a None signal is safe",
      analyzer.enforce_signal_invariants(None, AN, CFG)[0] is None)

print("\n3) Degenerate inputs fall back rather than inventing numbers")
bad = {"signal": "BUY_CE", "strike": 24200, "entry": 100.0,
       "stoploss": 120.0, "target1": 130.0}   # stop ABOVE entry
out3, rep3 = analyzer.enforce_signal_invariants(dict(bad), AN, CFG)
check("stop above entry is caught", any("invalid" in r for r in rep3), str(rep3))
check("reset to the rule-engine's own 30%/60%",
      out3["stoploss"] == 70.0 and out3["target1"] == 160.0)
rr3 = (out3["target1"] - out3["entry"]) / (out3["entry"] - out3["stoploss"])
check("and that fallback itself satisfies the gate", rr3 >= 1.95, f"rr={rr3:.2f}")

print("\n4) Policy is respected, not hard-coded")
out4, rep4 = analyzer.enforce_signal_invariants(
    dict(live), AN, dict(CFG, option_strike_policy="any"))
check("policy 'any' leaves the strike alone", out4["strike"] == 24100)
check("but the RR floor still applies",
      any("target1" in r for r in rep4), str(rep4))
out5, _ = analyzer.enforce_signal_invariants(
    {"signal": "BUY_PE", "strike": 24300, "entry": 148.0, "stoploss": 103.6,
     "target1": 236.8}, AN, dict(CFG, option_strike_policy="atm_or_otm"))
check("policy 'atm_or_otm' snaps an ITM put back to ATM", out5["strike"] == 24200)

ASRC = open("analyzer.py").read()
check("wired into the LLM path", "enforce_signal_invariants(sig, analysis" in ASRC)
check("a repair-layer failure cannot kill signal generation",
      "check failed:" in ASRC, "no signal at all is a silent outage")
check("signal_min_rr registered", "signal_min_rr" in config.DEFAULTS)
check("it sits above the RiskAgent's 1.95 floor",
      config.DEFAULTS["signal_min_rr"] >= 1.95)

print("\n5) Futures shadow journal")
GS = open("agents.py").read()
check("writer exists", "def log_futures_shadow" in GS)
check("tagged kind='futures' so one reader serves both",
      '"kind": "futures"' in GS)
check("records the gate map", '"gates": gates' in GS)
check("extracts which gates blocked", "failed_gates" in GS)
check("logs REJECTED, not only taken", '"REJECTED"' in GS.split("def log_futures_shadow")[1][:1800])
check("called on the budget block", GS.count("log_futures_shadow(self.bus") >= 3,
      f"{GS.count('log_futures_shadow(self.bus')} call sites")
check("called when sizing refuses (the new rupee cap)",
      "log_futures_shadow(self.bus, sym, side, gates, False, sizing_why" in GS,
      "an over-tight cap must be distinguishable from a good filter")
check("called on the eligible path too", '"eligible"' in GS)
# Sliced to the END OF THE FUNCTION, not to a character count. This
# used to read [:2000] and broke the day the fail-loud rewrite lengthened
# the docstring — the property was still true, the ruler was too short.
_body = GS.split("def log_futures_shadow")[1].split("\ndef ")[0]
check("writes to the same journal options use", "SHADOW_PATH" in _body,
      f"function body is {len(_body)} chars")

_e = agents.log_futures_shadow(None, "NIFTY", "LONG",
                               {"budget": "blocked (spent)"}, False, "budget")
check("entry carries a verdict", _e["verdict"] == "REJECTED")
check("entry names the failed gate", _e["failed_gates"] == ["budget"])
check("id distinguishes futures from options", "-FUT-" in _e["id"])

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
