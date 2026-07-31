"""v58.73 — pasting a fresh token must actually take effect.

Reported as "alert still shows Dhan token expired after updating the
keys, it is again rolling back". The stored token was correct the whole
time (a valid 303-char JWT, saved 09:15:46). What rolled back was
nothing at all — a CACHED CLIENT kept using the old one.

`DhanClient.__init__` snapshots client_id/token into its request
headers, and `reset_dhan()` cleared only `_dhan`. `_dhan_fallback` —
the dedicated client SENSEX uses when the active broker cannot serve it
— survived, so every SENSEX prev_close_for() 401'd with the OLD token,
each 401 re-armed the 30-minute AUTH backoff, and the alert re-fired
seconds after the operator had fixed the setting.

The general rule this pins down: a cached object built from config is a
copy of config, and every such copy must be invalidated in ONE place.
The last check enforces that — a new module-level client cache that
reset_dhan() forgets will fail here rather than in production.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("builds broker clients and reads credentials")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import config
import app

# Credentials good enough for DhanClient.available() to be True. Safe:
# store.require_isolated() above guarantees this is a temp store.
config.save({"dhan_client_id": "1234567890",
             "dhan_access_token": "token-AAA"})

print("1) both cached clients are built from the CURRENT token")
app.reset_dhan()
c1 = app.dhan_client()
f1 = app._dhan_fallback_client()
check("active client built", c1 is not None)
check("fallback client built", f1 is not None)
check("active client carries token-AAA", getattr(c1, "token", None) == "token-AAA",
      str(getattr(c1, "token", None)))
check("fallback client carries token-AAA", getattr(f1, "token", None) == "token-AAA",
      str(getattr(f1, "token", None)))

print("\n2) the reported bug — a new token must reach BOTH")
config.save({"dhan_access_token": "token-BBB"})
app.reset_dhan()
c2 = app.dhan_client()
f2 = app._dhan_fallback_client()
check("active client picks up token-BBB", getattr(c2, "token", None) == "token-BBB",
      str(getattr(c2, "token", None)))
check("FALLBACK client picks up token-BBB too — the actual defect",
      getattr(f2, "token", None) == "token-BBB",
      str(getattr(f2, "token", None)))
check("neither is the stale instance", c2 is not c1 and f2 is not f1)

print("\n3) reset_dhan clears the globals, not just rebuilds on demand")
app.dhan_client(); app._dhan_fallback_client()
app.reset_dhan()
check("_dhan cleared", app._dhan is None)
check("_dhan_fallback cleared", app._dhan_fallback is None)

print("\n4) saving settings triggers the reset (the operator's actual path)")
src = open("app.py").read()
seg = src[src.index('@app.post("/api/settings")'):][:1600]
check("POST /api/settings calls reset_dhan()", "reset_dhan()" in seg)
check("...and clears the auth backoff, or a good token stays paused 30min",
      "_reset_quote_rate_limit()" in seg)

print("\n5) no cached client may be forgotten in future")
# Every module-level `_x = None` holding a broker client must be cleared
# by reset_dhan(). This is the check that would have caught the bug.
_code = [ln.split("#", 1)[0] for ln in src.splitlines()]
caches = [ln.split("=")[0].strip() for ln in _code
          if ln.startswith("_dhan") and ln.strip().endswith("= None")
          and "def " not in ln]
body_start = next(n for n, ln in enumerate(_code) if ln.startswith("def reset_dhan"))
body = "\n".join(_code[body_start:body_start + 26])
missing = [c for c in caches if f"{c} = None" not in body]
check(f"every cached client ({', '.join(caches)}) is cleared by reset_dhan",
      not missing, f"forgotten: {missing}")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
