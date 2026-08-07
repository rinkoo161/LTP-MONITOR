#!/usr/bin/env python3
"""Part 0.4 — live config.json vs DEFAULTS, flagging keys that move
position sizing, risk limits or gate thresholds."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

live = json.load(open(os.path.expanduser("~/.ltp-monitor/config.json")))
D = config.DEFAULTS
SEC = set(getattr(config, "SECRET_KEYS", []))

MATERIAL = ("lot", "risk", "loss", "cap", "max_", "min_", "confidence", "fee",
            "sizing", "trades_per_day", "concurrent", "profit", "stop",
            "cooldown", "paper", "enabled", "halfspread", "capital", "target")

rows = []
for k in sorted(set(D) | set(live)):
    if k in SEC: continue
    dv, lv = D.get(k, "<absent>"), live.get(k, "<absent>")
    if dv == lv: continue
    material = any(m in k for m in MATERIAL)
    rows.append((material, k, dv, lv))

mat = [r for r in rows if r[0]]
oth = [r for r in rows if not r[0]]
print(f"  {len(rows)} keys differ from DEFAULTS; {len(mat)} touch sizing / risk / gates\n")
print(f"  {'key':38} {'DEFAULT':>22}   LIVE")
for _, k, dv, lv in mat:
    print(f"  {k:38} {str(dv)[:22]:>22} -> {str(lv)[:40]}")
print(f"\n  non-material ({len(oth)}): {', '.join(k for _, k, _, _ in oth)}")
