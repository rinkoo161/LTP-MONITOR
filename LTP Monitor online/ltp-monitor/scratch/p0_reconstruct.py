#!/usr/bin/env python3
"""Part 0.3 — can stored P&L be independently reconstructed?

For each closed trade, look for a chain snapshot at its entry and exit
time. Where both exist, recompute gross from the recorded premiums and
diff against the stored gross_pnl. Where they do not, that trade is
UNVERIFIABLE and is counted as such rather than skipped.
"""
import json, os, sqlite3, datetime, collections, statistics as st
DB = os.path.expanduser("~/.ltp-monitor/history.db")
rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
c = sqlite3.connect(DB)

cov = c.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM chain_snapshots").fetchone()
print(f"  chain_snapshots: {cov[2]:,} rows, "
      f"{datetime.datetime.fromtimestamp(cov[0]):%Y-%m-%d} .. {datetime.datetime.fromtimestamp(cov[1]):%Y-%m-%d}")
days = [r[0] for r in c.execute(
    "SELECT DISTINCT date(ts,'unixepoch','+5 hours','+30 minutes') FROM chain_snapshots ORDER BY 1")]
print(f"  distinct snapshot days: {len(days)} -> {days}\n")

trade_days = collections.Counter(str(t.get("closed_date")) for t in rows)
verifiable = sum(n for d, n in trade_days.items() if d in set(days))
print(f"  {len(rows)} closed trades across {len(trade_days)} days")
print(f"  {verifiable} ({100*verifiable/len(rows):.0f}%) fall on a day where snapshots still exist")
print(f"  {len(rows)-verifiable} ({100*(len(rows)-verifiable)/len(rows):.0f}%) are UNVERIFIABLE — snapshots pruned\n")

def snap(sym, strike, leg, ts):
    r = c.execute("SELECT ltp FROM chain_snapshots WHERE symbol=? AND strike=? AND leg=? "
                  "AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                  (sym, strike, leg, ts-180, ts+180, ts)).fetchone()
    return r[0] if r else None

errs, checked, nomatch = [], 0, 0
for t in rows:
    if t.get("leg") not in ("CE", "PE"): continue
    d = str(t.get("closed_date"))
    if d not in set(days): continue
    try:
        o = datetime.datetime.strptime(f"{d} {t['opened']}", "%Y-%m-%d %H:%M:%S").timestamp()
        x = datetime.datetime.strptime(f"{d} {t['closed']}", "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception: continue
    pi = snap(t["symbol"], t.get("strike"), (t.get("leg") or "").lower(), int(o))
    po = snap(t["symbol"], t.get("strike"), (t.get("leg") or "").lower(), int(x))
    if pi is None or po is None:
        nomatch += 1; continue
    recomputed = (po - pi) * (t.get("qty") or 0)
    stored = t.get("gross_pnl")
    if stored is None: continue
    errs.append(recomputed - stored); checked += 1

print(f"  option trades with BOTH entry and exit snapshots: {checked}")
print(f"  option trades on a snapshot day but with no matching strike/leg row: {nomatch}")
if errs:
    a = sorted(errs)
    print(f"  reconstruction error (recomputed - stored gross), rupees:")
    print(f"    n {len(a)}  mean {st.mean(a):+.0f}  median {st.median(a):+.0f}  "
          f"sd {st.pstdev(a):.0f}")
    print(f"    min {a[0]:+.0f}  p10 {a[int(.1*len(a))]:+.0f}  p90 {a[int(.9*len(a))]:+.0f}  max {a[-1]:+.0f}")
    print(f"    within +-Rs 50: {100*sum(1 for e in a if abs(e)<=50)/len(a):.0f}%")
else:
    print("  NO trade could be independently reconstructed.")
