#!/usr/bin/env python3
"""size_aware_cost.py — does the 1-lot assumption hold?

v59.0 item 29. Every replay prices ONE lot: `pts x 0.5 x lot - fee`,
where `lot` is the contract size, and `fee = fee_per_lot * 2` with no
multiplier for position size at all. The promotion gate then compares
that per-trade figure against a cost bias also computed at one lot.

Live does not trade one lot. `lots_per_trade` is 5, and the journal
shows 30% of executed trades above one lot (mean 1.82, max 10).

WHY THIS IS NOT SELF-CANCELLING. Most of the trade scales linearly with
size — P&L, brokerage, STT, exchange fees — so the ratio of cost to
edge is unchanged by size for those, and a naive reading says "both
scale, no problem". The bid-ask does NOT scale linearly. The measured
distribution (median 0.65, mean 2.27, max 15.80 points) is the signature
of thin depth AT THE TOUCH: a 3-lot order does not pay 3x the 1-lot
spread, it walks further into the book and pays more than 3x. So the
per-lot cost RISES with size while the per-lot edge does not.

Modelled as an impact exponent on the spread component only:

    effective_halfspread(n) = halfspread_1 * n^alpha

    alpha = 0.0   no impact — the lower bound, and what the flat model
                  implicitly assumes
    alpha = 0.5   square-root impact, the standard empirical form
    alpha = 1.0   the order walks the book proportionally — an upper
                  bound, deliberately pessimistic

Nothing here picks a true alpha. The point is the BAND: how much the
per-lot cost bias moves across a defensible range, so the gate can be
re-run against the pessimistic end rather than the convenient one.
"""
import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import options_costs as oc

HS1 = 0.325             # measured median half-spread, 1 lot
SPOT = {"NIFTY": 24300, "BANKNIFTY": 57300, "FINNIFTY": 26300,
        "SENSEX": 80000}


def executed_lots(cfg):
    """(distribution, mean, max) of real executed lots from the journal."""
    ls = cfg.get("lot_sizes") or {}
    p = os.path.join(os.path.expanduser("~"), ".ltp-monitor", "journal.json")
    det = []
    try:
        for day in json.load(open(p)):
            det.extend(day.get("trades_detail") or [])
    except Exception:
        return {}, None, None
    lots = []
    for t in det:
        q, s = t.get("qty"), (t.get("symbol") or "").upper()
        if q and ls.get(s):
            lots.append(q / ls[s])
    if not lots:
        return {}, None, None
    return (collections.Counter(round(x) for x in lots),
            st.mean(lots), max(lots))


def cost_bias_per_lot(symbol, n_lots, alpha, cfg=None, legs=1):
    """Per-LOT understatement of the flat model at `n_lots`, in rupees.

    Per-lot is the right unit: the replay's net-per-trade is a one-lot
    figure, so the gate must compare it against a one-lot cost. Size
    enters only through the spread term.
    """
    cfg = cfg if cfg is not None else config.load()
    s = SPOT.get(symbol.upper(), 24000)
    lot = (cfg.get("lot_sizes") or {}).get(symbol.upper(), 75)
    prem = s * 0.005
    hs = HS1 * (float(n_lots) ** alpha)
    real = oc.cost_round_trip(prem, prem, lot, legs=legs, cfg=cfg,
                              halfspread=hs)["total"]
    flat = oc.flat_model_cost(legs=legs, cfg=cfg)
    return real - flat


def main():
    cfg = config.load()
    print("\n  CONFIG SIZING")
    for k in ("lots_per_trade", "max_lots_per_trade", "dynamic_sizing_enabled",
              "risk_pct_per_trade"):
        print(f"    {k:24} {cfg.get(k)}")

    dist, mean, mx = executed_lots(cfg)
    print("\n  ACTUALLY EXECUTED (journal trades_detail)")
    if not dist:
        print("    no sized trades found")
        return
    tot = sum(dist.values())
    for k in sorted(dist):
        print(f"    {k:>2} lot(s): {dist[k]:>4}  ({100*dist[k]/tot:.1f}%)")
    above = sum(v for k, v in dist.items() if k > 1)
    print(f"    mean {mean:.2f}   max {mx:.0f}   "
          f"{100*above/tot:.0f}% of trades exceed ONE lot")
    print("\n    => the replays' 1-lot assumption does NOT describe live.")

    print("\n  PER-LOT COST BIAS vs SIZE  (spread impact = hs x n^alpha)")
    print(f"    {'symbol':10} {'n=1':>8} {'n=1.82 a=0':>12} "
          f"{'n=1.82 a=.5':>12} {'n=5 a=.5':>10} {'n=10 a=.5':>11} "
          f"{'n=10 a=1':>10}")
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        row = [cost_bias_per_lot(sym, 1, 0.0, cfg),
               cost_bias_per_lot(sym, mean, 0.0, cfg),
               cost_bias_per_lot(sym, mean, 0.5, cfg),
               cost_bias_per_lot(sym, 5, 0.5, cfg),
               cost_bias_per_lot(sym, 10, 0.5, cfg),
               cost_bias_per_lot(sym, 10, 1.0, cfg)]
        print(f"    {sym:10} " + " ".join(
            f"{v:>{w},.0f}" for v, w in zip(row, (8, 12, 12, 10, 11, 10))))

    print("\n    alpha=0 is size-independent by construction — the columns")
    print("    differ only where impact is admitted. At the mean executed")
    print("    size the bias rises by ~35%; at the observed maximum of 10")
    print("    lots it roughly triples under square-root impact.")


if __name__ == "__main__":
    main()
