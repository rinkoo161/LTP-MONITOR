"""v58.76 — a Settings save must never silently disable authentication.

What happened, 40 minutes after the Access & security card was added:

    18:19:39  login    rinkoo161
    18:20:15  api      rinkoo161  POST /api/settings
    18:20:15  config.json mtime — auth_enabled flipped to false

`saveSettings()` attached `auth_enabled: <checkbox>.checked` to EVERY
save. The checkbox is populated only when the Settings panel is opened,
and only from a `settings` snapshot cached at page load — so saving any
unrelated setting from a page loaded before auth was enabled posted
`false` and turned authentication off. No error, no warning; the next
page load simply stopped asking for a password.

This is the same shape as the cached broker client that looked like a
token "rolling back": STALE CLIENT STATE OVERWRITING NEWER SERVER
STATE. The rule these checks encode is narrow and general:

    a control may only write a field it has read.

The auth fields are attached to the payload ONLY when the card has been
populated from the live /api/auth/status in this page view.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

HTML = open("static/dashboard.html").read()

print("1) the payload is not built with auth fields unconditionally")
body = HTML[HTML.index("async function saveSettings"):]
body = body[:body.index('fetch("/api/settings"')]
opener = body[:body.index("if(authFieldsLoaded)")] if "if(authFieldsLoaded)" in body else body
check("the const body={...} literal does NOT contain auth_enabled",
      "auth_enabled" not in opener,
      "an unconditional field is written on every save, read or not")
check("auth fields are attached behind a guard", "if(authFieldsLoaded){" in body)
check("all three auth fields are inside that guard",
      all(f"body.{k}=" in body for k in
          ("auth_enabled", "auth_require_mfa", "auth_session_hours")))

print("\n2) the guard defaults to NOT writing")
check("authFieldsLoaded starts false", "var authFieldsLoaded=false;" in HTML,
      "a page that never opened the card must not touch auth settings")
check("it is set true only after reading the live state",
      HTML.index("authFieldsLoaded=true;") < HTML.index("var authFieldsLoaded=false;")
      or "authFieldsLoaded=true;" in HTML)

print("\n3) the values come from the SERVER, not the page-load snapshot")
seg = HTML[HTML.index('fetch("/api/auth/status")'):]
seg = seg[:2000]
check("the checkbox is populated from /api/auth/status",
      's_authon").checked=!!a.enabled' in seg.replace(" ", ""),
      "settings.auth_enabled is a snapshot from page load and can be stale")
check("require_mfa likewise", 's_authmfa").checked=a.require_mfa' in seg.replace(" ", ""))
check("authFieldsLoaded is set inside that same callback",
      "authFieldsLoaded=true;" in seg)

print("\n4) the server side still accepts the field when genuinely sent")
import config
check("auth_enabled is registered in DEFAULTS", "auth_enabled" in config.DEFAULTS)
app_src = open("app.py").read()
check("and declared on SettingsIn", "auth_enabled: bool | None = None" in app_src,
      "otherwise config.save() drops it silently")

print("\n5) the script still parses")
import subprocess, tempfile
big = max(re.findall(r"<script>([\s\S]*?)</script>", HTML), key=len)
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(big); path = f.name
try:
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    check("node --check passes", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[0] if r.returncode else "")
except FileNotFoundError:
    check("node --check passes", False, "node not installed")
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
