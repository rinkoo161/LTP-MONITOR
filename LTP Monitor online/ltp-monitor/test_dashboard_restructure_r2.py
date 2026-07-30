"""v58.14+ — tests for the second design-restructuring round: removing
the duplicate Key Levels panel (confirmed redundant with the chart's
own R1-R3/S1-S3 level-line overlay, per direct user report) and
consolidating the LTP Monitor from 4 separate per-symbol cards into
one compact table, per the wireframe's actual proposed structure.

Run:  python3 test_dashboard_restructure_r2.py
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


h = open("static/dashboard.html").read()

print("1) Key Levels duplicate panel completely removed — confirmed "
     "genuinely redundant with the chart's own R1-R3/S1-S3 level-line "
     "overlay (both draw from the same underlying levels data)")
check("the Key Levels panel heading is gone", "Key Levels</span>" not in h and
      ">&#127961; Key Levels" not in h)
check("the ladder element (text list of R/S levels) is gone",
      'id="ladder"' not in h)
check("the OI histogram canvas is gone", 'id="oiChart"' not in h)
check("the now-dead renderLadder function was actually removed, not "
     "just made unreachable",
      "function renderLadder" not in h)
check("no dangling reference to the removed ladderRefresh element",
      "ladderRefresh" not in h)

print("\n2) JS syntax still valid after removing the panel and its "
     "supporting code")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/r2_test_dash.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/r2_test_dash.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n3) div nesting stayed balanced across the dashboard view "
     "section after removing the row3 wrapper the panel used to share "
     "with Market Sentiment")
start = h.index('<div id="view-dash"')
end = h.index('<!-- ============================================================ P&L VIEW')
section = h[start:end]
check("opening and closing <div> tags balance exactly",
      section.count("<div") == section.count("</div>"),
      f"{section.count('<div')} vs {section.count('</div>')}")

print("\n4) backend: regime added to /api/ltp-monitor's response "
     "(cheap addition — regime:{symbol} was already computed and "
     "cached elsewhere, just not exposed here before)")
import app as app_module
from fastapi.testclient import TestClient
client = TestClient(app_module.app)
app_module.pilot.bus.set("symbols", ["NIFTY"])
app_module.pilot.bus.set("regime:NIFTY", {"regime": "trending-up"})
r2 = client.get("/api/ltp-monitor")
d2 = r2.json()
check("regime field present and correct in a real HTTP response",
      d2["symbols"]["NIFTY"].get("regime") == "trending-up", str(d2["symbols"]["NIFTY"]))

print("\n5) NOTE: this section used to REAL-BROWSER-VERIFY the LTP "
     "Monitor compact table (#ltpMonitorGrid, click-to-expand detail "
     "rows) directly on the Dashboard. That panel was moved off the "
     "Dashboard entirely to its own dedicated page on 2026-07-28, per "
     "direct request, and rebuilt there with a different (detailed "
     "card) layout — this section's own assertions no longer apply to "
     "anything that exists. Superseded by test_futures_trading_page.py, "
     "which verifies the new page's rendering, execution controls, and "
     "confirms the Dashboard has zero trace of the old panel. Kept as "
     "a note rather than silently deleted, so this file's history "
     "stays legible.")

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
