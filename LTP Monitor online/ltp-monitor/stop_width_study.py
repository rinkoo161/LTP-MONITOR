#!/usr/bin/env python3
"""stop_width_study.py — what would a narrower option stop have done?

2026-08-02. The option stop is clamped to 10-60% of premium
(`agents.py`: `min(max(sl_pts_premium, entry*0.10), entry*0.60)`), and
the realised median is ~15% of premium — 69 option points. That width is
why one lot risks a median ₹3,198 against a ₹5,000 portfolio cap, and
why the per-trade cap has to sit at ₹5,000 rather than the futures
₹2,500.

Narrowing the stop is the only lever that reduces per-trade risk without
refusing trades. But it is NOT free: a tighter stop converts trades that
drew down and recovered into losses. Whether it helps is an empirical
question about how deep winners dig before they work, and this codebase
happens to have the data — `p["mae"]` is tracked per option position
(agents.py ~5183) and 370 journal records carry it.

METHOD. For each trade with entry, qty and MAE:

    mae_per_share = |mae| / qty          worst adverse excursion, points
    stopped(X)    = mae_per_share >= entry * X
    pnl(X)        = -X * entry * qty     if stopped
                    actual pnl           otherwise

ASSUMPTIONS, stated because they flatter the tighter stop:
  - the stop fills exactly at its price: no slippage, no gap-through.
    Real fills are worse, and worse by MORE on a tighter stop, since it
    sits closer to the noise.
  - MAE is sampled on the monitor cadence, so a spike between samples is
    invisible; true MAE is deeper, meaning tighter stops are hit MORE
    often than this shows.
  - a trade stopped early cannot then recover — correct — but it also
    frees capital, which this ignores.

So treat the tight-stop columns as an UPPER bound on their benefit.
"""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIDTHS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.60]


def load():
    """SINGLE-LEG option buys only.

    2026-08-02 — the first version of this took every `trades_detail`
    row, which mixes in SPREAD legs. On a sold spread leg `stoploss` is
    not a price below entry at all: 385 of 500 rows carry a stoploss
    <= 0, median -32, which is meaningless for a long option and made
    the pooled "median stop = 200% of premium" figure nonsense. Reading
    the SHAPE of a field instead of its MEANING — the exact failure this
    codebase keeps hitting. Filtered here, deliberately, to the
    population the question is about.
    """
    p = os.path.join(os.path.expanduser("~"), ".ltp-monitor", "journal.json")
    det = []
    for d in json.load(open(p)):
        det.extend(d.get("trades_detail") or [])
    out = []
    for t in det:
        e, q, mae, pnl = (t.get("entry"), t.get("qty"), t.get("mae"),
                          t.get("pnl"))
        if not e or not q or mae is None or pnl is None:
            continue
        if "spread" in (t.get("reason") or "").lower():
            continue                      # spread leg, not an option buy
        sl = t.get("stoploss")
        if sl is None or not (0 < sl < e):
            continue                      # no reachable stop -> not this study
        out.append({"entry": e, "qty": q, "mae_ps": abs(mae) / q,
                    "pnl": pnl, "sl": t.get("stoploss"),
                    "symbol": t.get("symbol"), "reason": t.get("reason") or ""})
    return out


def simulate(rows, x):
    tot = 0.0
    stopped = wins_killed = 0
    for r in rows:
        if r["mae_ps"] >= r["entry"] * x:
            tot += -x * r["entry"] * r["qty"]
            stopped += 1
            if r["pnl"] > 0:
                wins_killed += 1
        else:
            tot += r["pnl"]
    return tot, stopped, wins_killed


def main():
    rows = load()
    if not rows:
        sys.exit("no trades carry MAE — nothing to simulate")
    base = sum(r["pnl"] for r in rows)
    cur = [r["mae_ps"] / r["entry"] for r in rows]
    print(f"\n  {len(rows)} option trades with MAE recorded")
    print(f"  actual net P&L: ₹{base:,.0f}")
    print(f"  realised stop width: median {100*st.median([abs(r['entry']-r['sl'])/r['entry'] for r in rows if r['sl']]):.0f}% of premium")
    print(f"  drawdown reached: median {100*st.median(cur):.0f}% of premium, "
          f"p90 {100*sorted(cur)[int(.9*len(cur))]:.0f}%\n")

    print(f"  {'stop':>6} {'net P&L':>11} {'vs actual':>11} {'stopped':>9} "
          f"{'winners killed':>15} {'risk/lot ₹':>11}")
    lot = 65
    for x in WIDTHS:
        tot, n, wk = simulate(rows, x)
        med_entry = st.median([r["entry"] for r in rows])
        risk = x * med_entry * lot
        print(f"  {100*x:>4.0f}%  ₹{tot:>10,.0f} {tot-base:>+11,.0f} "
              f"{n:>4}/{len(rows):<4} {wk:>15} {risk:>11,.0f}")

    print("\n  UPPER BOUND on the tight-stop case: fills are assumed exact and")
    print("  MAE is sampled, so tighter stops are hit MORE often in reality.")


if __name__ == "__main__":
    main()
