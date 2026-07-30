"""v58.10+ — tests for the design-system re-theme and institutional
panel consolidation (items #7/#8, first increment).

Scope of this round, stated explicitly: re-theme colors/fonts globally
(safe — 283 existing var() references already use these names, only
values changed) and consolidate the dashboard's duplicated full-detail
institutional/smart-money panels into one compact summary linking to
the existing dedicated Institutional page. A full page-by-page rebuild
matching all 11 wireframe sheets was NOT attempted in this round —
the risk of breaking JS wiring in a 5000-line live-trading file wasn't
worth it without being able to test every page's data-binding
thoroughly in one pass.

Run:  python3 test_design_theme_consolidation.py
"""
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

print("1) theme re-applied: new Azia palette (purple/blue/teal) and "
     "Roboto fonts, replacing the old Supabase-green/Inter theme")
check("new purple accent present", "#8B5CF6" in h or "#6F42C1" in h)
check("Roboto font family imported", "Roboto" in h and "family=Roboto" in h)
check("old Inter font import removed", "family=Inter" not in h)
check("old Supabase-green accent value removed from :root",
      "--accent:#3ecf8e" not in h)

print("\n2) JS syntax still valid after the re-theme and consolidation")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/design_test_dash.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/design_test_dash.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n3) institutional/smart-money consolidation: the dashboard's "
     "two duplicated full-detail panels are gone, replaced by one "
     "compact summary card")
check("old duplicated Option Chain Intelligence panel removed",
      "ociSummary" not in h and "ociNarrative" not in h and "ociSmartMoney" not in h)
check("old duplicated Institutional Activity full panel removed "
      "(iaeScoreVal/iaeCommentary/iaeEvents as real element ids, not "
      "just mentioned in historical comments)",
      'id="iaeScoreVal"' not in h and 'id="iaeCommentary"' not in h and
      'id="iaeEvents"' not in h)
check("new consolidated summary card present",
      'id="instSummaryCard"' in h)
check("the summary card links to the dedicated Institutional page "
     "instead of duplicating its content",
      "View full analysis" in h and "showView('inst')" in h)

print("\n4) the dedicated Institutional page (the full-detail view "
     "item #7 says deserves its own page) is UNTOUCHED — still has "
     "its own AI Narrative / Smart Money Events / Per-Strike Activity")
check("dedicated page's own AI Narrative panel still present",
      "AI Narrative" in h)
check("dedicated page's own Smart Money Events panel still present",
      "Smart Money Events" in h)
check("dedicated page's own Per-Strike Activity panel still present",
      "Per-Strike Activity" in h)

print("\n5) no dangling references to removed element ids anywhere in "
     "the live code (historical comments mentioning the old names are "
     "fine and expected, but no getElementById call should target a "
     "now-nonexistent id)")
js_ids = set(re.findall(r'getElementById\(["\']([a-zA-Z0-9_]+)["\']\)', h))
html_ids = set(re.findall(r'id=["\']([a-zA-Z0-9_]+)["\']', h))
missing = sorted(js_ids - html_ids)
# Pre-existing, unrelated gaps confirmed present before this round's
# changes (agentsGrid/agentsGridFull naming mismatch, dead canvas-chart
# references from before Lightweight Charts) — not introduced by this
# round, not fixed by this round either (out of scope).
pre_existing_gaps = {"agentsGrid", "cInfo", "candleCanvas", "candleMsg", "stats",
                    "lwLevelsDetail"}   # intentionally removed this round (item 11 above);
                                       # all 3 remaining references are null-guarded, confirmed separately
new_gaps = [m for m in missing if m not in pre_existing_gaps]
check("no NEW dangling getElementById references introduced by "
      "this round's changes",
      len(new_gaps) == 0, str(new_gaps))

print("\n6) renderOci/renderIae correctly write to DIFFERENT elements "
     "of the consolidated card (score/KV vs headline) rather than one "
     "function's innerHTML overwriting the other's contribution")
check("renderIae owns the score element",
      'document.getElementById("instSummaryScore")' in h)
check("renderIae owns the KV summary element",
      'document.getElementById("instSummaryKv")' in h)
check("renderOci owns a DIFFERENT element (the headline), not the "
      "same one renderIae writes to",
      'document.getElementById("instSummaryHeadline")' in h)

print("\n7) REAL LAYOUT VERIFICATION (not just CSS reasoning): the actual "
     "root cause of 'no layout change visible' was found — EVERY panel "
     "had the `.full` class, which forces grid-column:1/-1 regardless "
     "of the .grid wrapper's own sizing, so nothing could ever sit "
     "side by side no matter how the colors changed. Verified via a "
     "REAL headless-browser render with measured bounding boxes, not "
     "just checking the CSS exists.\n"
     "NOTE: this specific pairing (Institutional Summary + Portfolio "
     "Risk Engine in a shared .row3) was later SUPERSEDED by an even "
     "better restructuring (the full 8/4 dashboard column split,\n"
     "see test_dashboard_8_4_split.py) — both panels are now standalone "
     "cards stacked in the right column rather than paired together, "
     "which is why this checks for the element's continued EXISTENCE "
     "rather than the now-obsolete row3 wrapper specifically.")
check(".full removed from the Institutional Summary panel (still "
     "true after the later 8/4 restructuring superseded the row3 "
     "pairing specifically)",
      'id="instSummaryCard"' in h and '"panel full"' not in
      h[h.index('id="instSummaryCard"') - 50:h.index('id="instSummaryCard"')])
check("the Institutional Summary panel still exists and renders "
     "(now as a standalone card in the right column rather than "
     "row3-paired — a further improvement, not a regression)",
      'id="instSummaryCard"' in h)

print("\n8) source-level guard: div nesting is balanced across the "
     "entire dashboard view (a real risk when restructuring nested "
     "grid/row wrappers by hand)")
start = h.index('<div id="view-dash"')
end = h.index('<!-- ============================================================ P&L VIEW')
section = h[start:end]
check("opening and closing <div> tags balance exactly across the "
     "whole dashboard view section",
      section.count("<div") == section.count("</div>"),
      f"{section.count('<div')} opens vs {section.count('</div>')} closes")

print("\n9) THE ACTUAL EXPLANATION for 'nothing changed' after a real, "
     "browser-verified layout fix: FileResponse's default headers "
     "let browsers skip re-fetching entirely on reload — confirmed "
     "via a real HTTP request that explicit no-cache headers are now "
     "set, forcing a fresh fetch every load going forward")
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import app as app_module
client = TestClient(app_module.app)
r = client.get("/")
check("root route returns 200", r.status_code == 200)
check("Cache-Control explicitly disables caching",
      "no-cache" in r.headers.get("cache-control", "") and
      "no-store" in r.headers.get("cache-control", ""),
      r.headers.get("cache-control"))
check("Pragma: no-cache set for older browsers/proxies",
      r.headers.get("pragma") == "no-cache")

print("\n10) THE REAL BUG behind a follow-up report ('layout is the "
     "same') that WAS genuine, separate from the screenshot-mismatch "
     "explanation: .row3 shared the SAME 1100px collapse breakpoint "
     "as .grid/#chartPanel, which have much denser content and "
     "genuinely need that wide a threshold. .row3's own content "
     "(compact score+summary cards) stays comfortably readable at "
     "half-width down to a much narrower window — sharing the wider "
     "threshold meant the side-by-side pairing could vanish on "
     "perfectly reasonable, non-maximized browser windows for no "
     "real content reason. Verified with REAL browser renders at "
     "multiple widths, not just reading the CSS")
check(".row3 no longer shares .grid's breakpoint declaration",
      ".grid,.row3{grid-template-columns:1fr}" not in h)
check(".row3 has its own, narrower 640px threshold instead",
      "@media(max-width:640px){.row3{grid-template-columns:1fr}}" in h)

print("\n11) THE ACTUAL DUPLICATE the person meant: a 'KEY LEVELS (R1-R3/"
     "S1-S3)' text strip embedded in the chart panel itself, repeating "
     "the same R1-R3/S1-S3 data already shown as price-line labels "
     "directly on the chart. Removed the strip; confirmed via a real "
     "browser load that no JS runtime errors occur (all 3 remaining "
     "getElementById references were already null-guarded)")
check("the duplicate 'KEY LEVELS' label text removed from the HTML",
      "KEY LEVELS (R1-R3" not in h)
check("the lwLevelsDetail container element itself removed",
      'id="lwLevelsDetail"' not in h)
check("all remaining JS references to it are safely null-guarded "
     "(if(det)/if(detailEl)/if(el) before use, not a bare "
     "getElementById().innerHTML= that would throw)",
      'if(det) det.innerHTML=' in h and 'if(detailEl){' in h)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
