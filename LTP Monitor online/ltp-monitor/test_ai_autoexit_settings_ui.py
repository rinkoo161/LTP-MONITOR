"""v58.22+ — tests for the new Settings page toggles: AI option
auto-exit and AI futures auto-exit, matching the existing AI spread
auto-exit toggle exactly, so the new config keys added for the option/
futures AI advisories are actually reachable from the UI, not just
editable via config.json directly.

Run:  python3 test_ai_autoexit_settings_ui.py
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

print("1) source-level: both new toggles exist in the HTML, load "
     "path, and save path — matching the existing spread toggle's "
     "exact pattern")
check("option auto-exit checkbox exists in the HTML",
      'id="s_option_ai_autoexit"' in h)
check("futures auto-exit checkbox exists in the HTML",
      'id="s_futures_ai_autoexit"' in h)
check("option auto-exit is wired into the settings load path",
      's_option_ai_autoexit").checked=!!settings.option_ai_auto_exit_enabled'
      in h)
check("futures auto-exit is wired into the settings load path",
      's_futures_ai_autoexit").checked=!!settings.futures_ai_auto_exit_enabled'
      in h)
check("option auto-exit is wired into the settings save path",
      'option_ai_auto_exit_enabled:document.getElementById("s_option_ai_autoexit").checked'
      in h)
check("futures auto-exit is wired into the settings save path",
      'futures_ai_auto_exit_enabled:document.getElementById("s_futures_ai_autoexit").checked'
      in h)

print("\n2) JS syntax still valid")
js = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S))
open("/tmp/ai_autoexit_test.js", "w").write(js)
r = subprocess.run(["node", "--check", "/tmp/ai_autoexit_test.js"],
                  capture_output=True, text=True)
check("node --check passes with zero errors", r.returncode == 0, r.stderr[:300])

print("\n3) REAL BROWSER VERIFICATION: both toggles render on the "
     "Settings page and correctly reflect a saved True value")
import app as app_module
import uvicorn
from playwright.sync_api import sync_playwright

config_u = uvicorn.Config(app_module.app, host="127.0.0.1", port=8941, log_level="error")
server = uvicorn.Server(config_u)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
time.sleep(2)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto("http://127.0.0.1:8941/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        # openSettings() reads from the global `settings` JS variable
        # (populated elsewhere on page load, not re-fetched by
        # openSettings itself) — set it directly rather than depend on
        # route-interception timing for a variable this function
        # doesn't fetch on its own.
        page.evaluate('''() => {
            settings.option_ai_auto_exit_enabled = true;
            settings.futures_ai_auto_exit_enabled = true;
            settings.spread_ai_auto_exit_enabled = false;
            openSettings();
        }''')
        page.wait_for_timeout(500)

        opt_cb = page.query_selector("#s_option_ai_autoexit")
        fut_cb = page.query_selector("#s_futures_ai_autoexit")
        check("option auto-exit checkbox is present in the rendered "
             "page", opt_cb is not None)
        check("futures auto-exit checkbox is present in the rendered "
             "page", fut_cb is not None)
        if opt_cb and fut_cb:
            check("option auto-exit checkbox correctly checked (True "
                 "in the settings object)",
                  opt_cb.is_checked())
            check("futures auto-exit checkbox correctly checked (True "
                 "in the settings object)",
                  fut_cb.is_checked())

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
