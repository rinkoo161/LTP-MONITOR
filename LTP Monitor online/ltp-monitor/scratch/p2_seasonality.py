#!/usr/bin/env python3
"""Part 2 — intraday seasonality on 2 years of index candles."""
import sqlite3, os, math, statistics as st, collections, datetime
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db"))
SID = {"NIFTY": "13", "BANKNIFTY": "25"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

for sym, sid in SID.items():
    rows = c.execute("SELECT ts,o,c FROM candles WHERE security_id=? ORDER BY ts", (sid,)).fetchall()
    buck = collections.defaultdict(list)
    for ts, o, cl in rows:
        if not o or not cl: continue
        t = datetime.datetime.fromtimestamp(ts, IST)
        if not (9 <= t.hour <= 15): continue
        buck[(t.hour, 0 if t.minute < 30 else 30)].append((cl - o) / o * 1e4)   # bps
    print(f"\n  {sym} — mean return by 30-min bucket, basis points, {len(rows):,} bars")
    print(f"    {'bucket':>8} {'n':>7} {'mean bps':>9} {'sd':>7} {'t':>6}")
    for k in sorted(buck):
        v = buck[k]
        if len(v) < 200: continue
        m, sd = st.mean(v), st.pstdev(v)
        t = m / (sd / math.sqrt(len(v))) if sd else 0
        flag = "  <--" if abs(t) > 3 else ""
        print(f"    {k[0]:02d}:{k[1]:02d}    {len(v):7,} {m:+9.3f} {sd:7.2f} {t:+6.2f}{flag}")
