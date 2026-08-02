#!/usr/bin/env python3
"""entry_quality.py — what separates an entry that travels from one that doesn't?

2026-08-03, following the target-geometry result: the achievable target
is ~1R while the strategies target 2R, so the binding constraint is that
ENTRIES do not produce 2R of favourable travel. This asks whether any
pre-specified condition identifies the entries that do.

TWO METHOD DECISIONS THAT DO THE REAL WORK.

1. ENTRY QUALITY IS MEASURED OVER A FIXED HORIZON, NOT TO THE EXIT.
   `target_geometry.py` measured MFE between entry and exit, which
   confounds entry quality with exit timing: a strategy that exits early
   records a small MFE even from a good entry. Here MFE is measured over
   a FIXED window after entry (`HORIZON` bars), identical for every
   trade, so the number describes the ENTRY alone.

2. THE HYPOTHESES ARE PRE-SPECIFIED AND FEW.
   With 6,899 trades, sweeping conditions until one separates would find
   several that mean nothing. The five below were written down before
   any result was looked at, each with a stated reason, and each is
   tested on a CHRONOLOGICAL out-of-sample split — earliest 60% to find
   an effect, latest 40% to see whether it survives. An effect that
   appears only in-sample is reported as failed, not as a finding.

   A. TREND ALIGNMENT — does the entry agree with the higher-timeframe
      trend (price vs EMA200 on 1m)? The most standard entry filter
      there is; if it does nothing here that is itself informative.
   B. TIME OF DAY — first hour vs rest. ORB and momentum strategies are
      open-driven; follow-through is usually concentrated early.
   C. VOLATILITY — ATR at entry vs its own median. A momentum entry in a
      dead tape has less room to travel regardless of direction.
   D. EXTENSION — distance from the session VWAP in ATR units. Entering
      far from VWAP is chasing, and mean reversion works against it.
   E. HEADROOM — distance to the nearest prior pivot in the trade's
      direction, in R. Directly implied by the target-geometry result:
      an entry with a structural ceiling 0.4R away cannot travel 2R no
      matter how good the signal is. This is the hypothesis the previous
      test actually motivates.

NOTHING HERE CHANGES LIVE BEHAVIOUR. It reports.
"""
import argparse
import bisect
import statistics as st
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt
import config
import history

HORIZON = 60         # bars after entry over which MFE is measured
PIVOT_K = 5
LOOKBACK = 240
IS_FRAC = 0.60       # chronological in-sample share


def series(symbol):
    c = history._conn()
    try:
        r = c.execute("SELECT security_id FROM instruments WHERE UPPER(symbol)=?"
                      " LIMIT 1", (symbol.upper(),)).fetchone()
        if not r:
            return None
        rows = c.execute("SELECT ts,h,l,c,v FROM candles WHERE security_id=? "
                         "ORDER BY ts", (r[0],)).fetchall()
    finally:
        c.close()
    if not rows:
        return None
    ts = [x[0] for x in rows]; hi = [x[1] for x in rows]
    lo = [x[2] for x in rows]; cl = [x[3] for x in rows]
    # EMA200 and a rolling ATR14, computed once per symbol
    ema, atr = [None]*len(cl), [None]*len(cl)
    k = 2/(200+1); e = cl[0]
    prev = cl[0]; trs = []
    for i in range(len(cl)):
        e = cl[i]*k + e*(1-k); ema[i] = e
        tr = max(hi[i]-lo[i], abs(hi[i]-prev), abs(lo[i]-prev)); prev = cl[i]
        trs.append(tr)
        if i >= 14:
            trs.pop(0)
            atr[i] = sum(trs)/len(trs)
    return ts, hi, lo, cl, ema, atr


def pivots_before(hi, lo, idx, k=PIVOT_K, look=LOOKBACK):
    ph, pl = [], []
    for j in range(max(k, idx-look), idx-k):
        if hi[j] == max(hi[j-k:j+k+1]): ph.append(hi[j])
        if lo[j] == min(lo[j-k:j+k+1]): pl.append(lo[j])
    return ph, pl


def collect(symbol, names, days, lot):
    s = series(symbol)
    if not s: return []
    ts, hi, lo, cl, ema, atr = s
    out = []
    for name in names:
        try:
            res = bt.replay_pa(symbol, name, days=days)
            trades = res["trades"] if isinstance(res, dict) else res
        except Exception:
            continue
        for t in trades:
            e_ts, e, risk = t.get("entry_ts"), t.get("entry_spot"), t.get("risk")
            if not (e_ts and e and risk): continue
            i0 = bisect.bisect_left(ts, e_ts)
            if i0 < LOOKBACK or i0 + HORIZON >= len(ts): continue
            if atr[i0] is None or not atr[i0]: continue
            d = 1 if ((t.get("exit_spot", e) >= e) == ((t.get("pnl") or 0) > 0)) else -1
            R = abs(risk)/(0.5*lot)
            if R <= 0: continue
            w = slice(i0, i0+HORIZON)
            mfe = (max(hi[w]) - e) if d > 0 else (e - min(lo[w]))
            ph, pl = pivots_before(hi, lo, i0)
            lv = [p for p in (ph if d > 0 else pl) if (p > e if d > 0 else p < e)]
            head = ((min(lv)-e) if d > 0 else (e-max(lv)))/R if lv else None
            # session VWAP proxy: mean close since the day's first bar
            j = i0
            day0 = ts[i0] - (ts[i0] % 86400)
            while j > 0 and ts[j-1] >= day0: j -= 1
            vwap = sum(cl[j:i0+1])/max(1, i0-j+1)
            hour = ((ts[i0] + 19800) % 86400)/3600.0      # IST hour
            out.append({"ts": e_ts, "strategy": name, "mfe_R": mfe/R,
                        "A_trend": (d > 0) == (cl[i0] > ema[i0]),
                        "B_early": hour < 10.5,
                        "C_vol": atr[i0],
                        "D_ext": abs(e - vwap)/atr[i0],
                        "E_head": head})
    return out


def split_test(rows, label, pred, key=None):
    """Median MFE/R for pred-true vs pred-false, in-sample then out."""
    rows = sorted(rows, key=lambda r: r["ts"])
    n = int(len(rows)*IS_FRAC)
    res = []
    for tag, part in (("IS", rows[:n]), ("OOS", rows[n:])):
        yes = [r["mfe_R"] for r in part if pred(r) is True]
        no  = [r["mfe_R"] for r in part if pred(r) is False]
        if len(yes) < 30 or len(no) < 30:
            res.append((tag, None, None, len(yes), len(no))); continue
        res.append((tag, st.median(yes), st.median(no), len(yes), len(no)))
    return label, res


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=250)
    a = ap.parse_args()
    cfg = config.load()
    NAMES = ("orb", "vwap_pullback", "momentum_confluence", "ema_mtf")
    rows = []
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        lot = (cfg.get("lot_sizes") or {}).get(sym, 65)
        rows += collect(sym, NAMES, history.index_days(sym, a.days), lot)
    if not rows: sys.exit("no usable trades")
    allR = sorted(r["mfe_R"] for r in rows)
    print(f"\n  {len(rows)} entries, MFE measured over a FIXED {HORIZON}-bar "
          f"window (not to exit)")
    print(f"  baseline: median {st.median(allR):.2f}R   "
          f"reach 1R {100*sum(1 for x in allR if x>=1)/len(allR):.1f}%   "
          f"reach 2R {100*sum(1 for x in allR if x>=2)/len(allR):.1f}%")
    vol_med = st.median([r["C_vol"] for r in rows])
    ext_med = st.median([r["D_ext"] for r in rows])
    tests = [
        split_test(rows, "A trend-aligned", lambda r: r["A_trend"]),
        split_test(rows, "B first hour",    lambda r: r["B_early"]),
        split_test(rows, "C ATR above med", lambda r: r["C_vol"] > vol_med),
        split_test(rows, "D near VWAP",     lambda r: r["D_ext"] < ext_med),
        split_test(rows, "E headroom >=2R",
                   lambda r: None if r["E_head"] is None else r["E_head"] >= 2.0),
    ]
    print(f"\n  {'hypothesis':20} {'sample':>6} {'cond TRUE':>11} {'cond FALSE':>11} "
          f"{'lift':>8}  n(T/F)")
    for label, res in tests:
        for tag, y, n_, ny, nn in res:
            if y is None:
                print(f"  {label:20} {tag:>6} {'thin':>11} {'':>11} {'':>8}  {ny}/{nn}")
            else:
                print(f"  {label:20} {tag:>6} {y:>10.2f}R {n_:>10.2f}R "
                      f"{y-n_:>+7.2f}R  {ny}/{nn}")
    print("\n  A hypothesis only counts if the lift survives OOS with the same sign.")


if __name__ == "__main__":
    main()
