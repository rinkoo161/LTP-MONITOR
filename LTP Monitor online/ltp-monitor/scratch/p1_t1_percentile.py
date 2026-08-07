#!/usr/bin/env python3
"""Part 1.3b — what PERCENTILE of the empirical move distribution does
T1 sit at? Two measures:

  move-to-close : what the brief asks for
  max favourable: the largest move IN THE TRADE'S DIRECTION at any point
                  before close — the fairer test, since a target can be
                  touched and given back
"""
import json, os, sqlite3, datetime, statistics as st
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db"))
rows = [json.loads(l) for l in open(os.path.expanduser("~/.ltp-monitor/trades.jsonl")) if l.strip()]
opts = [t for t in rows if t.get("leg") in ("CE", "PE")]
sid = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "SENSEX": "51"}
DELTA, PREM_FRAC = 0.526, 0.0042      # both MEASURED in p1_geometry.py

close_m, mfe_m = {}, {}
for t in opts:
    s, d = t["symbol"], str(t.get("closed_date"))
    if s not in sid: continue
    try:
        ts = int(datetime.datetime.strptime(f"{d} {t['opened']}", "%Y-%m-%d %H:%M:%S").timestamp())
        end = int(datetime.datetime.strptime(f"{d} 15:15:00", "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception: continue
    bars = c.execute("SELECT c,h,l FROM candles WHERE security_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                     (sid[s], ts, end)).fetchall()
    if len(bars) < 2: continue
    entry = bars[0][0]
    if not entry: continue
    up = t.get("leg") == "CE"
    close_m.setdefault(s, []).append(abs(bars[-1][0] - entry))
    fav = max((b[1] - entry) if up else (entry - b[2]) for b in bars)
    mfe_m.setdefault(s, []).append(max(0.0, fav))

def pct_at(v, x):
    v = sorted(v)
    return 100.0 * sum(1 for y in v if y < x) / len(v)

print(f"  T1 = +30% premium / delta {DELTA} ; premium = {100*PREM_FRAC:.2f}% of spot")
print(f"  {'sym':10} {'spot':>7} {'T1 needs':>9} {'n':>4} {'med close':>10} {'%ile(close)':>12} {'med MFE':>9} {'%ile(MFE)':>10}")
for s, spot in (("NIFTY", 24650), ("BANKNIFTY", 57800), ("FINNIFTY", 26900), ("SENSEX", 78700)):
    cv, mv = close_m.get(s, []), mfe_m.get(s, [])
    if len(cv) < 3: continue
    need = spot * PREM_FRAC * 0.30 / DELTA
    print(f"  {s:10} {spot:7d} {need:9.0f} {len(cv):4d} {st.median(cv):10.0f} "
          f"{pct_at(cv, need):11.0f}% {st.median(mv):9.0f} {pct_at(mv, need):9.0f}%")
print()
print("  reading: '%ile' is the share of actual entries whose move was SMALLER")
print("  than T1 requires. 90% means only 1 entry in 10 ever moved far enough.")
