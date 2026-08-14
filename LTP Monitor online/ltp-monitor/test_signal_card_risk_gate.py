"""v58.5 — tests for a real bug found from a live report: the "AI Trade
Signal" card showed a high-confidence, actionable-looking BUY_CE
recommendation with an ENABLED "Confirm & place order" button, while
the risk gate's own regime check (already computed and returned by the
backend as `risk_preview_checks`) showed a clear failure for that exact
direction. renderSignal()'s `canTrade` only ever checked the signal's
DIRECTION, never whether the already-computed risk preview actually
passed.

Critically: the underlying ORDER was never actually at risk — 
manual_trade() re-runs risk.evaluate() server-side before anything is
placed, confirmed in agents.py. This was a misleading DISPLAY gap, not
a capital-safety gap — but a real and confusing one, fixed here.

Run:  python3 test_signal_card_risk_gate.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) backend already computes and returns risk_preview_checks — "
     "confirming this is a pure frontend fix, no backend change needed")
app_src = open("app.py").read()
check("/api/signal already attaches risk_preview_checks to every "
      "actionable signal preview",
      'sig["risk_preview_checks"] = checks' in app_src)
check("that preview call is read-only (documented as safe to run "
      "speculatively, not a live order)",
      "risk.evaluate() is read-only" in app_src)

agents_src = open("agents.py").read()
check("manual_trade() re-runs risk.evaluate() server-side before "
      "placing anything — confirms the ORDER itself was never at risk "
      "from the frontend display bug",
      "ok, checks = risk.evaluate(job)" in agents_src and
      "ex.place(job, manual=True)" in agents_src)
check("each check string is prefixed with \u2713/\u2717 — confirms the exact "
      "format the frontend fix parses",
      # v59.87 renamed the variable after the prefix (`label` -> `text`,
      # since a check may now carry a separate failure phrasing). The
      # EMITTED format is byte-identical, so assert the prefix
      # construction rather than the variable name that follows it.
      '("\u2713" if cond else "\u2717") + " "' in agents_src)

print("\n2) the shipped frontend fix: canTrade now actually consults "
     "risk_preview_checks, not just the signal direction")
h = open("static/dashboard.html").read()
check("renderSignal computes failedChecks from risk_preview_checks",
      "risk_preview_checks||[]" in h)
check("canTrade requires zero failed checks, not just a valid direction",
      re.search(r'canTrade=\([^)]*\)&&failedChecks\.length===0', h) is not None,
      "canTrade computation")
check("a failing check is surfaced directly on the card (not only "
      "discoverable after clicking through to a rejection alert)",
      "Would be blocked right now" in h)

print("\n3) logic verification against the exact reported scenario "
     "(BUY_CE with a failed regime-gate check) and two control cases")


def can_trade(s):
    checks = s.get("risk_preview_checks") or []
    failed = [c for c in checks if c.startswith("\u2717")]
    return (s.get("signal") in ("BUY_CE", "BUY_PE")) and len(failed) == 0, failed


reported = {"signal": "BUY_CE", "risk_preview_checks": [
    "\u2713 confidence 86>=70", "\u2713 valid price points",
    "\u2713 risk-reward 2.4 (need >=2.0)",
    "\u2713 strike 57100.0 not OTM (ATM 57100.0)",
    "\u2717 regime 'trending-down' allows ['BUY_PE']",
    "\u2713 timeframe confluence for CE (mixed-bull)",
    "\u2713 no active news risk", "\u2713 daily loss limit",
    "\u2713 daily profit target not yet reached",
    "\u2713 fresh BANKNIFTY data (0s old)"]}
ct, failed = can_trade(reported)
check("the EXACT reported scenario now computes canTrade=False",
      ct is False, str(ct))
check("the failed regime check is the one surfaced",
      len(failed) == 1 and "regime" in failed[0], str(failed))

clean = {"signal": "BUY_PE", "risk_preview_checks": [
    "\u2713 confidence 80>=70", "\u2713 regime allows ['BUY_PE']",
    "\u2713 no active news risk"]}
ct2, failed2 = can_trade(clean)
check("a genuinely all-checks-passing signal still computes canTrade=True "
      "(the fix doesn't over-correct into blocking everything)",
      ct2 is True and not failed2, str((ct2, failed2)))

wait = {"signal": "WAIT"}
ct3, failed3 = can_trade(wait)
check("a WAIT signal (no risk_preview_checks at all, since the backend "
      "only computes it for BUY_CE/BUY_PE) still correctly computes "
      "canTrade=False",
      ct3 is False, str(ct3))

print("\n" + "=" * 60)
failed_checks = [l for l, ok in results if not ok]
if failed_checks:
    print(f"FAIL ({len(failed_checks)}/{len(results)}):")
    for f in failed_checks:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
