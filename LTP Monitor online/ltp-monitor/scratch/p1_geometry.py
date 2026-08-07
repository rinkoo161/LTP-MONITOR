#!/usr/bin/env python3
"""Part 1.3 — what spot move does T1 actually require, and how often
does the market deliver it from our actual entry times?

Premium and delta come from chain_snapshots at the real entry moments,
not from an assumption.
"""
import json, os, sqlite3, datetime, statistics as st
DB = os.path.expanduser("~/.ltp-monitor/history.db")
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
opts = [t for t in rows if t.get("leg") in ("CE", "PE")]

snapdays = {r[0] for r in c.execute(
    "SELECT DISTINCT date(ts,'unixepoch','+5 hours','+30 minutes') FROM chain_snapshots")}

# 1) measured ATM premium as a fraction of spot, and delta, at entry
prem_frac, deltas, samples = [], [], []
for t in opts:
    d = str(t.get("closed_date"))
    if d not in snapdays: continue
    try:
        ts = int(datetime.datetime.strptime(f"{d} {t['opened']}", "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception: continue
    r = c.execute("SELECT ltp, delta, strike FROM chain_snapshots WHERE symbol=? AND strike=? "
                  "AND leg=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                  (t["symbol"], t.get("strike"), (t.get("leg") or "").lower(),
                   ts-180, ts+180, ts)).fetchone()
    if not r or not r["ltp"]: continue
    spot = t.get("strike")          # ATM entries, so strike ~ spot
    prem_frac.append(r["ltp"] / spot)
    if r["delta"]: deltas.append(abs(r["delta"]))
    samples.append((t["symbol"], r["ltp"], spot, r["delta"], t.get("target1"), t.get("entry")))

print(f"  measured at {len(prem_frac)} real entries with a snapshot:")
if prem_frac:
    print(f"    ATM premium as % of spot : median {100*st.median(prem_frac):.2f}%")
if deltas:
    print(f"    |delta| at entry          : median {st.median(deltas):.3f}  n={len(deltas)}")
else:
    print("    |delta| at entry          : NOT RECORDED in chain_snapshots (all null)")

# 2) required spot move for T1 / T2 given the strategies' own targets
print()
print("  required SPOT move to reach the strategy's own premium targets")
print("  (premium target / delta, i.e. how far the index must travel)")
D = st.median(deltas) if deltas else 0.50
for label, mult in (("T1 (+60% premium, S1 spec)", 0.60), ("T2 (+105%)", 1.05),
                    ("T1 (+30%, retuned)", 0.30), ("T2 (+40%, retuned)", 0.40)):
    for sym, spot in (("NIFTY", 24650), ("BANKNIFTY", 57800)):
        pf = st.median(prem_frac) if prem_frac else 0.006
        prem = spot * pf
        need = prem * mult / D
        print(f"    {label:28} {sym:9} premium ~{prem:6.0f}  needs {need:6.0f} pts = {100*need/spot:5.2f}% of spot")
    break_ = None

# 3) empirical: how far DID spot move from each entry time to session close?
print()
print("  empirical spot move from ACTUAL entry times to 15:15, by symbol")
sid = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "SENSEX": "51"}
moves = {}
for t in opts:
    d = str(t.get("closed_date")); s = t["symbol"]
    if s not in sid: continue
    try:
        ts = int(datetime.datetime.strptime(f"{d} {t['opened']}", "%Y-%m-%d %H:%M:%S").timestamp())
        end = int(datetime.datetime.strptime(f"{d} 15:15:00", "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception: continue
    a = c.execute("SELECT c FROM candles WHERE security_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",(sid[s], ts)).fetchone()
    b = c.execute("SELECT c FROM candles WHERE security_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",(sid[s], end)).fetchone()
    if a and b and a[0]:
        moves.setdefault(s, []).append(abs(b[0]-a[0]))
for s, v in moves.items():
    v = sorted(v)
    if len(v) < 3: continue
    print(f"    {s:9} n={len(v):3d}  median {st.median(v):6.0f} pts   p75 {v[int(.75*len(v))]:6.0f}   p90 {v[int(.90*len(v))]:6.0f}   max {v[-1]:6.0f}")
