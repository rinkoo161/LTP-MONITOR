"""v58.25+ — tests for the Trade Quality page changes, per direct
request: "add trade performance by Strategy" and "reduce the chart
sizing - all 4 charts can be shown in a single row."

Investigated before building anything: traced /api/quality's backend
_setup_of() helper and found "By Setup" was ALREADY grouping trades by
strategy name in practice (PA signal's source field, spread's
strategy field, or a leg-based fallback for plain option buys) — it
was just labeled ambiguously as "Setup". Renamed to "By Strategy"
rather than building a duplicate table showing the same grouping twice
under two different names.

The 4-chart layout change merges the two existing grid2 (2-column)
rows into one grid4 (4-column) row. Since each scatterbox's SVG uses
a fixed viewBox (520x260) scaled by width:100%, the rendered labels
would have shrunk roughly in half at 4-per-row — bumped the SVG's
internal font-size values (9->11, 10->12) to compensate.

Run:  python3 test_quality_page_changes.py
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

print("1) source-level: By Setup renamed to By Strategy, table id "
     "kept stable (internal, not user-facing)")
check('the "By Setup" label is gone', "By Setup" not in h)
check('the "By Strategy" label is present', "By Strategy" in h)
check("the qBySetup table id is deliberately unchanged (internal "
     "id, renaming would be pure churn for no benefit)",
      'id="qBySetup"' in h)
check("the column header changed from Setup to Strategy",
      '<th style="text-align:left">Strategy</th>' in h)

print("\n2) source-level: grid4 CSS class defined with responsive "
     "breakpoints matching the grid2 pattern")
check("grid4 class defined with 4 columns",
      ".grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}"
      in h)
check("grid4 has a responsive breakpoint at 1100px (down to 2 cols)",
      "@media (max-width:1100px){.grid4{grid-template-columns:repeat(2,1fr)}}"
      in h)
check("grid4 has a responsive breakpoint at 900px (down to 1 col)",
      "@media (max-width:900px){.grid4{grid-template-columns:1fr}}" in h)

print("\n3) source-level: all 4 scatter charts now share one grid4 "
     "container, not two separate grid2 rows")
check("only one grid4 container for the scatter charts (not split "
     "across two rows)",
      h.count('<div class="grid4">') == 1)
check("all four scatterbox ids present in that single container",
      all(f'id="{sid}"' in h for sid in
         ["scMfePnl", "scMaePnl", "scMfeVol", "scMaeVol"]))

print("\n4) source-level: SVG label font sizes bumped for readability "
     "at the smaller rendered width")
check("axis min/max labels bumped to font-size 11",
      'font-size="11" fill="var(--dim)">\'+fmt(Math.round(xMin))' in h)
check("axis titles bumped to font-size 12",
      'font-size="12" fill="var(--dim)" text-anchor="middle">\'+xLabel'
      in h)

print("\n5) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/quality_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/quality_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n6) REAL BROWSER VERIFICATION: all 4 charts genuinely render "
     "in a single row with equal widths, and the By Strategy rename "
     "shows up in the rendered page, not just the source")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8945, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

mock_quality = {
    "has_data": True, "n_trades": 10, "win_rate": 60, "expectancy": 150,
    "profit_factor": 1.8,
    "by_hour": [{"key": 10, "trades": 3, "win_rate": 66.7, "net_pnl": 500, "avg_pnl": 166.7}],
    "by_setup": [{"key": "bear_call_spread", "trades": 5, "win_rate": 60,
                 "net_pnl": 800, "avg_pnl": 160}],
    "by_symbol": [{"key": "NIFTY", "trades": 4, "win_rate": 50, "net_pnl": 300, "avg_pnl": 75}],
    "exit_efficiency": 65,
    "scatter_points": [{"mfe": 500, "mae": -100, "pnl": 300, "volume": 75}],
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.route("**/api/quality**",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_quality)))
        page.goto("http://127.0.0.1:8945/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        page.evaluate('showView("quality")')
        page.wait_for_timeout(1500)

        panel_header = page.evaluate(
            'document.querySelector("#qBySetup").closest(".panel").querySelector("h2").innerText')
        check('rendered panel header reads "By Strategy"',
              "By Strategy" in panel_header, panel_header)

        scatter_boxes = page.query_selector_all(".grid4 .scatterbox")
        check("exactly 4 scatterboxes rendered inside grid4",
              len(scatter_boxes) == 4, str(len(scatter_boxes)))
        if len(scatter_boxes) == 4:
            boxes = [b.bounding_box() for b in scatter_boxes]
            ys = [b["y"] for b in boxes]
            check("all 4 charts sit on the same row (y within 5px)",
                  max(ys) - min(ys) < 5, str(ys))
            widths = [b["width"] for b in boxes]
            check("all 4 charts have equal width (evenly split across "
                 "the row)",
                  max(widths) - min(widths) < 2, str(widths))

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
