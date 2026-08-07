#!/usr/bin/env python3
"""Part 0.1 — power analysis from the actual closed-trade history."""
import json, os, collections, math, statistics as st
P = os.path.expanduser("~/.ltp-monitor/trades.jsonl")
rows = [json.loads(l) for l in open(P) if l.strip()]

def fam(t):
    if t.get("leg") == "SPREAD": return "credit spread"
    if t.get("kind") == "future": return "futures"
    if t.get("leg") in ("CE", "PE"): return "long option"
    return "other"

g = collections.defaultdict(list)
for t in rows:
    g[fam(t)].append(t.get("pnl", 0) or 0)
g["ALL"] = [t.get("pnl", 0) or 0 for t in rows]

Z_A, Z_B = 1.959963985, 0.841621234      # two-sided a=0.05, power 0.80
K = (Z_A + Z_B) ** 2                      # 7.849

print(f"{'family':16} {'n':>5} {'mean':>9} {'sd':>9} " + "".join(f"{'n@'+str(d):>10}" for d in (200,500,1000)))
for k in ("long option", "credit spread", "futures", "ALL"):
    v = g.get(k) or []
    if len(v) < 2: continue
    sd = st.pstdev(v); mean = st.mean(v)
    need = [math.ceil(K * sd * sd / (d * d)) for d in (200, 500, 1000)]
    print(f"{k:16} {len(v):5d} {mean:+9.0f} {sd:9.0f} " + "".join(f"{x:10,d}" for x in need))

print()
print("trading-days to reach those counts at a 5-trade/day cap (250 sessions/yr):")
for k in ("long option", "credit spread", "futures", "ALL"):
    v = g.get(k) or []
    if len(v) < 2: continue
    sd = st.pstdev(v)
    out = []
    for d in (200, 500, 1000):
        n = math.ceil(K * sd * sd / (d * d))
        out.append(f"{n/5:,.0f}d ({n/5/250:.1f}yr)")
    print(f"  {k:16} " + "  ".join(f"{o:>18}" for o in out))
