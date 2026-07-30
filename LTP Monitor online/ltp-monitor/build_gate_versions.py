"""build_gate_versions.py -- assert the three version strings agree.

2026-07-30. The release process bumped the dashboard badge with
`sed -i '462s/v58.X/v58.Y/'`. When v58.52's CSS edit shifted the file by
two lines, that sed silently matched nothing -- sed does not error when
the pattern is absent from the addressed line -- and the badge froze at
v58.51 while VERSION and APP_VERSION advanced to v58.54. Three releases
shipped with a stale version in the UI, which is exactly the kind of
thing that makes a user doubt whether a fix landed at all.

Two lessons, both encoded here: bump by CONTENT not by line number, and
never trust a mutation that cannot fail loudly. Run this before every
package.
"""
import re
import sys

vf = open("VERSION").read().strip()
app = re.search(r'APP_VERSION = "([^"]+)"', open("app.py").read()).group(1)
badge_m = re.search(r'class="badge" style="font-weight:400">(v[\d.]+)',
                    open("static/dashboard.html").read())
badge = badge_m.group(1) if badge_m else None

print(f"  VERSION file : {vf}")
print(f"  APP_VERSION  : {app}")
print(f"  chart badge  : {badge}")
if badge is None:
    print("FAIL: badge not found -- the markup changed, update this gate")
    sys.exit(1)
if not (vf == app == badge):
    print(f"FAIL: version strings disagree ({vf} / {app} / {badge})")
    sys.exit(1)
print(f"PASS -- all three agree at {vf}")
