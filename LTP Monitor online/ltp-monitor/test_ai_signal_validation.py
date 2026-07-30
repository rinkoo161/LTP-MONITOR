"""v58.8 — tests for a real bug found from a live activity log: the
local Ollama model (qwen2.5:3b per the uploaded config) was literally
echoing back the prompt's own schema placeholder "BUY_CE|BUY_PE"
(meant as "pick one of these three") as if it were a valid answer.
Nothing validated `sig["signal"]` against the actual allowed values
before this fix, so the malformed value sailed through into every
downstream check — the regime gate can never match a value that isn't
literally "BUY_CE" or "BUY_PE", so this manifested as 65 needless
signal rejections in one session, easy to misread as "the regime
gate is blocking a 90%-confidence trade" rather than what it actually
was: a malformed AI response that was never a valid trade at all.

Run:  python3 test_ai_signal_validation.py
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyzer

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def make_analysis(sym="NIFTY"):
    # unique symbol per case avoids the AI response cache (_ai_cache)
    # returning a stale result from an earlier case in this same run
    return {"symbol": sym, "spot": 24000, "atm": 24000,
           "strikes": [{"strike": 24000, "ce": {"ltp": 83, "volume": 100, "state": "x"},
                       "pe": {"ltp": 80, "volume": 100, "state": "x"}}],
           "signal_lines": {"R": [], "S": []}, "bias": "BULLISH", "risk_meter": 40,
           "momentum": {"trend": "flat"}, "support": [23900], "resistance": [24100],
           "max_pain": 24000, "confidence": 70, "pcr_oi": 0.9, "market_state": "TRENDING"}


MALFORMED = ('{"signal":"BUY_CE|BUY_PE","strike":24000,"entry":83.05,'
            '"stoploss":67.75,"target1":134.8,"target2":null,'
            '"spot_invalidation":23900,"confidence":90,"reasons":["test"],'
            '"risk_note":"test"}')
VALID_CE = ('{"signal":"BUY_CE","strike":24000,"entry":83.05,"stoploss":67.75,'
           '"target1":134.8,"target2":180,"spot_invalidation":23900,'
           '"confidence":90,"reasons":["test"],"risk_note":"test"}')
VALID_WAIT = ('{"signal":"WAIT","strike":24000,"entry":0,"stoploss":0,'
             '"target1":0,"target2":0,"spot_invalidation":0,"confidence":0,'
             '"reasons":[],"risk_note":""}')

print("1) the EXACT malformed response from the real activity log is "
     "now rejected, falling back to the rule engine rather than "
     "sailing through unvalidated")
with mock.patch("analyzer._claude_json", return_value=(MALFORMED, None)):
    sig = analyzer.ai_signal(make_analysis("NIFTYTEST1"))
check("falls back to rule-engine source, doesn't accept the malformed value",
      "invalid signal value" in sig.get("source", ""), sig.get("source"))
check("the fallback itself still produces an ACTIONABLE signal (WAIT/"
      "BUY_CE/BUY_PE from the deterministic rule engine), not a crash",
      sig.get("signal") in ("BUY_CE", "BUY_PE", "WAIT"), str(sig.get("signal")))

print("\n2) a genuinely valid BUY_CE response is still accepted correctly "
     "(the fix doesn't over-correct into rejecting everything)")
with mock.patch("analyzer._claude_json", return_value=(VALID_CE, None)):
    sig2 = analyzer.ai_signal(make_analysis("NIFTYTEST2"))
check("valid BUY_CE accepted with source='AI'",
      sig2.get("signal") == "BUY_CE" and sig2.get("source") == "AI",
      str((sig2.get("signal"), sig2.get("source"))))

print("\n3) a genuinely valid WAIT response is still accepted correctly")
with mock.patch("analyzer._claude_json", return_value=(VALID_WAIT, None)):
    sig3 = analyzer.ai_signal(make_analysis("NIFTYTEST3"))
check("valid WAIT accepted with source='AI'",
      sig3.get("signal") == "WAIT" and sig3.get("source") == "AI",
      str((sig3.get("signal"), sig3.get("source"))))

print("\n4) a genuinely valid BUY_PE response is still accepted correctly")
valid_pe = VALID_CE.replace('"BUY_CE"', '"BUY_PE"')
with mock.patch("analyzer._claude_json", return_value=(valid_pe, None)):
    sig4 = analyzer.ai_signal(make_analysis("NIFTYTEST4"))
check("valid BUY_PE accepted with source='AI'",
      sig4.get("signal") == "BUY_PE" and sig4.get("source") == "AI",
      str((sig4.get("signal"), sig4.get("source"))))

print("\n5) other garbage values (not just the one specific malformed "
     "string seen in the log) are also caught by the same validation")
garbage = MALFORMED.replace("BUY_CE|BUY_PE", "SELL")
with mock.patch("analyzer._claude_json", return_value=(garbage, None)):
    sig5 = analyzer.ai_signal(make_analysis("NIFTYTEST5"))
check("an entirely unexpected signal value is also rejected, not just "
      "the specific pipe-joined case",
      "invalid signal value" in sig5.get("source", ""), sig5.get("source"))

print("\n6) the prompt itself was also hardened (defense in depth, not "
     "just catching it after the fact)")
src = open("analyzer.py").read()
check("prompt now explicitly forbids combining values or using '|'",
      "never combine them" in src and "never output the word" in src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
