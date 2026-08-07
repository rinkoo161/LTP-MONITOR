#!/usr/bin/env python3
"""Part 0.1b — power at the level promotion decisions are made.

Uses strategy_stats' labelling rather than a fresh one: most option
strategies record their name in `setup`, not `strategy`, and a
hand-rolled labeller collapsed them all into "(option, unattributed)".
"""
import sys, os, math, json, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy_stats as ss

rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
g = collections.defaultdict(list)
for t in rows:
    g[ss._label(t)].append(t.get("pnl", 0) or 0)

K = (1.959963985 + 0.841621234) ** 2
print(f"{'strategy':34} {'n':>4} {'mean':>7} {'sd':>7} {'t':>6} {'n@500':>7} {'yrs@1/day':>10}")
for k in sorted(g, key=lambda k: -len(g[k])):
    v = g[k]
    if len(v) < 2: continue
    sd = st.pstdev(v); mean = st.mean(v)
    need = math.ceil(K * sd * sd / 500 ** 2)
    t = mean / (sd / math.sqrt(len(v))) if sd else float("nan")
    print(f"{k[:34]:34} {len(v):4d} {mean:+7.0f} {sd:7.0f} {t:+6.2f} {need:7,d} {need/250:10.1f}")
