"""v58.18+ — tests for two changes:

1. Chart "zoom to last hour" fix, correcting an earlier misunderstanding
   (v58.17 wrongly defaulted the candle GRANULARITY to 60-minute bars;
   the actual request was to always show the last hour of price action
   regardless of which granularity — 1m/5m/15m — is selected).

2. Dashboard restructuring: Market Sentiment, Portfolio Risk Engine,
   and Technical Analysis moved into their own 1x3 row below the whole
   dashgrid (chart + option chain on the left, everything else on the
   right), rather than three more entries in an already-tall stack.

The zoom fix needed real debugging, not just a plausible-looking
change — Lightweight Charts silently ignores a `setVisibleRange`/
`setVisibleLogicalRange`/`barSpacing` request if fewer bars are asked
for than would fill the container at its default spacing, UNLESS the
call happens after the chart has had a moment to settle post-setData.
Verified against the actual charting library (installed locally via
npm, since the CDN it normally loads from isn't reachable in this
sandboxed environment) rather than assumed from source alone.

Run:  python3 test_chart_hour_zoom_and_1x3_row.py
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

print("1) source-level: the earlier misunderstanding (default candle "
     "granularity = 60m) is reverted — default is back to 1m, with the "
     "hour-zoom handled as a separate, always-applied behavior")
check('lwCurrentInterval defaults back to "1" (not "60")',
      'let lwCurrentInterval="1";' in h)
check("the 1m button carries the active class by default again",
      re.search(r'lwIntervalBtn active"\s+data-interval="1"', h) is not None)
check("the 1h button no longer carries the active class",
      re.search(r'lwIntervalBtn active"\s+data-interval="60"', h) is None)

print("\n2) source-level: zoomToLastHour exists, computes a bar count "
     "from actual candle timestamps (not a fixed guess), and the "
     "auto-trigger on load/switch calls it instead of fitLwChart — "
     "the manual Fit button is untouched")
check("zoomToLastHour function is defined", "function zoomToLastHour()" in h)
check("it computes bars actually within the last 3600 seconds from "
     "real candle timestamps",
      "times.filter(function(t){ return t>=cutoff; })" in h)
check("the auto-trigger on load/interval-switch calls zoomToLastHour, "
     "not fitLwChart",
      "zoomToLastHour();" in h and
      re.search(r"lwFitPending.*\n.*lwFitPending=false;\n\s*zoomToLastHour\(\);", h) is not None)
check('the manual "Fit" button\'s onclick is untouched (still calls '
     "fitLwChart directly)",
      'onclick="fitLwChart()"' in h)

print("\n3) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/r6_test_dash.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/r6_test_dash.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n4) div nesting stayed balanced after moving 3 panels into a "
     "new row and removing them from their old location")
start = h.index('<div id="view-dash"')
end = h.index('<!-- ============================================================ P&L VIEW')
section = h[start:end]
check("opening and closing <div> tags balance exactly",
      section.count("<div") == section.count("</div>"),
      f"{section.count('<div')} vs {section.count('</div>')}")

print("\n5) the new 1x3 row's CSS is registered, and the three panels "
     "(Market Sentiment, Portfolio Risk Engine, Technical Analysis) "
     "each appear exactly once in the file — moved, not duplicated")
check(".row1x3 class is defined with 3 equal columns",
      ".row1x3{display:grid;grid-template-columns:repeat(3,1fr)" in h)
check("Market Sentiment's content div appears exactly once",
      h.count('id="sentiment"') == 1)
check("Portfolio Risk Engine's score element appears exactly once",
      h.count('id="riskScoreVal"') == 1)
check("Technical Analysis's score element appears exactly once",
      h.count('id="taeScoreVal"') == 1)

print("\n6) REAL BROWSER + REAL CHARTING LIBRARY VERIFICATION — the "
     "actual bug (Lightweight Charts silently ignoring a narrow-range "
     "request made immediately after setData, before the chart has had "
     "a moment to settle) is fixed by the existing retry pattern "
     "(80ms/400ms), confirmed against the real library rather than a "
     "mock or source inspection alone")

# The CDN this dashboard loads lightweight-charts from isn't reachable
# in this sandboxed test environment — use a locally npm-installed
# copy instead (an allowed domain here), checking a few plausible
# locations before giving up and skipping this one sub-check.
#
# v59.89 — the version is SCRAPED from the dashboard's own script tag
# rather than written here. Two reasons, both learned on the v4->v5
# upgrade:
#
#   * A hard-coded version in a test is a second source of truth. Pinned
#     at 4.1.3 while the dashboard moved to 5.x, this test would have
#     kept passing — against the OLD library — while every chart in the
#     real browser threw, because v5 REMOVED addLineSeries et al. A test
#     that supplies its own version cannot detect a mismatch with the
#     thing it is testing.
#   * The install directory is now version-stamped. It used to be reused
#     across versions, so a cached 4.1.3 tree from an earlier run would
#     be found first and silently served in place of the version under
#     test — the same failure by a different route.
_m = re.search(r'lightweight-charts@([0-9]+\.[0-9]+\.[0-9]+)/', h)
if not _m:
    print("  FAIL  could not read the lightweight-charts version from the "
          "dashboard script tag — the browser check cannot be trusted")
    results.append(("dashboard pins a lightweight-charts version", False))
    LWC_VERSION = None
else:
    LWC_VERSION = _m.group(1)
    print(f"  ..    dashboard pins lightweight-charts@{LWC_VERSION}; the "
          f"browser check below runs against exactly that build")

if LWC_VERSION is None:
    lwc_file = None
else:
    _root = f"/tmp/lwc_test_install_{LWC_VERSION}"
    candidate_paths = [
        f"{_root}/node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js",
    ]
    lwc_file = next((p for p in candidate_paths if os.path.exists(p)), None)
    if lwc_file is None:
        os.makedirs(_root, exist_ok=True)
        subprocess.run(["npm", "install", f"lightweight-charts@{LWC_VERSION}"],
                      cwd=_root, capture_output=True, timeout=120)
        lwc_file = next((p for p in candidate_paths if os.path.exists(p)), None)

if lwc_file is None:
    print("  SKIP  could not install lightweight-charts locally (no npm "
         "access in this run) — skipping the real-library browser check; "
         "all source-level checks above still ran")
else:
    import app as app_module
    import auth as auth_module
    import uvicorn
    from playwright.sync_api import sync_playwright

    config = uvicorn.Config(app_module.app, host="127.0.0.1", port=8938, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(2)

    # v59.89 — this whole section had been dead since login was added:
    # GET / serves the SIGN-IN page to an unauthenticated browser, so the
    # dashboard's script never ran, `LightweightCharts` was undefined and
    # the block died on `lwChart is not defined`. It failed identically
    # on 4.1.3, so it was never a charting-library problem — the browser
    # check simply stopped testing the dashboard and nobody noticed,
    # because the failure looked like the sandbox's usual CDN trouble.
    #
    # Sessions live in memory (auth._sessions), so one can be minted
    # here directly. That deliberately avoids putting a password or a
    # TOTP secret in a test, and avoids adding a test-only bypass to the
    # auth path — the app is exercised exactly as it ships.
    _token = "test-session-" + "0" * 24
    auth_module._sessions[_token] = {
        "user": "test-browser", "role": "admin", "expires": time.time() + 3600}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_cookies([{"name": app_module.SESSION_COOKIE,
                                  "value": _token, "domain": "127.0.0.1",
                                  "path": "/"}])
            page = context.new_page()
            # v59.89 — capture uncaught exceptions. initLwChart() is
            # called from an event handler, so a throw inside it leaves
            # the page looking fine and the chart simply absent. Without
            # this the v4->v5 breakage would have been invisible here.
            page_errors = []
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            with open(lwc_file) as f:
                lwc_source = f.read()

            def handle_route(route):
                route.fulfill(status=200, content_type="application/javascript",
                             body=lwc_source)
            page.route("**/unpkg.com/lightweight-charts**", handle_route)
            page.goto("http://127.0.0.1:8938/", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            # Assert we are on the DASHBOARD before asserting anything
            # about it. Without this the sign-in page passes silently
            # through every DOM check that merely looks for absence.
            check("the browser is authenticated and on the dashboard, "
                 "not the sign-in page",
                  page.query_selector("#lwChartContainer") is not None,
                  page.title())

            has_lwc = page.evaluate('typeof LightweightCharts')
            check("the real charting library loaded (via local install, "
                 "not the unreachable CDN)", has_lwc == "object")

            # Simulate a full session's worth of 1-minute candles and the
            # exact production retry pattern (immediate + 80ms + 400ms)
            result = page.evaluate('''
            async () => {
              if (!lwChart) return {error: 'no chart'};
              const nowTs = Math.floor(Date.now()/1000);
              const candles = [];
              lwCandleByTime = {};
              for (let i = 0; i < 375; i++) {
                const t = nowTs - (375-i)*60;
                const c = {time: t, open: 100+i*0.01, high: 100.5+i*0.01,
                          low: 99.5+i*0.01, close: 100.2+i*0.01};
                candles.push(c);
                lwCandleByTime[t] = c;
              }
              lwSeries.setData(candles.map(c => ({time:c.time, open:c.open,
                high:c.high, low:c.low, close:c.close})));
              zoomToLastHour();
              await new Promise(r => setTimeout(r, 80));
              zoomToLastHour();
              await new Promise(r => setTimeout(r, 400));
              zoomToLastHour();
              await new Promise(r => setTimeout(r, 100));
              const range = lwChart.timeScale().getVisibleRange();
              const latest = candles[candles.length-1].time;
              const oldest = candles[0].time;
              // v59.89 — drive the marker path too. In v5 markers moved
              // off the series onto a primitive whose handle is returned
              // by the attach call; a stale or missing handle draws
              // NOTHING and throws nothing, so only reading the markers
              // back proves the layer is still wired.
              let markers_readback = -1;
              try {
                lwTradeAndFlagMarkers = [
                  {time: candles[10].time, position:'belowBar',
                   color:'#3fb950', shape:'arrowUp', text:'T'},
                  {time: candles[20].time, position:'aboveBar',
                   color:'#f85149', shape:'arrowDown', text:'X'}];
                lwZigzagMarkers = []; lwSignalMarkers = [];
                lwRedrawMarkers();
                markers_readback = lwMarkers.markers().length;
              } catch (e) { markers_readback = 'threw: ' + e.message; }
              return {
                span_minutes: (range.to-range.from)/60,
                oldest_excluded: oldest < range.from,
                latest_included: latest <= range.to && latest >= range.from,
                markers_readback: markers_readback,
                has_volume: !!lwVolumeSeries,
                overlay_count: Object.keys(lwOverlaySeries||{}).length,
                pane_count: Object.keys(lwPanes||{}).length
              };
            }
            ''')
            check("received a real result from the chart (not an error)",
                 "error" not in result, str(result))
            if "error" not in result:
                check("visible span is close to 60 minutes (55-70 range, "
                     "not the full ~375-minute session)",
                      55 <= result["span_minutes"] <= 70,
                      f"{result['span_minutes']:.1f} min")
                check("the oldest candle (full session start, ~6 hours "
                     "back) is correctly EXCLUDED from the visible range",
                      result["oldest_excluded"])
                check("the newest candle is correctly INCLUDED in the "
                     "visible range",
                      result["latest_included"])
                # v59.89 — the v4->v5 migration checks. Every one of
                # these objects is built by a call that v5 renamed or
                # moved; if any site was missed, the construction throws
                # and the count is 0 rather than merely wrong.
                check("the marker layer round-trips through the v5 "
                     "primitive (2 set, 2 read back)",
                      result["markers_readback"] == 2,
                      str(result["markers_readback"]))
                check("the volume histogram was created",
                      result["has_volume"])
                check("all 11 indicator overlay line series were created",
                      result["overlay_count"] == 11,
                      str(result["overlay_count"]))
                check("all 4 oscillator panes were created "
                     "(macd/rsi/stoch/atr)",
                      result["pane_count"] == 4, str(result["pane_count"]))
                # A page that threw during chart construction still
                # renders; only the console records it. v5 removing the
                # v4 series methods makes that the exact failure shape.
                check("no uncaught page errors during chart construction",
                      not page_errors, "; ".join(page_errors[:3]))

            # Confirm the new 1x3 row also renders correctly in this
            # same real page load
            row = page.query_selector(".row1x3")
            check(".row1x3 renders in the actual page", row is not None)
            panels = page.query_selector_all(".row1x3 > .panel")
            check("exactly 3 panels in the new row", len(panels) == 3,
                  str(len(panels)))
            if len(panels) == 3:
                boxes = [pn.bounding_box() for pn in panels]
                same_row = all(abs(boxes[0]["y"] - b["y"]) < 5 for b in boxes)
                check("all 3 panels sit on the same row", same_row)
                titles = [pn.query_selector("h2").inner_text() for pn in panels]
                check("titles are Market Sentiment / Portfolio Risk Engine "
                     "/ Technical Analysis, in that order",
                      "Sentiment" in titles[0] and "Risk" in titles[1] and
                      "Technical" in titles[2], str(titles))

            overflow = page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth")
            check("no horizontal overflow", overflow is False)

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
