"""v55.1 — tests for /api/strategies/pa_toggle, which closes the
"Auto Deploy is read-only for orb/vwap_pullback/ema_mtf" gap flagged
since v54.1. Also guards against reintroducing the v54 SettingsIn/
config.DEFAULTS sync bug for this specific new key.

Run:  python3 test_pa_toggle.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
from fastapi.testclient import TestClient
import app
import config
import pa_strategies as pa

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)

print("1) pa_enabled registered correctly (DEFAULTS + SettingsIn, "
      "the exact gap that bit S7/futures in v54)")
check("pa_enabled is in config.DEFAULTS", "pa_enabled" in config.DEFAULTS)
check("default value matches pa_strategies.PA_NAMES exactly (behavior-"
      "preserving for anyone who never touched this setting)",
      set(config.DEFAULTS["pa_enabled"]) == set(pa.PA_NAMES),
      f"{config.DEFAULTS['pa_enabled']} vs {list(pa.PA_NAMES)}")
src = open("app.py").read()
m = re.search(r'class SettingsIn\(BaseModel\):(.*?)\n\n\n@app\.get\("/api/settings"\)',
             src, re.S)
declared = set(re.findall(r'^\s+(\w+):', m.group(1), re.M))
check("pa_enabled is declared on SettingsIn (else POSTs would silently drop it)",
      "pa_enabled" in declared)

print("\n2) endpoint: real add/remove via the actual HTTP path")
before = config.load().get("pa_enabled")
r = client.post("/api/strategies/pa_toggle", json={"name": "orb", "enabled": False})
check("disabling orb returns 200 and removes it", r.status_code == 200 and
      "orb" not in r.json().get("pa_enabled", ["orb"]), str(r.json()))
check("disk value actually updated (not just the response)",
      "orb" not in (config.load().get("pa_enabled") or ["orb"]))
r2 = client.post("/api/strategies/pa_toggle", json={"name": "orb", "enabled": True})
check("re-enabling orb returns 200 and restores it",
      r2.status_code == 200 and "orb" in r2.json().get("pa_enabled", []),
      str(r2.json()))
config.save({"pa_enabled": before})   # restore exactly what was there

print("\n3) invalid strategy name rejected, not silently accepted")
r3 = client.post("/api/strategies/pa_toggle", json={"name": "not_real", "enabled": True})
check("an unknown strategy name returns an error, not a fabricated success",
      "error" in r3.json(), str(r3.json()))
check("config.json untouched by the rejected call",
      config.load().get("pa_enabled") == before,
      f"{config.load().get('pa_enabled')} vs {before}")

print("\n4) PriceActionAgent's existing read path is unaffected")
import agents
cfg = config.load()
enabled = cfg.get("pa_enabled", list(pa.PA_NAMES))
check("PriceActionAgent's own cfg.get(...) call still resolves to a "
      "real list after registering the default (not None/crash)",
      isinstance(enabled, list) and len(enabled) > 0, str(enabled))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
