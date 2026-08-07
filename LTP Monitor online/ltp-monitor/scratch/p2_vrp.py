#!/usr/bin/env python3
"""Part 2 — volatility risk premium: is implied consistently above
realised, for OUR symbols, on the data we actually hold?

IV: ATM implied from chain_snapshots (the only IV we have; daily_atm_iv
    is EMPTY — the backfill was never run).
RV: close-to-close realised vol from index candles, annualised.
"""
import sqlite3, os, math, statistics as st, datetime, collections
c = sqlite3.connect(os.path.expanduser("~/.ltp-monitor/history.db")); c.row_factory = sqlite3.Row
SID = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "SENSEX": "51"}

# --- ATM IV per symbol-day, from the strike nearest to put-call parity spot
rows = c.execute("""
  SELECT symbol, date(ts,'unixepoch','+5 hours','+30 minutes') d, strike, leg, ltp, iv
  FROM chain_snapshots WHERE iv IS NOT NULL AND iv > 0""").fetchall()
byday = collections.defaultdict(lambda: collections.defaultdict(dict))
for r in rows:
    byday[(r["symbol"], r["d"])][r["strike"]][r["leg"]] = (r["ltp"], r["iv"])

atm_iv = {}
for key, strikes in byday.items():
    cand = []
    for k, legs in strikes.items():
        if "ce" in legs and "pe" in legs and legs["ce"][0] and legs["pe"][0]:
            cand.append((abs(legs["ce"][0] - legs["pe"][0]), k, legs))
    if not cand: continue
    _, k, legs = min(cand)
    ivs = [legs[l][1] for l in ("ce", "pe") if legs[l][1]]
    if ivs: atm_iv[key] = st.mean(ivs)

# --- realised vol from daily closes
def realised(sym, days=None):
    r = c.execute("""SELECT date(ts,'unixepoch','+5 hours','+30 minutes') d, c
                     FROM candles WHERE security_id=? ORDER BY ts""", (SID[sym],)).fetchall()
    lastclose = {}
    for x in r: lastclose[x["d"]] = x["c"]
    ds = sorted(lastclose)
    if days: ds = [d for d in ds if d in days]
    rets = []
    for a, b in zip(ds, ds[1:]):
        if lastclose[a] and lastclose[b]:
            rets.append(math.log(lastclose[b] / lastclose[a]))
    if len(rets) < 3: return None, 0
    return st.pstdev(rets) * math.sqrt(252) * 100, len(rets)

print("  VOLATILITY RISK PREMIUM — implied vs realised\n")
print(f"  {'symbol':10} {'ATM IV %':>9} {'days':>5} {'RV(same days) %':>16} {'RV(2yr) %':>10} {'VRP vs 2yr':>11}")
for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
    ivs = {d: v for (s, d), v in atm_iv.items() if s == sym}
    if not ivs: 
        print(f"  {sym:10}   no IV rows"); continue
    miv = st.mean(ivs.values())
    rv_same, n_same = realised(sym, set(ivs))
    rv_all, n_all = realised(sym)
    vrp = miv - rv_all if rv_all else float("nan")
    print(f"  {sym:10} {miv:9.1f} {len(ivs):5d} "
          f"{(f'{rv_same:.1f} (n={n_same})' if rv_same else 'n/a'):>16} "
          f"{rv_all:10.1f} {vrp:+11.1f}")

print()
print("  NOTE: IV sample is 9 trading days (chain_snapshots retention).")
print("  RV(2yr) uses 2024-06-20..2026-08-07 daily closes. daily_atm_iv is EMPTY,")
print("  so a like-for-like multi-year IV series does not exist in this system.")
