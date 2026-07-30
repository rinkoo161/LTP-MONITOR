"""v58.23+ — tests for the AI probability per-factor breakdown becoming
always-visible, per the wireframe's own explicit design note for this
exact card: "AI probability sits top-right as the single number that
summarises every upstream engine, with its contributors always visible
beneath it rather than behind a hover."

The backend (ai_probability_engine.py's unified_probability()) already
computed and returned per-factor components/weights — nothing new was
built there. The gap was purely that the frontend buried this in a
hover-only `title` tooltip attribute, undiscoverable on touch devices
and not matching the wireframe's explicit intent. Fixed by rendering
each factor as a visible row: label, signed contribution-from-neutral
((component score - 50) * weight — an honest transformation of data
already sent, not a new invented figure), raw score, weight, and a
fill bar.

Run:  python3 test_ai_probability_visible_breakdown.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


h = open("static/dashboard.html").read()

print("1) source-level: the hover-only tooltip is genuinely gone, not "
     "just supplemented")
check('the old title="..." tooltip attribute pattern for this card is '
     "removed",
      'title="\'+Object.entries(unified.components)' not in h)
check("a labels dict maps raw component keys to readable names",
      'const labels={option_chain:"Option chain"' in h)
check("the contribution formula is the honest (score - 50) * weight "
     "transformation of already-sent data, not a new invented number",
      "const contribution=(val-50)*w;" in h)

print("\n2) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/ai_prob_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/ai_prob_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n3) REAL BROWSER VERIFICATION: the breakdown genuinely renders "
     "visibly (not behind a hover) with realistic mocked data matching "
     "the actual backend's unified_probability shape")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8942, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

mock_signal = {
    "signal": {
        "signal": "BUY_CE", "strike": 24000, "confidence": 72, "source": "rule-engine",
        "entry": 55, "stoploss": 45, "target1": 75, "target2": 90,
        "risk_preview_checks": [],
        "unified_probability": {
            "unified_probability_pct": 69, "unavailable": False,
            "basis": "combined from 4/5 available inputs",
            "components": {"option_chain": 78, "decision_engine": 74,
                          "institutional": 59, "technical": 42},
            "weights": {"option_chain": 0.33, "decision_engine": 0.4,
                       "institutional": 0.16, "technical": 0.11},
        },
        "ai_probability": None,
    },
    "analysis_snapshot": {"spot": 24000, "bias": "bullish", "risk_meter": 40},
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 900})
        page.route("**/api/signal/**",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_signal)))
        page.goto("http://127.0.0.1:8942/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        page.evaluate("getSignal(true)")
        page.wait_for_timeout(1500)

        sigcard = page.query_selector("#sigcard")
        text = sigcard.inner_text()
        check("Option chain factor row is visible in the rendered "
             "(not hover-only) text",
              "Option chain" in text, text[:300])
        check("Decision engine factor row is visible",
              "Decision engine" in text)
        check("Institutional factor row is visible",
              "Institutional" in text)
        check("Technical factor row is visible",
              "Technical" in text)
        check("a signed positive contribution is shown for a "
             "supporting factor (option chain, score 78 > 50)",
              "+9.2" in text or "+9" in text, text[:300])
        check("a signed negative contribution is shown for an "
             "opposing factor (technical, score 42 < 50)",
              "-0.9" in text or "\u22120.9" in text, text[:400])

        # Confirm the breakdown is in the DOM (visible), not gated
        # behind a hover — query for actual rendered bar elements
        bars = page.query_selector_all(
            "#sigcard div[style*='background:var(--up'], "
            "#sigcard div[style*='background:var(--dn']")
        check("4 fill bar elements actually rendered in the DOM "
             "(genuinely visible, not requiring a hover to appear)",
              len(bars) == 4, str(len(bars)))

        browser.close()
finally:
    server.should_exit = True
    thread.join(timeout=5)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
