"""v58.21+ — tests for the P&L page's Open Options/Spreads/Futures
sections always being visible, per direct report ("Open thread and
Open future panels are missing").

Root cause: each category's section only appeared in the HTML at all
when it had something open in it — an all-empty category vanished
entirely rather than showing "nothing open here right now", reading
exactly like "this panel doesn't exist" rather than "it's just empty".
The underlying data/rendering was confirmed already correct with real
data — this was purely about empty states being indistinguishable from
a missing feature.

Fix: always render three clearly labeled sections (Open options / Open
spreads / Open futures), each showing its own table when populated or
an explicit "No open X" line when not.

Run:  python3 test_pnl_open_positions_always_visible.py
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

print("1) source-level: all three category headings are unconditional "
     "now, not gated behind an if(length) check")
check('"Open options" heading is added before any length check',
      "let opHtml='<div class=\"reasons\" style=\"margin-bottom:4px\">Open options</div>';"
      in h)
check("an explicit else branch renders \"No open options.\" when empty",
      "opHtml+='<div class=\"reasons\">No open options.</div>';" in h)
check("an explicit else branch renders \"No open spreads.\" when empty",
      "opHtml+='<div class=\"reasons\">No open spreads.</div>';" in h)
check("an explicit else branch renders \"No open futures.\" when empty",
      "opHtml+='<div class=\"reasons\">No open futures.</div>';" in h)
check("the final assignment no longer falls back to a generic "
     "'No open position' catch-all (opHtml is never empty now, it "
     "always has at least the three headings)",
      "op.innerHTML=opHtml;" in h)

print("\n2) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/pnl_open_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/pnl_open_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n3) REAL BROWSER VERIFICATION — empty state: all three sections "
     "visible with explicit 'no open X' messages, not vanished")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8940, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

mock_empty = {
    "open": None, "positions": {}, "open_spreads": {}, "open_futures": {},
    "closed": [], "daily": [],
    "guardrails": {"daily_pnl": 0, "daily_loss_limit": 5000, "daily_profit_target": 0,
                  "consecutive_losses": 0, "consecutive_losses_limit": 2,
                  "portfolio_drawdown": 0, "portfolio_max_drawdown": 15000},
    "stats": {"count": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0,
             "unrealized_pnl": 0, "total_pnl": 0, "total_fees": 0},
}
mock_populated = {
    "open": None,
    "positions": {"NIFTY": {"symbol": "NIFTY", "strike": 24000, "leg": "CE", "qty": 75,
                            "entry": 50, "target1": 70, "target2": 90, "stoploss": 30,
                            "ltp": 55, "capital_used": 3750, "pnl": 375,
                            "opened": "10:00:00", "paper": True}},
    "open_spreads": {"spread1": {"strategy": "bear_call_spread", "symbol": "SENSEX",
                                "legs": [{"action": "SELL", "strike": 77000, "leg": "CE"},
                                        {"action": "BUY", "strike": 77200, "leg": "CE"}],
                                "credit": 74, "margin_used": 560000, "pnl": -2415,
                                "opened": "10:03:30", "paper": True}},
    "open_futures": {"BANKNIFTY": {"symbol": "BANKNIFTY", "side": "LONG", "lots": 1,
                                  "entry": 57220, "ltp": 57214.4, "sl": 56991.1,
                                  "target": 57677.8, "margin": 110000, "pnl": -168,
                                  "opened": "10:43:14", "paper": True}},
    "closed": [], "daily": [],
    "guardrails": mock_empty["guardrails"],
    "stats": {"count": 0, "wins": 0, "losses": 0, "win_rate": 0, "realized_pnl": 0,
             "unrealized_pnl": -2208, "total_pnl": -2208, "total_fees": 0},
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # Empty state
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.route("**/api/trades",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_empty)))
        page.goto("http://127.0.0.1:8940/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        page.evaluate('showView("pnl")')
        page.wait_for_timeout(1000)
        op = page.query_selector("#pnlOpen")
        empty_text = op.inner_text()
        check("empty state shows 'Open options' heading",
              "Open options" in empty_text)
        check("empty state shows 'Open spreads' heading",
              "Open spreads" in empty_text)
        check("empty state shows 'Open futures' heading",
              "Open futures" in empty_text)
        check("empty state explicitly says no open options",
              "No open options." in empty_text)
        check("empty state explicitly says no open spreads",
              "No open spreads." in empty_text)
        check("empty state explicitly says no open futures",
              "No open futures." in empty_text)
        page.close()

        # Populated state
        page2 = browser.new_page(viewport={"width": 1400, "height": 1000})
        page2.route("**/api/trades",
                   lambda route: route.fulfill(status=200, content_type="application/json",
                                              body=json.dumps(mock_populated)))
        page2.goto("http://127.0.0.1:8940/", wait_until="domcontentloaded", timeout=15000)
        page2.wait_for_timeout(1500)
        page2.evaluate('showView("pnl")')
        page2.wait_for_timeout(1000)
        op2 = page2.query_selector("#pnlOpen")
        pop_text = op2.inner_text()
        tables = page2.query_selector_all("#pnlOpen table")
        check("populated state renders exactly 3 tables (options, "
             "spreads, futures)",
              len(tables) == 3, str(len(tables)))
        check("populated state shows the real NIFTY option data",
              "NIFTY" in pop_text and "24,000" in pop_text)
        check("populated state shows the real SENSEX spread data",
              "bear_call_spread" in pop_text and "SENSEX" in pop_text)
        check("populated state shows the real BANKNIFTY futures data",
              "BANKNIFTY" in pop_text and "LONG" in pop_text)
        page2.close()

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
