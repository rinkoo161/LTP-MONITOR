#!/usr/bin/env python3
"""Candidate C — opening-gap fade. PRE-REGISTERED, Part 3 of the memo.

Frozen entry rule (no parameter is chosen here):
  09:45 IST. |open(09:15) - prior close| >= 80 NIFTY / 160 BANKNIFTY,
  AND spot at 09:45 has not retraced more than 50% of the gap.
Exit: 15:15 IST.

Measures the DIRECTIONAL component only, in index points, because
pricing the spread historically would require an IV series we do not
have (daily_atm_iv starts 2026-07). Break-even from the pre-registration
(net delta ~0.30, 1 lot): 13.0 NIFTY pts, 28.1 BANKNIFTY pts.

A credit spread also earns theta and wins on a stall, so this
UNDERSTATES the structure. It is a floor, not the whole result.

HOLDOUT (last 20%, 2026-03-16 onward) IS EXCLUDED. Touched once, later.
"""
import sqlite3, os, math, statistics as st, datetime, collections, sys
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db"))
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SPEC = {"NIFTY": ("13", 80.0, 13.0, 65), "BANKNIFTY": ("25", 160.0, 28.1, 30)}
HOLDOUT_FROM = "2026-03-16"
USE_HOLDOUT = "--holdout" in sys.argv

for sym, (sid, gap_min, breakeven, lot) in SPEC.items():
    rows = c.execute("SELECT ts,o,c FROM candles WHERE security_id=? ORDER BY ts", (sid,)).fetchall()
    day = collections.OrderedDict()
    for ts, o, cl in rows:
        if not o or not cl: continue
        t = datetime.datetime.fromtimestamp(ts, IST)
        d = t.date().isoformat()
        hm = t.hour * 60 + t.minute
        rec = day.setdefault(d, {})
        if hm >= 555 and "open" not in rec: rec["open"] = o          # 09:15
        if hm >= 585 and "s0945" not in rec: rec["s0945"] = cl        # 09:45
        if hm <= 915: rec["close"] = cl                               # last <= 15:15
    ds = [d for d in day if {"open","s0945","close"} <= set(day[d])]
    ds.sort()
    caps, skipped = [], collections.Counter()
    for prev, cur in zip(ds, ds[1:]):
        in_holdout = cur >= HOLDOUT_FROM
        if in_holdout != USE_HOLDOUT:
            skipped["holdout" if in_holdout else "train"] += 1
            continue
        pc = day[prev]["close"]; op = day[cur]["open"]
        s45 = day[cur]["s0945"]; s15 = day[cur]["close"]
        gap = op - pc
        if abs(gap) < gap_min: skipped["gap too small"] += 1; continue
        retrace = (op - s45) / gap                    # >0 means moved back toward pc
        if retrace > 0.50: skipped["already retraced >50%"] += 1; continue
        # fade = movement AGAINST the gap direction, 09:45 -> 15:15
        caps.append(-(s15 - s45) if gap > 0 else (s15 - s45))
    n = len(caps)
    label = "HOLDOUT" if USE_HOLDOUT else "TRAIN (pre-2026-03-16)"
    print(f"\n  {sym}  [{label}]  qualifying sessions: {n}")
    for k, v in skipped.most_common(): print(f"      skipped {k:24} {v}")
    if n < 2: continue
    m, sd = st.mean(caps), st.stdev(caps)
    t = m / (sd / math.sqrt(n))
    wins = sum(1 for x in caps if x > breakeven)
    print(f"      mean fade  {m:+8.2f} pts   sd {sd:7.2f}   t {t:+6.2f}")
    print(f"      break-even {breakeven:8.2f} pts  -> mean is "
          f"{'ABOVE' if m > breakeven else 'BELOW'} it by {abs(m-breakeven):.2f}")
    print(f"      sessions clearing break-even: {wins}/{n} ({wins/n*100:.0f}%)")
    print(f"      mean net in rupees at 1 lot: Rs {(m-breakeven)*lot*0.30:+,.0f} "
          f"(0.30 delta approximation)")

# ---- appended: the two remaining pre-registered checks
print("\n\n  POWER AND SIGN STABILITY (Part 4: n = 7.849 * sd^2 / delta^2)")
for sym, (sid, gap_min, breakeven, lot) in SPEC.items():
    rows = c.execute("SELECT ts,o,c FROM candles WHERE security_id=? ORDER BY ts", (sid,)).fetchall()
    day = collections.OrderedDict()
    for ts, o, cl in rows:
        if not o or not cl: continue
        t = datetime.datetime.fromtimestamp(ts, IST)
        d = t.date().isoformat(); hm = t.hour*60 + t.minute
        rec = day.setdefault(d, {})
        if hm >= 555 and "open" not in rec: rec["open"] = o
        if hm >= 585 and "s0945" not in rec: rec["s0945"] = cl
        if hm <= 915: rec["close"] = cl
    ds = sorted(d for d in day if {"open","s0945","close"} <= set(day[d]))
    caps = []
    for prev, cur in zip(ds, ds[1:]):
        if cur >= HOLDOUT_FROM: continue
        pc, op = day[prev]["close"], day[cur]["open"]
        s45, s15 = day[cur]["s0945"], day[cur]["close"]
        gap = op - pc
        if abs(gap) < gap_min: continue
        if (op - s45)/gap > 0.50: continue
        caps.append((cur, -(s15-s45) if gap > 0 else (s15-s45)))
    sd = st.stdev([x for _, x in caps])
    need = math.ceil(7.849 * sd**2 / breakeven**2)
    per_yr = len(caps)/2.0
    print(f"\n  {sym}: sd {sd:.1f} pts, break-even {breakeven} pts")
    print(f"    n needed to detect an edge the SIZE of break-even: {need:,}")
    print(f"    qualifying sessions available: {len(caps)/2:.0f}/yr "
          f"-> {need/per_yr:.0f} YEARS to reach it")
    half = len(caps)//2
    a = [x for _, x in caps[:half]]; b = [x for _, x in caps[half:]]
    print(f"    first half  mean {st.mean(a):+7.2f} (n={len(a)}, to {caps[half-1][0]})")
    print(f"    second half mean {st.mean(b):+7.2f} (n={len(b)})")
    print(f"    sign flips between halves: "
          f"{'YES — pre-registered kill clause 3' if st.mean(a)*st.mean(b) < 0 else 'no'}")
