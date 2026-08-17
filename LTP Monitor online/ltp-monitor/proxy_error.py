#!/usr/bin/env python3
"""proxy_error.py — how wrong is `pts x 0.5 x lot`?

v59.0 item 15. Every price-action replay prices an option trade as

    pnl = (exit_spot - entry_spot) x direction x 0.5 x lot - fee

which never references an option price. It therefore omits:

  gamma  delta is not 0.5 for the whole hold; on a large move the real
         option gains MORE than the proxy says (understates winners)
  theta  absent entirely. On a near-expiry ATM option a two-hour hold can
         decay more than the entire bid-ask we have been arguing about,
         and it is a pure LOSS to the buyer every time
  IV     a volatility move repriceS the option with no spot move at all

Two of those have opposite signs, so the net error is not guessable — it
has to be measured. This reprices real replay trades from the actual
premiums in chain_snapshots and reports the error distribution against
the 1.4-2.4x cost understatement, so the two can be compared on one
scale.

HARD LIMIT, stated up front: chain_snapshots keeps 5 days. It overlaps
the 250-session backtest window by 4 sessions, giving ~110 repriceable
trades. That is enough to size the error and its sign; it is NOT enough
to correct the historical P&L, and this script never claims to.
"""
import argparse
import statistics as st
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtester as bt
import config
import history


def chain_at(symbol, ts, strike, leg, tol=180):
    """Nearest snapshot within `tol` seconds — ltp, bid, ask and greeks."""
    c = history._conn()
    try:
        # 2026-08-17 — LIMIT 1 could return an out-of-session frame as
        # the "nearest" quote; take candidates and keep the nearest
        # IN-SESSION one instead.
        cands = c.execute(
            "SELECT ts, ltp, bid, ask, delta, theta, iv FROM chain_snapshots "
            "WHERE symbol=? AND strike=? AND leg=? AND ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts-?) LIMIT 20",
            (symbol.upper(), strike, leg, ts - tol, ts + tol, ts)).fetchall()
    finally:
        c.close()
    import agents
    r = next((x for x in cands if agents.in_market_session(int(x[0]))), None)
    if not r:
        return None
    return {"ts": r[0], "ltp": r[1], "bid": r[2], "ask": r[3],
            "delta": r[4], "theta": r[5], "iv": r[6]}


def atm_strike(symbol, ts, spot):
    """The strike actually present in the archive nearest to spot."""
    c = history._conn()
    try:
        # 2026-08-17 — same in-session guard as snap() above.
        cands = c.execute(
            "SELECT strike, ts FROM chain_snapshots WHERE symbol=? "
            "AND ts BETWEEN ? AND ? ORDER BY ABS(strike-?) LIMIT 40",
            (symbol.upper(), ts - 600, ts + 600, spot)).fetchall()
    finally:
        c.close()
    import agents
    r = next((x for x in cands if agents.in_market_session(int(x[1]))), None)
    return r[0] if r else None


def reprice(symbol, t, lot, cfg):
    """(proxy_pnl, real_pnl, diagnostics) or None if unrepriceable."""
    d = 1 if t["exit_spot"] >= t["entry_spot"] else -1
    # The replay's own direction: infer from pnl sign vs spot move, since
    # the trade record stores neither side nor leg.
    if t["pnl"] is not None:
        moved_up = t["exit_spot"] >= t["entry_spot"]
        won = t["pnl"] > 0
        d = 1 if (moved_up == won) else -1
    leg = "ce" if d > 0 else "pe"
    k = atm_strike(symbol, t["entry_ts"], t["entry_spot"])
    if k is None:
        return None
    a = chain_at(symbol, t["entry_ts"], k, leg)
    b = chain_at(symbol, t["exit_ts"], k, leg)
    if not a or not b or not a.get("ltp") or not b.get("ltp"):
        return None
    fee = cfg.get("fee_per_lot", 40) * 2
    pts = (t["exit_spot"] - t["entry_spot"]) * d
    proxy = pts * 0.5 * lot - fee
    real = (b["ltp"] - a["ltp"]) * lot - fee
    hold_min = (t["exit_ts"] - t["entry_ts"]) / 60.0
    spread_pts = None
    if a.get("bid") and a.get("ask"):
        spread_pts = a["ask"] - a["bid"]
    return proxy, real, {
        "strike": k, "leg": leg, "hold_min": hold_min, "move_pts": pts,
        "entry_prem": a["ltp"], "exit_prem": b["ltp"],
        "entry_delta": a.get("delta"), "entry_theta": a.get("theta"),
        "entry_iv": a.get("iv"), "spread_pts": spread_pts,
    }


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
                r = reprice(sym, t, lot, cfg)
                if r:
                    proxy, real, diag = r
                    rows.append({"sym": sym, "strat": name, "proxy": proxy,
                                 "real": real, "err": real - proxy, **diag})
    if not rows:
        sys.exit("no trades could be repriced — check chain_snapshots coverage")

    errs = [r["err"] for r in rows]
    print(f"  repriced {len(rows)} trades against real chain premiums\n")
    print(f"  {'':22} {'proxy ₹':>10} {'real ₹':>10} {'error ₹':>10}")
    print(f"  {'total':22} {sum(r['proxy'] for r in rows):>10,.0f} "
          f"{sum(r['real'] for r in rows):>10,.0f} {sum(errs):>10,.0f}")
    print(f"  {'mean per trade':22} {st.mean(r['proxy'] for r in rows):>10,.0f} "
          f"{st.mean(r['real'] for r in rows):>10,.0f} {st.mean(errs):>10,.0f}")
    print(f"  {'sd of error':22} {'':>10} {'':>10} {st.pstdev(errs):>10,.0f}")
    neg = sum(1 for e in errs if e < 0)
    print(f"\n  sign: real BELOW proxy on {neg}/{len(errs)} trades "
          f"({100*neg/len(errs):.0f}%) — the proxy is {'OPTIMISTIC' if neg > len(errs)/2 else 'pessimistic'}")

    def corr(xs, ys):
        n = len(xs)
        if n < 3:
            return 0.0
        mx, my = st.mean(xs), st.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = (sum((x - mx) ** 2 for x in xs) ** 0.5)
        dy = (sum((y - my) ** 2 for y in ys) ** 0.5)
        return num / (dx * dy) if dx and dy else 0.0

    holds = [r["hold_min"] for r in rows]
    moves = [abs(r["move_pts"]) for r in rows]
    print(f"\n  correlation of error with hold duration (theta): "
          f"{corr(holds, errs):+.2f}")
    print(f"  correlation of |error| with move size   (gamma): "
          f"{corr(moves, [abs(e) for e in errs]):+.2f}")

    print("\n  by hold duration:")
    for lo, hi, lab in ((0, 30, "<30m"), (30, 90, "30-90m"), (90, 1e9, ">90m")):
        g = [r for r in rows if lo <= r["hold_min"] < hi]
        if g:
            print(f"    {lab:8} {len(g):>3} trades   mean error "
                  f"₹{st.mean(r['err'] for r in g):>9,.0f}")

    sp = [r["spread_pts"] for r in rows if r.get("spread_pts") is not None]
    if sp:
        print(f"\n  measured bid-ask at these strikes: median {st.median(sp):.2f} pts, "
              f"mean {st.mean(sp):.2f}, max {max(sp):.2f}  (n={len(sp)})")
    print("\n  LIMIT: chain_snapshots keeps 5 days, so this is the overlap only.")
    print("  It sizes the proxy error; it cannot correct historical P&L.")


if __name__ == "__main__":
    main()
