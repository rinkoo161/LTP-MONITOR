"""v58.25+ — tests for the Macro/News page merge, per direct request:
"Macro/News event log Panel and News Tracker panel has similar
information; both can be merged and ranked accordingly. Similarly,
Digest and Global Market snapshots have similar information; we can
keep one only."

Investigated before merging: confirmed via both backend handlers that
Macro Event Log (macro_events bus key, fed by NewsMacroAgent's
checkpoint pipeline) and News Tracker (news_engine.read_tracked_
events(), a separate RSS+NewsAPI store) are genuinely DIFFERENT data
sources, not the same data duplicated — so this is a real merge of two
schemas into one ranked table, not a duplicate-removal. Digest and
Global Markets Snapshot, by contrast, WERE showing the same underlying
market-data numbers from two different endpoints (/api/macro/digest
vs /api/macro) — confirmed identical, so Global Markets Snapshot's two
unique pieces (provider-key warning, checkpoint status line) were
absorbed into Digest and the duplicate panel removed.

Run:  python3 test_macro_news_merge.py
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

print("1) source-level: the old separate panels/tables are genuinely "
     "gone, not just supplemented")
check("old separate macroTable id is gone",
      'id="macroTable"' not in h)
check("old separate newsTrackerTable id is gone",
      'id="newsTrackerTable"' not in h)
check("old separate macroMarketData grid id is gone",
      'id="macroMarketData"' not in h)
check("old loadMacroEvents function definition is gone",
      "async function loadMacroEvents(){" not in h)
check("old loadNewsTracker function definition is gone",
      "async function loadNewsTracker(){" not in h)
check("new merged table id exists",
      'id="macroNewsTable"' in h)
check("new merged loading function exists",
      "async function loadMacroAndNews(){" in h)
check("the view-switch call was updated to the merged function",
      'if(v==="macro"){loadMacroAndNews();loadMacroDigest();loadNewsFeeds();}'
      in h)

print("\n2) source-level: ranking logic present — impact severity "
     "first, recency second")
check("sort comparator uses impact_rank first, ts second",
      "merged.sort(function(a,b){ return (b.impact_rank-a.impact_rank) || (b.ts-a.ts); });"
      in h)
check("macro events map Risk/Opportunity/other to rank 2/1/0",
      'const impactRank=e.impact==="Risk"?2:(e.impact==="Opportunity"?1:0);'
      in h)
check("news events map bearish/bullish/neutral to rank 2/1/0",
      'const impactRank=e.market_impact==="bearish"?2:(e.market_impact==="bullish"?1:0);'
      in h)

print("\n3) source-level: RSS Feed Sources now has a 5-row-equivalent "
     "scroll height")
check("feedsTable wrapped in tablewrap-scroll with a bounded max-height",
      'tablewrap tablewrap-scroll" style="max-height:200px"><table id="feedsTable"'
      in h)

print("\n4) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/macro_merge_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/macro_merge_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n5) div nesting balanced")
start = h.index('<div id="view-macro"')
end = h.index("<!-- ============================================================ TRADE QUALITY VIEW")
section = h[start:end]
check("view-macro div nesting balanced",
      section.count("<div") == section.count("</div>"),
      f"{section.count('<div')} vs {section.count('</div>')}")
check("no duplicate RSS Feed Sources panel remains",
      section.count("RSS Feed Sources") == 1, str(section.count("RSS Feed Sources")))

print("\n6) REAL BROWSER VERIFICATION: merged table actually ranks "
     "correctly with realistic data from BOTH sources, and the Digest "
     "panel correctly absorbs the checkpoint status")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8944, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

mock_macro = {
    "events": [
        {"date": "28 Jul 2026", "time": "07:10 PM", "type": "Global Macro News",
        "event": "China threatens measures against US sanctions", "impact": "Risk",
        "action": "monitor", "ts": 1785232200},
        {"date": "28 Jul 2026", "time": "07:11 PM", "type": "Weather/Monsoon",
        "event": "Heavy rain likely this week in UP", "impact": "Info",
        "action": "none", "ts": 1785232260},
    ],
    "market_data": {"DJI": {"value": 52514.89, "chg_pct": 0.62}},
    "agent": {"summary": "ran checkpoint(s): us_close, asia_markets", "last_run": "19:11:00"},
    "providers_configured": {"twelve_data": True, "alpha_vantage": False, "newsapi": True},
}
mock_news = {
    "events": [
        {"fetched_ts": 1785232100, "source": "Bloomberg", "description": "Nasdaq jumps as oil tumbles",
        "category": "tech", "market_impact": "bearish", "impact_windows": ["15m"],
        "action": "monitor", "valid": True},
        {"fetched_ts": 1785232000, "source": "Investing", "description": "WeTouch approves dividend",
        "category": "other", "market_impact": "neutral", "impact_windows": [],
        "action": "none", "valid": True},
    ],
    "categories": ["tech", "other", "weather"],
}

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.route("**/api/macro?days=5",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_macro)))
        page.route("**/api/news/tracker**",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps(mock_news)))
        page.route("**/api/macro/digest",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps({"indices": {}, "commodities_fx": {}})))
        page.route("**/api/news/feeds",
                  lambda route: route.fulfill(status=200, content_type="application/json",
                                             body=json.dumps({"feeds": []})))
        page.goto("http://127.0.0.1:8944/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        page.evaluate('showView("macro")')
        page.wait_for_timeout(1500)

        table = page.query_selector("#macroNewsTable")
        rows = table.query_selector_all("tbody tr")
        check("4 merged rows rendered (2 macro + 2 news)",
              len(rows) == 4, str(len(rows)))
        if len(rows) == 4:
            row_texts = [r.inner_text() for r in rows]
            check("row 1 is the highest-severity item (Risk, most recent "
                 "in that tier)",
                  "China threatens" in row_texts[0], row_texts[0][:80])
            check("row 2 is the other rank-2 item (bearish)",
                  "Nasdaq jumps" in row_texts[1], row_texts[1][:80])
            check("row 3 is a rank-0 item, more recent than row 4",
                  "Heavy rain" in row_texts[2], row_texts[2][:80])
            check("row 4 is the oldest, lowest-severity item",
                  "WeTouch" in row_texts[3], row_texts[3][:80])

        summary_text = page.query_selector("#macroAgentSummary").inner_text()
        check("Digest panel correctly shows the absorbed checkpoint "
             "status line",
              "us_close" in summary_text and "19:11:00" in summary_text,
              summary_text)

        dc8 = page.query_selector("#view-macro .dc8")
        dc4 = page.query_selector("#view-macro .dc4")
        check("dashgrid columns still present after the merge",
              dc8 is not None and dc4 is not None)

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
