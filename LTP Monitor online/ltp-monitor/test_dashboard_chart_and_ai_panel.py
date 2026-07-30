"""v58.26+ — tests for the dashboard chart height increase and moving
AI Deep Analysis to its own collapsible full-width row, per direct
request: "increase the chart length to 120%" and "move the AI Deep
Analysis in the last row (full width) under market sentiment... include
an option to collapse and expand it."

Run:  python3 test_dashboard_chart_and_ai_panel.py
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

print("1) source-level: chart height increased to 120% (340px -> "
     "408px) consistently everywhere it's set — container, chart "
     "library option, and the H+/H- tracking variable")
check("chart container div height is 408px",
      '<div id="lwChartContainer" style="height:408px"></div>' in h)
check("createChart's own height option matches",
      "lwChart=LightweightCharts.createChart(container,{\n    height:408,"
      in h)
check("the H+/H- tracking variable starts at the new default (so "
     "manual height adjustments increment/decrement from 408, not "
     "jump back to the old 340)",
      "let lwChartHeight=408;" in h)
check("the Backtest page's separate chart (btChartContainer) was left "
     "unchanged — the request was specifically about the Dashboard's "
     "chart",
      '<div id="btChartContainer" style="height:340px"></div>' in h)

print("\n2) source-level: AI Deep Analysis moved out of the dc4 column "
     "into its own full-width row")
dash_start = h.index('<div id="view-dash"')
dash_end = h.index("<!-- ============================================================ FUTURES TRADING VIEW")
dash_section = h[dash_start:dash_end]
check("AI Deep Analysis panel uses the 'panel full' class (full-width "
     "row), not nested inside a dc4 column",
      '<div class="panel full" style="margin-bottom:8px">\n    <h2>&#128300; AI Deep Analysis'
      in dash_section)
row1x3_idx = dash_section.index('class="row1x3"')
ai_idx = dash_section.index("AI Deep Analysis")
check("AI Deep Analysis appears AFTER the row1x3 block (Market "
     "Sentiment/Portfolio Risk/Technical Analysis), matching 'under "
     "market sentiment' and 'last row'",
      ai_idx > row1x3_idx)

print("\n3) source-level: collapse/expand mechanism exists and starts "
     "collapsed")
check("aiOut starts with display:none (collapsed by default, since "
     "the panel only ever populates on an explicit refresh and starts "
     "empty otherwise)",
      '<div id="aiOut" style="display:none">' in h)
check("a dedicated collapse/expand button exists, separate from the "
     "refresh button",
      'id="aiCollapseBtn"' in h and 'id="aiBtn"' in h)
check("toggleAiDeepAnalysis function is defined",
      "function toggleAiDeepAnalysis(){" in h)
check("runAI() auto-expands the panel when clicked (a refresh request "
     "implies wanting to see the result, not a silently-refreshed "
     "hidden panel)",
      'if(out.style.display==="none") toggleAiDeepAnalysis();' in h)

print("\n4) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/dash_chart_ai_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/dash_chart_ai_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n5) div nesting balanced across the whole dashboard view")
check("balanced div nesting",
      dash_section.count("<div") == dash_section.count("</div>"),
      f"{dash_section.count('<div')} vs {dash_section.count('</div>')}")

print("\n6) REAL BROWSER VERIFICATION: actual rendered chart height, "
     "panel position, and a real click-driven collapse/expand cycle")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8947, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1200})
        page.goto("http://127.0.0.1:8947/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)

        chart_container = page.query_selector("#lwChartContainer")
        box = chart_container.bounding_box()
        check("chart container renders at ~408px height",
              405 <= box["height"] <= 411, str(box["height"]))

        ai_out = page.query_selector("#aiOut")
        initial_display = page.evaluate('document.getElementById("aiOut").style.display')
        check("AI Deep Analysis starts collapsed (display:none) on "
             "page load",
              initial_display == "none", initial_display)

        collapse_btn = page.query_selector("#aiCollapseBtn")
        check("collapse/expand button shows 'Expand' initially",
              "Expand" in collapse_btn.inner_text(), collapse_btn.inner_text())

        # Confirm the AI panel sits below the row1x3 panels (Market Sentiment etc.)
        sentiment_panel = page.query_selector("#sentiment")
        ai_panel_el = page.evaluate(
            'document.getElementById("aiOut").closest(".panel")')
        sentiment_box = sentiment_panel.bounding_box()
        ai_box = page.evaluate_handle(
            'document.getElementById("aiOut").closest(".panel")').as_element().bounding_box()
        check("AI Deep Analysis panel sits below (higher y-coordinate "
             "than) the Market Sentiment panel",
              ai_box["y"] > sentiment_box["y"],
              f"ai={ai_box['y']}, sentiment={sentiment_box['y']}")
        check("AI Deep Analysis panel spans full dashboard width, not "
             "squeezed into a narrow column",
              ai_box["width"] > sentiment_box["width"] * 2,
              f"ai_width={ai_box['width']}, sentiment_width={sentiment_box['width']}")

        # Click to expand
        collapse_btn.click()
        page.wait_for_timeout(300)
        display_after_click = page.evaluate('document.getElementById("aiOut").style.display')
        btn_text_after = page.query_selector("#aiCollapseBtn").inner_text()
        check("clicking the toggle expands the panel (display:block)",
              display_after_click == "block", display_after_click)
        check("button text updates to 'Collapse' after expanding",
              "Collapse" in btn_text_after, btn_text_after)

        # Click again to collapse
        collapse_btn.click()
        page.wait_for_timeout(300)
        display_after_second_click = page.evaluate(
            'document.getElementById("aiOut").style.display')
        check("clicking again collapses it back (display:none)",
              display_after_second_click == "none", display_after_second_click)

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
