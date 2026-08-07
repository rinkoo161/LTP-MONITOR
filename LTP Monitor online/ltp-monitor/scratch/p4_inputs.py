#!/usr/bin/env python3
"""Part 4 — the inputs the protocol needs, measured not assumed."""
import json, os, math, itertools, statistics as st, collections, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
H = os.path.expanduser("~/.ltp-monitor")

print("A) HOW MANY CONFIGURATIONS HAVE BEEN TRIED ON THIS CODEBASE")
tot = 0
for f, key in (("backtests.json", None), ("strategy_versions.json", None)):
    p = os.path.join(H, f)
    if not os.path.exists(p):
        print(f"    {f:26} MISSING"); continue
    d = json.load(open(p))
    n = sum(len(v) if isinstance(v, list) else 1 for v in d.values()) if isinstance(d, dict) else len(d)
    print(f"    {f:26} {n:6,} recorded entries  ({len(d) if hasattr(d,'__len__') else '?'} keys)")
    tot += n

import pa_strategies as pa, strategies as sp
def grid(bounds):
    out = {}
    for name, params in bounds.items():
        n = 1
        for k, b in params.items():
            try:
                lo, hi, step = b[0], b[1], (b[2] if len(b) > 2 else None)
                n *= max(1, int(round((hi - lo) / step)) + 1) if step else 3
            except Exception:
                n *= 3
        out[name] = n
    return out
g1, g2 = grid(pa.PA_BOUNDS), grid(sp.SPREAD_BOUNDS)
print(f"\n    tuner grid cardinality (one full sweep = one trial each):")
for nm, n in sorted({**g1, **g2}.items(), key=lambda x: -x[1]):
    print(f"      {nm:26} {n:12,}")
gtot = sum({**g1, **g2}.values())
print(f"      {'TOTAL one full sweep':26} {gtot:12,}")

print(f"\n    >>> N_trials for the deflated Sharpe must be at least {max(tot,1):,} "
      f"(recorded)\n        and is realistically ~{gtot:,} if the tuner ever swept the full grid.")

print("\n\nB) MAX HOLDING PERIOD OBSERVED  (sets the embargo)")
p = os.path.join(H, "trades.jsonl")
rows = [json.loads(l) for l in open(p) if l.strip()]
hold = []
for t in rows:
    a, b = t.get("entry_ts"), t.get("exit_ts") or t.get("ts")
    if a and b and b > a: hold.append((b - a) / 60.0)
if hold:
    hold.sort()
    print(f"    n={len(hold)}  median {st.median(hold):.0f} min   "
          f"p95 {hold[int(len(hold)*.95)]:.0f} min   max {hold[-1]:.0f} min "
          f"({hold[-1]/60:.1f} h)")
print("    All candidates square off same-day => max holding period < 1 session.")

print("\n\nC) EXPECTED MAX SHARPE UNDER THE NULL  (Bailey/Lopez de Prado)")
G = 0.5772156649
def z_inv(p):  # inverse normal CDF, Acklam
    a=[-3.969683028665376e+01,2.209460984245205e+02,-2.759285104469687e+02,1.383577518672690e+02,-3.066479806614716e+01,2.506628277459239e+00]
    b=[-5.447609879822406e+01,1.615858368580409e+02,-1.556989798598866e+02,6.680131188771972e+01,-1.328068155288572e+01]
    c=[-7.784894002430293e-03,-3.223964580411365e-01,-2.400758277161838e+00,-2.549732539343734e+00,4.374664141464968e+00,2.938163982698783e+00]
    d=[7.784695709041462e-03,3.224671290700398e-01,2.445134137142996e+00,3.754408661907416e+00]
    pl=0.02425
    if p<pl:
        q=math.sqrt(-2*math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p<=1-pl:
        q=p-0.5; r=q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q/(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q=math.sqrt(-2*math.log(1-p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)

print(f"    {'N trials':>12} {'E[max SR] (per-trade units, V[SR]=1)':>40}")
for N in (11, 100, 1000, max(tot,1), gtot):
    e = (1-G)*z_inv(1-1.0/N) + G*z_inv(1-1.0/(N*math.e))
    print(f"    {N:12,} {e:40.3f}")
print("    (multiply by sqrt(V[SR]) across trials to get the hurdle in Sharpe units)")
