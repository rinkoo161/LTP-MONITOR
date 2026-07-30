"""v58.25+ — tests for moving the Dashboard's "LTP Monitor — Spot vs
Futures" panel to its own dedicated page, per direct request: "LTP
Monitor — Spot vs Futures panel can be moved to a separate page with
detailed view (same as previous) and to execute the Futures trades."

Restores the ORIGINAL detailed per-symbol card layout (the 2026-07-27
consolidation had compressed 4 cards into one compact table
specifically to save Dashboard space) — that constraint doesn't apply
on a dedicated page. Futures Buy/Sell/Exit execution
(renderFuturePosition/futEnter/futExit) is unchanged and reused
directly, just made visible on each card instead of hidden behind a
click-to-expand row. Same /api/ltp-monitor endpoint, no new backend.

Run:  python3 test_futures_trading_page.py
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

print("1) source-level: the panel is genuinely gone from the "
     "Dashboard, not just supplemented")
dash_start = h.index('<div id="view-dash"')
dash_end = h.index('<!-- ============================================================ FUTURES TRADING VIEW')
dash_section = h[dash_start:dash_end]
check("ltpMonitorGrid id no longer present in the Dashboard view",
      'id="ltpMonitorGrid"' not in dash_section)
check("the old LTP Monitor panel's specific subtitle text no longer "
     "present in the Dashboard view (checking the actual removed "
     "element's unique text, not a loose substring that could match "
     "an explanatory code comment)",
      "institutional participation check" not in dash_section)

print("\n2) source-level: new Futures Trading view and nav rail entry "
     "exist")
check("new view-futures div exists",
      '<div id="view-futures" class="view" style="display:none">' in h)
check("new nav rail button exists, wired to showView('futures')",
      'id="rail-futures" onclick="showView(\'futures\')"' in h)
check("futures registered in showView's view-toggle array",
      '["dash","futures","pnl","strat","inst","bt","journal","agents","macro","quality"]'
      in h)
check("showView triggers loadFuturesPage on navigating to futures",
      'if(v==="futures")loadFuturesPage();' in h)

print("\n3) source-level: old function/element names fully retired, "
     "no dangling references")
for stale in ["loadLtpMonitor", "ltpMonitorGrid", "toggleLtpDetail", "ltpDetail_"]:
    check(f'no remaining reference to "{stale}"', stale not in h)
check("new loadFuturesPage function exists",
      "async function loadFuturesPage(){" in h)
check("futEnter refreshes the new page, not the removed one",
      "loadFuturesPage();\n}\nasync function futExit(sym){" in h)

print("\n4) source-level: the unconditional initial-load call was "
     "removed (futures isn't the default view) and the 5s auto-"
     "refresh now targets the futures view")
check("no bare unconditional loadLtpMonitor()/loadFuturesPage() call "
     "left at page-load time",
      "\nloadFuturesPage();\npollEngine();" not in h and
      "\nloadLtpMonitor();\npollEngine();" not in h)
check("5s auto-refresh now checks currentView===\"futures\"",
      'setInterval(function(){if(currentView==="futures")loadFuturesPage();},5000);'
      in h)

print("\n5) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/futures_page_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/futures_page_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n6) div nesting balanced across the combined view-dash + "
     "view-futures region")
end2 = h.index("<!-- ============================================================ P&L VIEW")
combined = h[dash_start:end2]
check("balanced div nesting",
      combined.count("<div") == combined.count("</div>"),
      f"{combined.count('<div')} vs {combined.count('</div>')}")

print("\n7) REAL BROWSER VERIFICATION: full end-to-end flow — "
     "dashboard has no trace of the panel, nav click loads the new "
     "page, cards render with position-aware execution controls")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8946, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

mock_ltp = {
    "symbols": {
        "NIFTY": {"spot": {"ltp": 23985.35, "chg_pct": -0.08, "open": 24000, "high": 24050,
                          "low": 23950, "vwap": 23990, "prev_close": 24003.65},
                 "futures": {"ltp": 24010.5, "open": 24020, "high": 24060, "low": 23980, "vwap": 24000},
                 "regime": "trending-down", "future_position": None},
        "BANKNIFTY": {"spot": {"ltp": 56755.6, "chg_pct": -0.63, "open": 57000, "high": 57100,
                              "low": 56700, "vwap": 56900, "prev_close": 57115.95},
                     "futures": {"ltp": 56800, "open": 57050, "high": 57150, "low": 56750, "vwap": 56950},
                     "regime": "trending-down",
                     "future_position": {"side": "SHORT", "lots": 1, "entry": 57055, "pnl": -100,
                                        "sl": 57283.2, "target": 56598.6}},
    },
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.route("**/api/ltp-monitor",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_ltp)))
        page.goto("http://127.0.0.1:8946/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        dash_ltp = page.query_selector("#view-dash #ltpMonitorGrid")
        check("dashboard genuinely has no ltpMonitorGrid element in "
             "the live DOM",
              dash_ltp is None)
        futures_nav = page.query_selector("#rail-futures")
        check("Futures nav rail button is present and clickable",
              futures_nav is not None)

        page.click("#rail-futures")
        page.wait_for_timeout(1500)

        cards = page.query_selector_all(".futures-card")
        check("2 futures cards rendered (one per mocked symbol)",
              len(cards) == 2, str(len(cards)))

        if len(cards) == 2:
            texts = [c.inner_text() for c in cards]
            nifty_card = next((t for t in texts if "NIFTY" in t and "BANKNIFTY" not in t), "")
            bn_card = next((t for t in texts if "BANKNIFTY" in t), "")

            check("NIFTY card (no position) shows Buy FUT / Sell FUT "
                 "execution buttons",
                  "Buy FUT" in nifty_card and "Sell FUT" in nifty_card,
                  nifty_card[:200])
            check("NIFTY card shows the full detailed spot/futures "
                 "breakdown (O/H/L, VWAP, Prev Close) — the 'same as "
                 "previous' detail, not the compact table",
                  "23,950" in nifty_card and "23,990" in nifty_card and
                  "24,003.65" in nifty_card, nifty_card[:300])
            check("BANKNIFTY card (open position) shows position "
                 "details and an Exit button instead of entry buttons",
                  "SHORT" in bn_card and "Exit" in bn_card and
                  "Buy FUT" not in bn_card, bn_card[:300])
            check("BANKNIFTY card shows the SL/Target from the open "
                 "position",
                  "57,283.2" in bn_card and "56,598.6" in bn_card)

        overflow = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth")
        check("no horizontal overflow introduced", overflow is False)

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
