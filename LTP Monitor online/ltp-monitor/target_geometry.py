#!/usr/bin/env python3
"""target_geometry.py — are STRUCTURE targets reachable where ATR/RR ones were not?

2026-08-03. Phase 0 found futures targets geometrically unreachable: the
median trade reached 1.1% of its target, none of 40 reached half, and the
cohort that ran a full session with no interference still only got to
5.5%. Those targets were a MULTIPLE — ATR-derived for futures, and
`rr_target x risk` for every price-action strategy.

The deferred 1H MTF Reversal port would set targets from STRUCTURE
(Fibonacci extension / nearest support-resistance) instead. That is its
single most valuable property, and it does not require porting the
strategy to test.

METHOD — the target definition is the ONLY thing that changes:

  for each replayed trade
    realised MFE  = max favourable excursion, from 1m candles between
                    entry and exit (the thing actually achieved)
    RR target     = rr_target x risk, exactly what the strategy used
    STRUCTURE tgt = nearest prior pivot level beyond entry in the trade's
                    direction, from candles STRICTLY BEFORE entry

then compare MFE against each.

WHY "STRICTLY BEFORE ENTRY" MATTERS. A pivot is only a pivot once
`pivot_k` bars have printed on BOTH sides of it. Confirming pivots with
bars that postdate the entry would let the target be chosen using the
move it is meant to predict — lookahead, and it would make structure
targets look reachable no matter what. Every pivot used here is fully
confirmed before the entry bar.

WHAT THIS CANNOT SETTLE. MFE is measured on 1m bars, so an excursion
inside a bar is invisible and every MFE here is a LOWER bound — which
flatters both target types equally, so the COMPARISON stands even though
the absolute reach rates are conservative.
"""
import argparse
import bisect
import collections
import statistics as st
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt
import config
import history

PIVOT_K = 5          # bars either side that must be lower/higher
LOOKBACK = 240       # bars of prior structure to search (4h of 1m)


def load_candles(symbol):
    """Sorted (ts, high, low) for the symbol's index series."""
    c = history._conn()
    try:
        r = c.execute("SELECT security_id FROM instruments WHERE UPPER(symbol)=? "
                      "LIMIT 1", (symbol.upper(),)).fetchone()
        if not r:
            return [], [], []
        rows = c.execute("SELECT ts,h,l FROM candles WHERE security_id=? "
                         "ORDER BY ts", (r[0],)).fetchall()
    finally:
        c.close()
    return ([x[0] for x in rows], [x[1] for x in rows], [x[2] for x in rows])


def pivots_before(ts_l, hi, lo, idx, k=PIVOT_K, look=LOOKBACK):
    """(highs, lows) confirmed strictly before bar `idx`.

    A pivot at j needs k bars on each side, so it is only confirmed at
    j+k. Requiring j+k < idx is what keeps post-entry bars out.
    """
    start = max(k, idx - look)
    ph, pl = [], []
    for j in range(start, idx - k):
        if j - k < 0:
            continue
        win_h = hi[j - k:j + k + 1]
        win_l = lo[j - k:j + k + 1]
        if hi[j] == max(win_h):
            ph.append(hi[j])
        if lo[j] == min(win_l):
            pl.append(lo[j])
    return ph, pl


def run(symbol, names, days, lot):
    ts_l, hi, lo = load_candles(symbol)
    if not ts_l:
        return []
    out = []
    for name in names:
        try:
            res = bt.replay_pa(symbol, name, days=days)
            trades = res["trades"] if isinstance(res, dict) else res
        except Exception:
            continue
        for t in trades:
            e_ts, x_ts = t.get("entry_ts"), t.get("exit_ts")
            e, x, risk = t.get("entry_spot"), t.get("exit_spot"), t.get("risk")
            if not (e_ts and x_ts and e and x and risk):
                continue
            i0 = bisect.bisect_left(ts_l, e_ts)
            i1 = bisect.bisect_right(ts_l, x_ts)
            if i0 >= i1 or i0 >= len(ts_l):
                continue
            # Direction: the replay stores neither side nor leg, so it is
            # recovered from whether the spot move agrees with the P&L.
            moved_up = x >= e
            won = (t.get("pnl") or 0) > 0
            d = 1 if (moved_up == won) else -1
            risk_pts = abs(risk) / (0.5 * lot)      # replay prices pts x 0.5 x lot
            if risk_pts <= 0:
                continue
            mfe = (max(hi[i0:i1]) - e) if d > 0 else (e - min(lo[i0:i1]))
            rr_tgt = 2.0 * risk_pts                  # rr_target default
            ph, pl = pivots_before(ts_l, hi, lo, i0)
            levels = [p for p in (ph if d > 0 else pl)
                      if (p > e if d > 0 else p < e)]
            struct = (min(levels) - e) if d > 0 and levels else \
                     (e - max(levels)) if d < 0 and levels else None
            # FIB EXTENSION of the prior confirmed swing, which is what
            # the 1H port actually specifies. Nearest-pivot and a Fib
            # projection are opposite errors — one sits inside the noise,
            # the other beyond it — so testing only the near one would
            # misrepresent the port's design.
            fib = None
            if ph and pl:
                swing = (max(ph) - min(pl))
                if swing > 0:
                    fib = 1.272 * swing
            out.append({"strategy": name, "mfe": mfe, "rr": rr_tgt,
                        "struct": struct, "fib": fib, "risk_pts": risk_pts,
                        "mfe_R": mfe / risk_pts})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    a = ap.parse_args()
    cfg = config.load()
    NAMES = ("orb", "vwap_pullback", "momentum_confluence", "ema_mtf")
    rows = []
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        lot = (cfg.get("lot_sizes") or {}).get(sym, 65)
        rows += run(sym, NAMES, history.index_days(sym, a.days), lot)
    if not rows:
        sys.exit("no trades with reconstructable structure")

    withs = [r for r in rows if r["struct"] and r["struct"] > 0]
    print(f"\n  {len(rows)} trades; {len(withs)} had a confirmed prior pivot "
          f"beyond entry\n")

    def reach(rs, key):
        f = [min(r["mfe"] / r[key], 4) for r in rs if r.get(key)]
        hit = sum(1 for r in rs if r.get(key) and r["mfe"] >= r[key])
        half = sum(1 for r in rs if r.get(key) and r["mfe"] >= 0.5 * r[key])
        return f, hit, half

    print(f"  {'target type':16} {'median dist':>12} {'MFE/target':>22} "
          f"{'reached':>9} {'half':>7}")
    for lab, key in (("RR (rr x risk)", "rr"), ("STRUCTURE (pivot)", "struct"),
                     ("FIB 1.272 ext", "fib")):
        f, hit, half = reach(withs, key)
        d = [r[key] for r in withs if r.get(key)]
        print(f"  {lab:16} {st.median(d):>10.0f}pt  "
              f"median {100*st.median(f):>5.1f}%  mean {100*st.mean(f):>5.1f}%  "
              f"{hit:>4}/{len(f):<4} {half:>4}/{len(f)}")

    print(f"\n  per strategy (median MFE as a share of each target)")
    print(f"    {'strategy':22} {'n':>4} {'RR':>8} {'STRUCT':>8}  {'struct/RR dist':>14}")
    for nm in sorted({r["strategy"] for r in withs}):
        g = [r for r in withs if r["strategy"] == nm]
        fr, _, _ = reach(g, "rr")
        fs, _, _ = reach(g, "struct")
        dr = st.median([r["rr"] for r in g])
        ds = st.median([r["struct"] for r in g])
        print(f"    {nm:22} {len(g):>4} {100*st.median(fr):>7.1f}% "
              f"{100*st.median(fs):>7.1f}%  {ds/dr:>13.2f}x")
    # THE ACTUALLY USEFUL NUMBER. Both target types are just distances;
    # what the data determines is how far price actually travels in
    # favour, expressed in units of the trade's OWN risk. That is
    # comparable across strategies and symbols and is what a target
    # should be set from.
    R = sorted(r["mfe_R"] for r in rows)
    print(f"\n  HOW FAR PRICE ACTUALLY GOES, in R (n={len(R)})")
    for q in (0.25, 0.50, 0.75, 0.90):
        print(f"    {int(100*(1-q)):>3}% of trades reach at least "
              f"{R[int((1-q)*len(R))]:>5.2f}R")
    print(f"    median MFE = {st.median(R):.2f}R   mean = {st.mean(R):.2f}R")
    for tgt in (0.5, 1.0, 1.5, 2.0, 2.5):
        hit = 100*sum(1 for x in R if x >= tgt)/len(R)
        print(f"    a {tgt:.1f}R target would be reached by {hit:>5.1f}% of trades")

    print("\n  MFE is measured on 1m bars, so it is a LOWER bound for BOTH")
    print("  target types equally — the comparison holds, the absolute")
    print("  reach rates are conservative.")


if __name__ == "__main__":
    main()
