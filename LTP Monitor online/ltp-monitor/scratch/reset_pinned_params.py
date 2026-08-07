#!/usr/bin/env python3
"""Reset the two (symbol, strategy) pairs the tuner ratcheted into silence.

Both reached wall_gap_frac=4.0 / credit_min_frac=0.40 — the restrictive
corner of SPREAD_BOUNDS — where they produce ~0 trades. Every version
reason on the way there reads "tightening"; there is not one relaxation
in 10 and 13 versions respectively, because strategies.evaluate() gated
relaxing candidates on the incumbent's tighter persisted value (fixed
separately in v59.55).

Resets to DEFAULT_PARAMS, NOT to the permissive corner: 2.0/0.28 are the
values backtester.DEFAULT_PARAMS documents as data-driven choices, and
picking the bounds minimum instead would be choosing the number that
generates the most trades, which is the mistake in the other direction.

APPENDS a new version rather than mutating the active one, so the
ratchet stays visible in the history. Run with --apply; default is a
dry run.
"""
import json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtester as bt

P = os.path.expanduser("~/.ltp-monitor/strategy_versions.json")
BACKUP = os.path.expanduser("~/.ltp-monitor/strategy_versions.pre-unpin.json")
APPLY = "--apply" in sys.argv
TARGETS = [("bear_call_spread", "NIFTY"), ("bull_put_spread", "BANKNIFTY")]

d = json.load(open(P))
for name, sym in TARGETS:
    e = d[name]["symbols"][sym]
    cur = next(v for v in e["versions"] if v["v"] == e["active"])
    new_v = max(v["v"] for v in e["versions"]) + 1
    defaults = dict(bt.DEFAULT_PARAMS[name])
    print(f"\n  {name}/{sym}")
    print(f"    live_enabled       : {e.get('live_enabled')}   <-- unchanged either way")
    print(f"    active  v{cur['v']:<3}       : {cur['params']}")
    print(f"    NEW     v{new_v:<3}       : {defaults}")
    print(f"    tuning_exhausted   : {e.get('tuning_exhausted')} -> False")
    print(f"    tuning_attempts    : {e.get('tuning_attempts')} -> 0")
    if APPLY:
        e["versions"].append({
            "v": new_v, "params": defaults,
            "reason": ("reset: ratcheted to the SPREAD_BOUNDS restrictive corner "
                       "(4.0/0.40) producing ~0 trades — every prior version reason "
                       "reads 'tightening'. Relaxing candidates were unmeasurable "
                       "until strategies.evaluate() accepted caller params (v59.55). "
                       "Reset to DEFAULT_PARAMS, not to the permissive bound."),
            "created": bt._now(), "last_tested": None, "results": None})
        e["active"] = new_v
        e["tuning_exhausted"] = False
        e["tuning_attempts"] = 0

if not APPLY:
    print("\n  DRY RUN — nothing written. Re-run with --apply.")
    sys.exit(0)
shutil.copy2(P, BACKUP)
json.dump(d, open(P, "w"), indent=1)
print(f"\n  written. backup: {BACKUP}")
