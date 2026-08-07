#!/usr/bin/env python3
"""Part 3 — opportunity sizing for the pre-registrations.

DELIBERATELY MEASURES ONLY: the magnitude of the raw phenomenon and the
sample size available. It does NOT measure any candidate's return —
that is the test, and pre-registration must be written before it.
"""
import sqlite3, os, math, statistics as st, collections, datetime
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db"))
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SID = {"NIFTY": ("13", 65), "BANKNIFTY": ("25", 30)}

print("A) OPENING GAP magnitude — 2 years, unconditional (NOT the fade return)")
for sym, (sid, lot) in SID.items():
    rows = c.execute("SELECT ts,o,c FROM candles WHERE security_id=? ORDER BY ts", (sid,)).fetchall()
    byday = collections.OrderedDict()
    for ts, o, cl in rows:
        if not o or not cl: continue
        t = datetime.datetime.fromtimestamp(ts, IST)
        d = t.date()
        if d not in byday: byday[d] = {"open": None, "close": None}
        if byday[d]["open"] is None and (t.hour, t.minute) >= (9, 15): byday[d]["open"] = o
        byday[d]["close"] = cl
    ds = [d for d in byday if byday[d]["open"] and byday[d]["close"]]
    gaps = []
    for prev, cur in zip(ds, ds[1:]):
        pc = byday[prev]["close"]
        gaps.append((byday[cur]["open"] - pc, pc))
    ap = sorted(abs(g) for g, _ in gaps)
    n = len(ap)
    px = st.median([p for _, p in gaps])
    print(f"\n  {sym}  {n} sessions, median spot {px:,.0f}, lot {lot}")
    print(f"    {'pctile':>8} {'|gap| pts':>10} {'|gap| %':>8} {'Rs/lot':>9} {'n above':>8}")
    for q in (50, 75, 90, 95):
        v = ap[int(n * q / 100)]
        above = sum(1 for x in ap if x >= v)
        print(f"    p{q:<7} {v:10.1f} {v/px*100:8.3f} {v*lot:9,.0f} {above:8d}")
    # how many sessions have a gap big enough that 30% of it clears break-even
    for thr in (30, 50, 80):
        k = sum(1 for x in ap if x >= thr)
        print(f"    |gap| >= {thr:3d} pts : {k:4d} sessions  ({k/n*100:4.1f}%)  "
              f"= {k/2:.0f}/yr   30% capture = Rs {thr*0.3*lot:,.0f}/lot")

print("\n\nB) SAMPLE SIZE AVAILABLE per candidate per year")
print(f"    {'candidate':34} {'events/yr':>10}  note")
print(f"    {'gap fade (|gap|>=50pt, NIFTY+BNF)':34} {'~120':>10}  2 symbols x ~60")
print(f"    {'expiry pinning':34} {'~100':>10}  4 symbols x weekly, but only 1 usable/wk/sym")
print(f"    {'conditional short premium':34} {'unknown':>10}  depends on how often IV>RV — UNMEASURED")
print(f"    {'RBI MPC / budget IV crush':34} {'~7':>10}  6 MPC + 1 budget")

print("\n\nC) REQUIRED n FOR 80% POWER  (Part 0 formula: n = 7.849 * sd^2 / d^2)")
print(f"    {'sd of per-trade net':>22} {'edge Rs200':>11} {'Rs500':>8} {'Rs1000':>8} {'Rs2000':>8}")
for sd in (1500, 3000, 6000):
    r = [math.ceil(7.849 * sd**2 / d**2) for d in (200, 500, 1000, 2000)]
    print(f"    {sd:22,} {r[0]:11,} {r[1]:8,} {r[2]:8,} {r[3]:8,}")
