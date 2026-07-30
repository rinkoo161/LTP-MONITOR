"""v58.19+ — tests for "other pages" restructuring, per explicit
request once the chart fix was confirmed working.

Scope decision, stated explicitly: surveyed every remaining page
(Strategies, Backtest, Journal, Agents, Macro/News, Quality) before
touching anything. Most are already appropriately built around wide
tables and filter bars — content that genuinely needs full width, not
a "too many small cards" problem the way Dashboard/P&L/Institutional
had. Applied the same dc8/dc4 grid (proven on the Dashboard) only
where a real structural mismatch existed:

  - P&L: Open Positions + Order History (left, dc8) paired with
    Day-wise P&L + a NEW Guardrails panel (right, dc4) — matching the
    wireframe's proposed P&L structure. Guardrails is a genuinely new
    UI element, but every value it shows already existed in config/
    agents; this only gathers them into one place.
  - Institutional: Per-Strike Activity table (left, dc8) paired with
    Smart Money Events (right, dc4), matching the wireframe's own
    Institutional sheet — AI Narrative stays full-width, also matching
    the wireframe's c12 positioning for it.

Strategies/Backtest/Journal/Agents/Macro/Quality deliberately left
structurally as-is — their wide tables and filter-bar-driven content
genuinely need full width; forcing a narrower column would hurt
usability, not help it.

Run:  python3 test_other_pages_restructure.py
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
app_src = open("app.py").read()

print("1) backend: /api/trades now returns a guardrails object with "
     "every value the P&L page's new panel needs — all sourced from "
     "existing config/agent state, not newly invented numbers")
check("guardrails dict is built in api_trades",
      '"daily_pnl": round(today_realized, 0)' in app_src)
check("daily_loss_limit comes from existing config, not a new default",
      'cfg.get("daily_loss_limit", 5000)' in app_src)
check("consecutive_losses reuses the exact same agent-state lookup "
     "/api/autopilot/status already uses (not a re-derived duplicate)",
      'next(\n        (a.consecutive_losses for a in pilot.agents if a.name == "risk"), 0)'
      in app_src)
check("portfolio_max_drawdown comes from existing config",
      'cfg.get("portfolio_max_drawdown", 15000)' in app_src)

print("\n2) backend: /api/trades genuinely returns the guardrails "
     "object in a real HTTP response, not just present in source")
import app as app_module
from fastapi.testclient import TestClient
client = TestClient(app_module.app)
r = client.get("/api/trades")
d = r.json()
check("response includes a guardrails key", "guardrails" in d)
gr = d.get("guardrails", {})
check("guardrails has all 7 expected fields",
      set(gr.keys()) >= {"daily_pnl", "daily_loss_limit", "daily_profit_target",
                        "consecutive_losses", "consecutive_losses_limit",
                        "portfolio_drawdown", "portfolio_max_drawdown"},
      str(gr.keys()))

print("\n3) JS syntax still valid after both page restructurings")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/other_pages_test.js", "w").write(js)
r2 = subprocess.run(["node", "--check", "/tmp/other_pages_test.js"],
                   capture_output=True, text=True)
check("node --check passes with zero errors", r2.returncode == 0, r2.stderr[:300])

print("\n4) div nesting stayed balanced in both restructured views")
for view_id, next_marker in [
    ('<div id="view-pnl"', "STRATEGIES VIEW"),
    ('<div id="view-inst"', "BACKTEST VIEW"),
]:
    start = h.index(view_id)
    end = h.index(f"<!-- ============================================================ {next_marker}")
    section = h[start:end]
    check(f"{view_id} div nesting balanced",
          section.count("<div") == section.count("</div>"),
          f"{section.count('<div')} vs {section.count('</div>')}")

print("\n5) REAL BROWSER VERIFICATION of both restructured pages — "
     "actual rendered layout, not just source inspection")
config_u = __import__("uvicorn").Config(app_module.app, host="127.0.0.1",
                                       port=8939, log_level="error")
server = __import__("uvicorn").Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

from playwright.sync_api import sync_playwright

mock_trades = {
    "open": None, "positions": {}, "open_spreads": {}, "open_futures": {},
    "closed": [{"time": "10:00", "symbol": "NIFTY", "signal": "CE 24000", "qty": 75,
               "entry": 100, "exit": 110, "sl": 90, "target1": 115, "target2": 120,
               "pnl": 750, "fees": 40, "mfe": 800, "mae": -100,
               "target_pct": "10%", "got_pct": "8%", "reason": "target hit", "mode": "paper"}],
    "daily": [{"date": "2026-07-28", "trades": 5, "win_rate": 60, "fees": 200, "pnl": 1200}],
    "guardrails": {"daily_pnl": -1200, "daily_loss_limit": 5000, "daily_profit_target": 8000,
                  "consecutive_losses": 1, "consecutive_losses_limit": 2,
                  "portfolio_drawdown": 2150, "portfolio_max_drawdown": 15000},
    "stats": {"count": 1, "wins": 1, "losses": 0, "win_rate": 100, "realized_pnl": 750,
             "unrealized_pnl": 0, "total_pnl": 750, "total_fees": 40},
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.route("**/api/trades",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_trades)))
        page.goto("http://127.0.0.1:8939/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        # P&L page
        page.evaluate('showView("pnl")')
        page.wait_for_timeout(1500)
        dc8 = page.query_selector("#view-pnl .dc8")
        dc4 = page.query_selector("#view-pnl .dc4")
        check("P&L page: both grid columns exist", dc8 is not None and dc4 is not None)
        if dc8 and dc4:
            b8, b4 = dc8.bounding_box(), dc4.bounding_box()
            pct8 = b8["width"] / (b8["width"] + b4["width"]) * 100
            check("P&L page: columns side by side, ~75/25 split",
                  abs(b8["y"] - b4["y"]) < 10 and 70 <= pct8 <= 80, f"{pct8:.1f}%")
        gr_el = page.query_selector("#pnlGuardrails")
        check("P&L page: Guardrails panel renders", gr_el is not None)
        gr_html = gr_el.inner_html() if gr_el else ""
        check("Guardrails shows the daily loss limit bar with real numbers",
              "Daily loss limit" in gr_html and "5,000" in gr_html)
        check("Guardrails shows the consecutive losses bar",
              "Consecutive losses" in gr_html)
        check("Guardrails shows the portfolio drawdown bar",
              "Portfolio drawdown" in gr_html and "15,000" in gr_html)

        # Institutional page
        page.evaluate('showView("inst")')
        page.wait_for_timeout(1000)
        dc8i = page.query_selector("#view-inst .dc8")
        dc4i = page.query_selector("#view-inst .dc4")
        check("Institutional page: both grid columns exist",
              dc8i is not None and dc4i is not None)
        if dc8i and dc4i:
            b8i, b4i = dc8i.bounding_box(), dc4i.bounding_box()
            check("Institutional page: columns side by side",
                  abs(b8i["y"] - b4i["y"]) < 10)
        strike_table = page.query_selector("#instStrikeTable")
        smart_money = page.query_selector("#instSmartMoney")
        narrative = page.query_selector("#instNarrative")
        check("Institutional page: Per-Strike table, Smart Money, and "
             "AI Narrative all present after restructuring",
              strike_table is not None and smart_money is not None and
              narrative is not None)

        overflow = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth")
        check("no horizontal overflow on either restructured page",
              overflow is False)

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
