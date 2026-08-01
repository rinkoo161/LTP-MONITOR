"""v58.74 — the alert banner must clear itself after 10 seconds.

It used to stay until clicked. A single high-severity alert therefore
sat across the top of every page for the rest of the session, and since
the app raises "Dhan token expired" and kill-switch alerts as HIGH, the
banner was frequently a permanent fixture reporting something the
operator had already dealt with.

Auto-dismiss is only safe because `seenAlertIds` already prevents a
dismissed alert being re-shown by the next poll — otherwise hiding it
would just make it flicker every polling interval. The alert itself is
not lost: the bell panel keeps the full list.

Verifies by parsing the shipped dashboard.html (there is no build step,
so the file IS the artifact) and by running `node --check` over the
extracted script, which is this project's existing convention for
frontend changes.
"""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

HTML = open("static/dashboard.html").read()

print("1) the timing contract")
check("a 10-second constant exists", "ALERT_BANNER_MS=10000" in HTML.replace(" ", ""),
      "10s per the request")
m = re.search(r"ALERT_BANNER_MS\s*=\s*(\d+)", HTML)
check("and it is used as the banner timeout, not just declared",
      bool(re.search(r"setTimeout\([\s\S]{0,400}?ALERT_BANNER_MS\s*\)", HTML)),
      str(m.group(1)) if m else "not found")
# Compare the two durations rather than grepping for a literal near a
# marker — the fade constant sits BETWEEN the declaration and the use of
# ALERT_BANNER_MS, so a positional search finds nothing and fails against
# correct code.
_show = HTML[HTML.index("function showAlertBanner"):HTML.index("function renderAlerts")]
_fades = [int(x) for x in re.findall(r"\}\s*,\s*(\d+)\s*\)", _show)]
check("the fade is shorter than the visible window",
      bool(_fades) and m and min(_fades) < int(m.group(1)),
      f"fade {min(_fades) if _fades else '?'}ms vs window {m.group(1) if m else '?'}ms")

print("\n2) it actually hides the element")
seg = HTML[HTML.index("function showAlertBanner"):HTML.index("function renderAlerts")]
check("the timer sets display:none", 'style.display="none"' in seg)
check("via a fade class first", '.add("fading")' in seg)
check("and clears the class so the next banner is visible",
      'remove("fading")' in seg)

print("\n3) timers cannot cut a NEW banner short")
check("a shared clear helper exists", "function clearAlertBannerTimers" in HTML)
check("showAlertBanner clears before arming",
      seg.index("clearAlertBannerTimers()") < seg.index("setTimeout"),
      "an in-flight timer from the previous alert would hide the new one")
check("both timer handles are cleared",
      HTML.count("alertBannerTimer=null") >= 2 and "alertFadeTimer=null" in HTML)

print("\n4) manual dismiss still works and cancels the timer")
tog = HTML[HTML.index("function toggleAlertPanel"):]
tog = tog[:tog.index("}") + 1]
check("clicking the banner routes through hideAlertBanner", "hideAlertBanner()" in tog,
      tog.replace("\n", " ")[:90])
check("hideAlertBanner clears the timers",
      "clearAlertBannerTimers()" in HTML[HTML.index("function hideAlertBanner"):
                                         HTML.index("function showAlertBanner")])

print("\n5) nothing is lost when the banner goes")
check("the bell panel is still populated from the full alert list",
      'panel.innerHTML=alerts.map' in HTML.replace(" ", ""),
      "the banner is transient; the panel is the record")
check("seenAlertIds still gates re-display",
      "seenAlertIds.add" in HTML and "seenAlertIds.has" in HTML,
      "without this, hiding would make it flicker each poll")

print("\n6) the shipped script still parses")
scripts = re.findall(r"<script>([\s\S]*?)</script>", HTML)
big = max(scripts, key=len)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(big); path = f.name
try:
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    check("node --check passes with zero errors", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[0] if r.returncode else "")
except FileNotFoundError:
    check("node --check passes with zero errors", False, "node not installed")
finally:
    os.unlink(path)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
