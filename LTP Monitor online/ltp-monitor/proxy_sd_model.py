#!/usr/bin/env python3
"""proxy_sd_model.py — what does the ₹1,143 actually vary with?

v59.0 item 24. Two strategies (ema_mtf BANKNIFTY 1,061 and NIFTY 1,080)
have LESS total P&L variance than the ₹1,143 claimed as their P&L-model
noise. That is not a caveat on a constant — it is a falsification of the
constant's generality. A noise term cannot exceed the entire variance of
the thing it is noise in.

So ₹1,143 is not a constant. It is the value of some function, averaged
over whatever mix of trades happened to be in the 74-trade sample. This
finds what that function varies with, because everything downstream of
the promotion gate depends on it.

Candidate drivers, from the structure of the proxy itself
(`pnl = pts x 0.5 x lot`):

  hold duration  theta is missing entirely, and decay accumulates with
                 time held. Longer holds => larger omitted term.
  move size      gamma is missing, so delta is wrong by more the further
                 spot travels. Bigger moves => larger omitted term.
  lot size       the error is per-share and multiplied by lot, so it
                 should scale close to LINEARLY with lot size. A NIFTY
                 (75) and a BANKNIFTY (35) trade cannot share one rupee
                 constant.
  premium level  a fixed points error is a bigger fraction of a cheap
                 option, and IV moves reprice expensive ones harder.

Reports the error sd within each bucket, not the mean. The mean is the
bias question (item 15); the sd is the one the gate consumes.
"""
import argparse
import statistics as st
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy_error as pe
import backtester as bt
import config
import history


def bucket_sd(rows, key, edges, labels):
    out = []
    for i, lab in enumerate(labels):
        lo = edges[i]
        hi = edges[i + 1] if i + 1 < len(edges) else float("inf")
        g = [r for r in rows if lo <= (r.get(key) or 0) < hi]
        if len(g) >= 3:
            out.append((lab, len(g), st.pstdev(r["err"] for r in g),
                        st.mean(r["err"] for r in g)))
        elif g:
            out.append((lab, len(g), None, st.mean(r["err"] for r in g)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    a = ap.parse_args()
    cfg = config.load()
    rows = []
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        lot = (cfg.get("lot_sizes") or {}).get(sym, 75)
        days = history.index_days(sym, a.days)
        for name in ("vwap_pullback", "momentum_confluence", "orb", "ema_mtf"):
            try:
                out = bt.replay_pa(sym, name, days=days)
                ts = out["trades"] if isinstance(out, dict) else out
            except Exception:
                continue
            for t in ts:
                r = pe.reprice(sym, t, lot, cfg)
                if r:
                    proxy, real, diag = r
                    rows.append({"sym": sym, "strat": name, "lot": lot,
                                 "proxy": proxy, "real": real,
                                 "err": real - proxy, **diag})
    if not rows:
        sys.exit("no repriceable trades")

    all_sd = st.pstdev(r["err"] for r in rows)
    print(f"\n  n={len(rows)}   pooled sd = ₹{all_sd:,.0f}  "
          f"(this is where 1,143 came from)\n")

    print("  BY HOLD DURATION (theta)")
    print(f"    {'bucket':10} {'n':>4} {'sd ₹':>9} {'mean ₹':>9}")
    for lab, n, sd, mu in bucket_sd(rows, "hold_min", [0, 30, 90],
                                    ["<30m", "30-90m", ">90m"]):
        print(f"    {lab:10} {n:>4} {(f'{sd:,.0f}' if sd else 'n/a'):>9} {mu:>9,.0f}")

    print("\n  BY MOVE SIZE (gamma)")
    rows2 = [dict(r, absmove=abs(r["move_pts"])) for r in rows]
    for lab, n, sd, mu in bucket_sd(rows2, "absmove", [0, 25, 75],
                                    ["<25pts", "25-75pts", ">75pts"]):
        print(f"    {lab:10} {n:>4} {(f'{sd:,.0f}' if sd else 'n/a'):>9} {mu:>9,.0f}")

    print("\n  BY LOT SIZE — the error is per-share, so it should scale")
    print(f"    {'symbol':10} {'lot':>5} {'n':>4} {'sd ₹':>9} {'sd/share':>10}")
    for sym in sorted({r["sym"] for r in rows}):
        g = [r for r in rows if r["sym"] == sym]
        if len(g) < 3:
            continue
        sd = st.pstdev(r["err"] for r in g)
        print(f"    {sym:10} {g[0]['lot']:>5} {len(g):>4} {sd:>9,.0f} "
              f"{sd / g[0]['lot']:>10.2f}")

    print("\n  BY ENTRY PREMIUM")
    for lab, n, sd, mu in bucket_sd(rows, "entry_prem", [0, 100, 250],
                                    ["<100", "100-250", ">250"]):
        print(f"    {lab:10} {n:>4} {(f'{sd:,.0f}' if sd else 'n/a'):>9} {mu:>9,.0f}")

    print("\n  BY STRATEGY — does the anomaly line up?")
    print(f"    {'strategy':22} {'n':>4} {'sd ₹':>9} {'mean hold':>10} "
          f"{'mean |move|':>12}")
    for s in sorted({r["strat"] for r in rows}):
        g = [r for r in rows if r["strat"] == s]
        sd = st.pstdev(r["err"] for r in g) if len(g) >= 3 else None
        print(f"    {s:22} {len(g):>4} {(f'{sd:,.0f}' if sd else 'n/a'):>9} "
              f"{st.mean(r['hold_min'] for r in g):>9.0f}m "
              f"{st.mean(abs(r['move_pts']) for r in g):>11.0f}")

    # ---- the assumption underneath all of it -------------------------
    # Every use of ₹1,143 so far — the gate's quadrature term and item
    # 21a's deconvolution — assumes the proxy error is INDEPENDENT
    # additive noise:  proxy = truth + error, with Cov(truth, error) = 0.
    # But `err = real - proxy` and both `real` and `proxy` are driven by
    # the SAME spot move, so that covariance has no reason to be zero.
    # If it isn't, variances do not add, and an own_sd below 1,143 stops
    # being a paradox.
    def corr(xs, ys):
        n = len(xs)
        mx, my = st.mean(xs), st.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = sum((x - mx) ** 2 for x in xs) ** 0.5
        dy = sum((y - my) ** 2 for y in ys) ** 0.5
        return num / (dx * dy) if dx and dy else 0.0

    proxy = [r["proxy"] for r in rows]
    real = [r["real"] for r in rows]
    errs = [r["err"] for r in rows]
    print("\n  IS THE NOISE ACTUALLY INDEPENDENT?  (the load-bearing assumption)")
    if len(proxy) == len(errs) == len(real) and proxy:
        sp_, sr_, se_ = st.pstdev(proxy), st.pstdev(real), st.pstdev(errs)
        print(f"    sd(proxy P&L) ₹{sp_:,.0f}   sd(real P&L) ₹{sr_:,.0f}   "
              f"sd(error) ₹{se_:,.0f}")
        print(f"    corr(error, proxy P&L) = {corr(errs, proxy):+.2f}")
        print(f"    corr(error, real  P&L) = {corr(errs, real):+.2f}")
        lhs = sp_ ** 2
        rhs = sr_ ** 2 + se_ ** 2
        print(f"    Var(proxy) = {lhs:,.0f}   vs   Var(real)+Var(err) = {rhs:,.0f}")
        print(f"    => the independent-additive identity is "
              f"{'HELD' if abs(lhs - rhs) / max(lhs, rhs) < 0.15 else 'VIOLATED'} "
              f"(off by {100 * abs(lhs - rhs) / max(lhs, rhs):.0f}%)")
    print("\n  The gate consumes the sd. If it varies materially across")
    print("  these buckets, a single ₹1,143 is the wrong shape and the")
    print("  per-strategy figure should be used instead.")


if __name__ == "__main__":
    main()
