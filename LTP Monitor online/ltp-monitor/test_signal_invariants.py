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

print("\n3b) Stop WIDTH is bounded (B8, found live 2026-08-04)")
# The real signal from that morning's first repair. It scored EXACTLY
# 2.00 on the RR floor because rr = (target-entry)/(entry-stop): the
# generator cleared "min 1:2" by WIDENING THE STOP rather than moving the
# target, and nothing validated the denominator. `0 < sl < entry` is a
# validity check, not a width one.
live_wide = {"signal": "BUY_CE", "strike": 24200, "entry": 132.8,
             "stoploss": 9.3, "target1": 379.8, "target2": 420.0}
outw, repw = analyzer.enforce_signal_invariants(dict(live_wide), AN, CFG)
_frac = (outw["entry"] - outw["stoploss"]) / outw["entry"]
# Strike == ATM on purpose: the strike-repair branch above already
# re-derives the stop through option_stop_geometry, so a non-ATM fixture
# would mask the clamp entirely and the test would pass without it.
check("a 93%-of-premium stop is clamped", _frac <= analyzer.STOP_BOUNDS[1] + 1e-9,
      f"stop is now {100*_frac:.0f}% (bound {100*analyzer.STOP_BOUNDS[1]:.0f}%)")
check("and the repair says so", any("stoploss" in r for r in repw), str(repw))
check("clamping runs BEFORE the RR floor",
      (outw["target1"] - outw["entry"]) / (outw["entry"] - outw["stoploss"]) >= 1.95,
      "the RR repair divides by (entry - sl); on the OLD stop it would "
      "size a target off a value about to change")
# The case the rupee caps could not catch: same absurd fraction, cheap
# premium, so the resulting rupee risk clears both caps.
cheap = {"signal": "BUY_CE", "strike": 24200, "entry": 30.0,
         "stoploss": 2.1, "target1": 86.0}
outc, _ = analyzer.enforce_signal_invariants(dict(cheap), AN, CFG)
_fc = (outc["entry"] - outc["stoploss"]) / outc["entry"]
check("a CHEAP option with the same 93% stop is clamped too",
      _fc <= analyzer.STOP_BOUNDS[1] + 1e-9,
      f"{100*_fc:.0f}% — at entry 30 the unclamped stop risked only "
      f"₹1,814 and passed every rupee cap")
# Both bounds, and the no-op case.
tight = {"signal": "BUY_CE", "strike": 24200, "entry": 150.0,
         "stoploss": 149.0, "target1": 400.0}
outt, _ = analyzer.enforce_signal_invariants(dict(tight), AN, CFG)
_ft = (outt["entry"] - outt["stoploss"]) / outt["entry"]
check("an absurdly TIGHT stop is widened to the floor",
      _ft >= analyzer.STOP_BOUNDS[0] - 1e-9,
      f"{100*_ft:.1f}% (floor {100*analyzer.STOP_BOUNDS[0]:.0f}%) — a 0.7% "
      f"stop is noise, not risk control")
ok_stop = {"signal": "BUY_CE", "strike": 24200, "entry": 150.0,
           "stoploss": 105.0, "target1": 240.0, "target2": 300.0}
outo, repo = analyzer.enforce_signal_invariants(dict(ok_stop), AN, CFG)
check("a stop already inside the bounds is untouched",
      outo["stoploss"] == 105.0 and not any("stoploss" in r for r in repo),
      str(repo))

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

# 2026-08-03 — the check above passed for months while the repair layer
# was COMPLETELY unobservable. `log` defaults to a no-op, the production
# caller passed only three arguments, and `invariant_repairs` (the other
# half of the record) is read by nothing. So "wired in" was true and
# worth nothing: repairs ran on real trades and left no trace. Assert the
# message reaches a SINK, by driving the real function with a real one.
_seen = []
analyzer.enforce_signal_invariants(dict(live), AN, CFG, log=_seen.append)
check("a repair actually reaches the log sink", len(_seen) == 1,
      _seen[0][:90] if _seen else "NOTHING LOGGED — this is the v58.44 bug")
check("and the message names what was repaired",
      bool(_seen) and "->" in _seen[0],
      "an operator needs the before/after, not just a count")
_quiet = []
analyzer.enforce_signal_invariants(dict(good), AN, CFG, log=_quiet.append)
check("a clean signal logs nothing", not _quiet, str(_quiet))

# The three checks above pass on the PRE-FIX code too — they hand the
# function a sink explicitly, and that never broke. What broke is the
# production path, where nobody handed it one. So drive THAT: patch the
# LLM transport so ai_signal takes its AI branch, and assert the message
# comes out the far end. This is the check that actually fails on the bug.
_llm = []


class _AnalysisWithDefaults(dict):
    """ai_signal builds its prompt from a fixed key tuple (analyzer.py,
    `compact = {k: analysis[k] ...}`). Rather than hard-code a copy of
    that tuple here — which would drift silently the moment the producer
    adds a key — supply the real fields this test cares about and let any
    other required key default. The point of this check is the LOG SINK,
    not the prompt contents."""
    def __missing__(self, k):
        return 0


AN_FULL = _AnalysisWithDefaults(AN)
AN_FULL["symbol"] = "NIFTY"
# The per-strike rows are consumed the same way (row["volume"], the
# leg dicts, ...), so they default too. AN itself is left untouched —
# the checks above depend on its exact contents.
AN_FULL["strikes"] = []
for _r in AN["strikes"]:
    _row = _AnalysisWithDefaults(_r)
    for _leg in ("ce", "pe"):
        _row[_leg] = _AnalysisWithDefaults(_r[_leg])
    AN_FULL["strikes"].append(_row)
_orig_cj = analyzer._claude_json
analyzer._claude_json = lambda *a, **k: (json.dumps(
    {"signal": "BUY_CE", "strike": 24100, "entry": 190.35, "stoploss": 133.2,
     "target1": 247.5, "target2": 285.0, "spot_invalidation": 23900,
     "confidence": 80, "reasons": ["t"], "risk_note": "t"}), None)
try:
    analyzer.ai_signal(AN_FULL, context=None, log=_llm.append)
    _reached = bool(_llm)
except TypeError as e:
    _reached = False
    _llm = [f"TypeError: {e}"]
finally:
    analyzer._claude_json = _orig_cj
check("the PRODUCTION path (ai_signal) surfaces repairs", _reached,
      (_llm[0][:95] if _llm else "no log emitted")
      + "  <- pre-fix this is where the message vanished")
check("the LLM caller passes a real sink, not the default",
      "log=log or" in ASRC, "analyzer must forward what agents gives it")
GSRC = open("agents.py").read()
check("and the agent supplies one from the bus",
      "ai_signal(analysis, context=context," in GSRC
      and "self.bus.log(self.name" in GSRC.split("ai_signal(analysis")[1][:300],
      "no bus sink = the message goes nowhere again")
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
