#!/usr/bin/env python3
"""Part 1.4 — theta bleed over the ACTUAL holding-period distribution,
separated from directional P&L, and the gross directional edge in index
points required merely to break even."""
import json, os, sqlite3, datetime, statistics as st
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db")); c.row_factory = sqlite3.Row
rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
opts = [t for t in rows if t.get("leg") in ("CE", "PE")]

def secs(t):
    try:
        a = [int(x) for x in str(t["opened"]).split(":")]
        b = [int(x) for x in str(t["closed"]).split(":")]
        return (b[0]*3600+b[1]*60+b[2]) - (a[0]*3600+a[1]*60+a[2])
    except Exception: return None

holds = [s for s in (secs(t) for t in opts) if s and s > 0]
print(f"  holding period, {len(holds)} option trades:")
h = sorted(holds)
print(f"    median {st.median(h)/60:.1f} min   p25 {h[len(h)//4]/60:.1f}   p75 {h[3*len(h)//4]/60:.1f}   max {h[-1]/60:.0f} min")

snapdays = {r[0] for r in c.execute("SELECT DISTINCT date(ts,'unixepoch','+5 hours','+30 minutes') FROM chain_snapshots")}
thetas = []
for t in opts:
    d = str(t.get("closed_date"))
    if d not in snapdays: continue
    try:
        ts = int(datetime.datetime.strptime(f"{d} {t['opened']}", "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception: continue
    r = c.execute("SELECT theta, ltp FROM chain_snapshots WHERE symbol=? AND strike=? AND leg=? "
                  "AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                  (t["symbol"], t.get("strike"), (t.get("leg") or "").lower(), ts-180, ts+180, ts)).fetchone()
    if r and r["theta"]:
        thetas.append((abs(r["theta"]), t.get("qty") or 0, secs(t) or 0, t["symbol"]))

if not thetas:
    print("\n  theta NOT RECORDED in chain_snapshots — cannot measure bleed directly.")
else:
    print(f"\n  theta at entry, {len(thetas)} trades with a snapshot:")
    tv = sorted(x[0] for x in thetas)
    print(f"    |theta| per share per DAY: median {st.median(tv):.2f}")
    bleeds = []
    for th, qty, sec, sym in thetas:
        # a trading day is 6.25h; bleed over the actual hold
        bleeds.append(th * qty * (sec / (6.25*3600)))
    b = sorted(bleeds)
    print(f"    rupee bleed over the ACTUAL hold: median Rs {st.median(b):.0f}  "
          f"p75 Rs {b[3*len(b)//4]:.0f}  max Rs {b[-1]:.0f}")
    print(f"    mean Rs {st.mean(b):.0f} per trade")

    print()
    print("  break-even directional move required, per trade:")
    import sys; sys.path.insert(0,'.')
    import agents, config
    cfg = config.load()
    for sym, spot, lot in (("NIFTY",24650,65),("BANKNIFTY",57800,30),("FINNIFTY",26900,60),("SENSEX",78700,20)):
        cost = agents.realistic_costs("option", sym, 1, spot*0.0042, spot*0.0042, cfg, legs=1)["total"]
        sub = [x for x in thetas if x[3]==sym]
        med_theta = st.median([x[0] for x in sub]) if sub else st.median(tv)
        med_hold = st.median([x[2] for x in sub]) if sub else st.median(holds)
        bleed = med_theta * lot * (med_hold/(6.25*3600))
        pts = (cost + bleed) / (0.526 * lot)
        print(f"    {sym:10} cost Rs {cost:5.0f} + theta Rs {bleed:5.0f} = Rs {cost+bleed:5.0f}"
              f"  -> needs {pts:5.1f} index pts ({100*pts/spot:.3f}% of spot) just to break even")
