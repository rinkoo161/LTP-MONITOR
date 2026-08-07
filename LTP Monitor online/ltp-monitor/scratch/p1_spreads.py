#!/usr/bin/env python3
"""Part 1.5 — the S5/S6 exception: were the credit spreads ever given a
fair sample, or starved by the wall-gap / credit-fraction filters?"""
import json, os, collections, math, statistics as st, sys, re
sys.path.insert(0, ".")
import strategies as slib, backtester as bt

rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
sp = [t for t in rows if t.get("leg") == "SPREAD"]
by = collections.defaultdict(list)
for t in sp: by[t.get("strategy") or "?"].append(t.get("pnl", 0) or 0)

print("  credit spreads, reported separately (post-restatement):")
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    sd = st.pstdev(v) if len(v) > 1 else 0
    t = (st.mean(v)/(sd/math.sqrt(len(v)))) if sd else float('nan')
    print(f"    {k:20} n={len(v):4d}  net Rs {sum(v):+8,.0f}  mean {st.mean(v):+7.0f}  "
          f"sd {sd:7.0f}  t {t:+5.2f}  win {100*sum(1 for x in v if x>0)/len(v):3.0f}%")

print("\n  the two filters that could starve them (SPREAD_BOUNDS):")
for name in ("bull_put_spread", "bear_call_spread"):
    b = slib.SPREAD_BOUNDS.get(name, {})
    p = bt.get_params(name, "FINNIFTY")
    print(f"    {name:18} wall_gap_frac bounds {b.get('wall_gap_frac')}  live {p.get('wall_gap_frac')}")
    print(f"    {'':18} credit_min_frac bounds {b.get('credit_min_frac')}  live {p.get('credit_min_frac')}")

print("\n  how often did the filters actually reject a candidate?")
log = os.path.expanduser("~/.ltp-monitor/activity.log")
reasons = collections.Counter()
skips = collections.Counter()
for l in open(log, errors="ignore"):
    if "no spreads deployed this cycle" in l:
        m = re.search(r"\{(.*)\}", l)
        if m:
            for k, v in re.findall(r"'(\w+)':\s*(\d+)", m.group(1)):
                skips[k] += int(v)
    if "wall too close" in l: reasons["wall_gap_frac (wall too close to spot)"] += 1
    if "% of" in l and "width" in l: reasons["credit_min_frac (credit too thin)"] += 1
print("    aggregate skip reasons from the deploy loop:")
for k, v in skips.most_common(10):
    print(f"      {k:26} {v:,}")
print("    eligibility rejections seen in the log:")
for k, v in reasons.most_common():
    print(f"      {k:44} {v:,}")
