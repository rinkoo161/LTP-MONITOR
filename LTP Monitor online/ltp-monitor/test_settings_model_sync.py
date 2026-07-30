"""v54 — regression guard for a severe bug found while building the
Strategies-page consolidated table: SettingsIn (the /api/settings
pydantic model) was missing 46 of config.DEFAULTS' 117 keys, including
every S7 and Futures Phase 2 setting. FastAPI/pydantic silently drops
undeclared fields BEFORE config.save() ever sees them, one layer
earlier than the "config.save() warns on dropped keys" fix from v53 —
so those Settings subcards rendered and read back correctly but never
actually persisted a save. This test makes the gap impossible to
reintroduce silently: it fails loudly (not just logs) the moment
SettingsIn and config.DEFAULTS drift apart again.

Run:  python3 test_settings_model_sync.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) SettingsIn declares every config.DEFAULTS key")
src = open("app.py").read()
m = re.search(r'class SettingsIn\(BaseModel\):(.*?)\n\n\n@app\.get\("/api/settings"\)',
             src, re.S)
check("SettingsIn class found in app.py", m is not None)
body = m.group(1)
declared = set(re.findall(r'^\s+(\w+):', body, re.M))
missing = sorted(set(config.DEFAULTS.keys()) - declared)
check("no config.DEFAULTS key is missing from SettingsIn",
      not missing, f"missing: {missing}" if missing else "")

print("\n2) end-to-end: a real HTTP POST to /api/settings persists "
      "keys that were silently dropped before this fix")
from fastapi.testclient import TestClient
import app
client = TestClient(app.app)

probe_keys = [
    ("s7_auto_deploy", True, False),
    ("futures_auto_deploy", True, False),
    ("chain_snapshot_retention_days", 9, config.DEFAULTS["chain_snapshot_retention_days"]),
    ("spread_defense_zone_pct", 40, config.DEFAULTS["spread_defense_zone_pct"]),
]
for key, test_val, restore_val in probe_keys:
    r = client.post("/api/settings", json={key: test_val})
    check(f"POST /api/settings persists '{key}' (status {r.status_code})",
          r.status_code == 200 and config.load().get(key) == test_val,
          f"disk value = {config.load().get(key)!r}")
    config.save({key: restore_val})   # restore immediately, one key at a time

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
