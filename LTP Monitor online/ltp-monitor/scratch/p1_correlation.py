#!/usr/bin/env python3
"""Part 1.1 — signal correlation and effective number of independent bets.

Two measures, deliberately:

  (a) Pearson r on the raw +1/-1/0 vectors, as the brief asks. On a
      sparse grid this is dominated by co-ABSENCE — two strategies that
      both sit flat in the same bucket score as agreeing. Reported, but
      not trusted alone.
  (b) Conditional agreement: of the buckets where BOTH strategies fired,
      what fraction agreed on direction? This is the measure that
      actually speaks to "one strategy expressed N ways".

Grid: (day, symbol, 30-minute bucket). Direction +1 BUY_CE / -1 BUY_PE.
"""
import json, os, collections, math, itertools

rows = []
for l in open(os.path.expanduser("~/.ltp-monitor/shadow_signals.jsonl")):
    l = l.strip()
    if not l: continue
    try: rows.append(json.loads(l))
    except Exception: pass

def bucket(r):
    ts = str(r.get("ts") or "")
    if len(ts) < 16: return None
    hh, mm = int(ts[11:13]), int(ts[14:16])
    return (ts[:10], r.get("symbol"), hh * 2 + (mm // 30))

def direc(r):
    s = str(r.get("signal") or "")
    return 1 if s == "BUY_CE" else (-1 if s == "BUY_PE" else 0)

vec = collections.defaultdict(dict)     # strategy -> bucket -> dir
for r in rows:
    src = r.get("source") or r.get("strategy")
    if not src: continue
    src = str(src)
    if src.startswith("rule-engine"): src = "rule-engine"
    b, d = bucket(r), direc(r)
    if b is None or d == 0: continue
    vec[src][b] = d

names = [k for k in vec if len(vec[k]) >= 8]
names.sort(key=lambda k: -len(vec[k]))
print(f"  {len(names)} strategies with >=8 directional signals\n")
print(f"  {'strategy':22} {'signals':>8} {'buckets':>8}")
for n in names: print(f"  {n[:22]:22} {len(vec[n]):8d} {len(set(vec[n])):8d}")

allb = sorted({b for n in names for b in vec[n]})
idx = {b: i for i, b in enumerate(allb)}
print(f"\n  common grid: {len(allb)} (day, symbol, 30-min) buckets where anything fired\n")

def col(n):
    v = [0] * len(allb)
    for b, d in vec[n].items(): v[idx[b]] = d
    return v

M = {n: col(n) for n in names}

def pearson(a, b):
    n = len(a); ma = sum(a)/n; mb = sum(b)/n
    va = sum((x-ma)**2 for x in a); vb = sum((y-mb)**2 for y in b)
    if va == 0 or vb == 0: return float("nan")
    return sum((x-ma)*(y-mb) for x, y in zip(a, b)) / math.sqrt(va*vb)

print("  (a) Pearson r on +1/-1/0 vectors")
print("      " + "".join(f"{n[:9]:>10}" for n in names))
R = []
for i in names:
    row = [pearson(M[i], M[j]) for j in names]
    R.append(row)
    print(f"  {i[:9]:9} " + "".join(f"{x:10.2f}" for x in row))

print("\n  (b) conditional agreement — of buckets where BOTH fired, % same direction")
print("      " + "".join(f"{n[:9]:>10}" for n in names))
for i in names:
    out = []
    for j in names:
        common = set(vec[i]) & set(vec[j])
        if i == j: out.append("     self"); continue
        if not common: out.append("       - "); continue
        agree = sum(1 for b in common if vec[i][b] == vec[j][b])
        out.append(f"{100*agree/len(common):7.0f}%{len(common):>3}")
    print(f"  {i[:9]:9} " + "".join(f"{o:>10}" for o in out))

# eigenvalue spectrum of the Pearson matrix -> effective independent bets
def eig_sym(A, iters=500):
    n = len(A); vals = []; B = [r[:] for r in A]
    for _ in range(n):
        v = [1.0/math.sqrt(n)]*n
        for _ in range(iters):
            w = [sum(B[i][j]*v[j] for j in range(n)) for i in range(n)]
            nm = math.sqrt(sum(x*x for x in w)) or 1.0
            v = [x/nm for x in w]
        lam = sum(v[i]*sum(B[i][j]*v[j] for j in range(n)) for i in range(n))
        vals.append(lam)
        for i in range(n):
            for j in range(n): B[i][j] -= lam*v[i]*v[j]
    return vals

R2 = [[0.0 if (x != x) else x for x in row] for row in R]
ev = sorted((abs(e) for e in eig_sym(R2)), reverse=True)
tot = sum(ev) or 1
print(f"\n  eigenvalues: {' '.join(f'{e:.2f}' for e in ev)}")
print(f"  variance explained by PC1: {100*ev[0]/tot:.0f}%")
enb = (sum(ev)**2) / sum(e*e for e in ev)
print(f"  effective number of independent bets (participation ratio): {enb:.2f} of {len(names)}")
