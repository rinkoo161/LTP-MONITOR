#!/usr/bin/env python3
"""option_cap_derivation.py — re-derive option_risk_per_trade_rupees.

2026-08-02. The ₹5,000 cap shipped earlier today was calibrated on a
contaminated population: `trades_detail` mixes single-leg option buys
with SPREAD legs, and until the stop-basis fix a spread leg's `stoploss`
was a negative P&L floor rather than a price. Pooling them produced a
"median ₹3,198 per lot" that described nothing real.

This re-derives it on the clean population only, going through
`agents.trade_risk_fields()` so both journal schemas — and the legacy
spread rows — resolve correctly, and filtering to `kind == "option"`.

Per-LOT risk is computed at CURRENT lot sizes, not the sizes in force
when the trade was filled. The cap governs FUTURE trades, so the
contract size that matters is today's.

THE CAP IS NOT A FREE PARAMETER. At `lots_per_trade` = 1 there is
nothing to size down to, so it can only ever BLOCK. Setting it is
therefore a choice about what fraction of signals to refuse, and it has
to be argued against the other risk numbers rather than picked:

    risk_pct_per_trade 2% x backtest_capital 200,000 = ₹4,000/trade
    portfolio_max_drawdown                            = ₹5,000 combined

Those two are already inconsistent with each other — they permit 1.25
concurrent trades — and that inconsistency, not the cap, may be the real
finding.
"""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
import config

CANDIDATES = [2000, 2500, 3000, 3500, 4000, 4500, 5000, 6000, 8000]


def clean_option_risks(cfg):
    """Per-lot rupee risk for genuine single-leg option buys."""
    p = os.path.join(os.path.expanduser("~"), ".ltp-monitor", "journal.json")
    det = []
    for d in json.load(open(p)):
        det.extend(d.get("trades_detail") or [])
    lots = cfg.get("lot_sizes") or {}
    out = []
    for t in det:
        f = agents.trade_risk_fields(t)
        if f["kind"] != "option":
            continue
        e, s = f["entry"], f["stop"]
        sym = (t.get("symbol") or "").upper()
        lot = lots.get(sym)
        if not (e and s and lot) or not (0 < s < e):
            continue
        out.append({"symbol": sym, "risk": abs(e - s) * lot,
                    "entry": e, "width": (e - s) / e, "pnl": t.get("pnl")})
    return out


def main():
    cfg = config.load()
    rows = clean_option_risks(cfg)
    if not rows:
        sys.exit("no clean option trades found")
    r = sorted(x["risk"] for x in rows)
    budget = (cfg.get("backtest_capital", 200000)
              * cfg.get("risk_pct_per_trade", 1.0) / 100)
    kill = cfg.get("portfolio_max_drawdown", 5000)

    def pct(q):
        return r[min(len(r) - 1, int(q * len(r)))]

    print(f"\n  CLEAN single-leg option buys: n={len(rows)}   "
          f"(lot sizes: {cfg.get('lot_sizes')})")
    print(f"  per-lot risk:  median ₹{st.median(r):,.0f}   p75 ₹{pct(.75):,.0f}   "
          f"p90 ₹{pct(.90):,.0f}   max ₹{max(r):,.0f}")
    print(f"  stop width:    median {100*st.median([x['width'] for x in rows]):.0f}% "
          f"of premium")
    print(f"\n  reference points")
    print(f"    risk budget  (2% x ₹200,000)      ₹{budget:,.0f}")
    print(f"    portfolio kill-switch             ₹{kill:,.0f}")
    print(f"    -> they permit {kill/budget:.2f} concurrent trades, which is the")
    print(f"       inconsistency the cap sits between.\n")

    print(f"  {'cap':>7} {'blocked':>9} {'%':>7} {'trades to trip kill':>21} "
          f"{'verdict':>34}")
    for c in CANDIDATES:
        b = sum(1 for x in r if x > c)
        conc = kill / c
        if b / len(r) > 0.5:
            v = "shutdown, not a risk control"
        elif conc < 1:
            v = "one trade can trip the portfolio"
        elif conc < 2:
            v = "portfolio cap survives one trade"
        else:
            v = "portfolio cap survives two"
        print(f"  ₹{c:>6,} {b:>9} {100*b/len(r):>6.1f}% {conc:>21.2f} {v:>34}")

    print(f"\n  P&L of the trades each cap would have REFUSED")
    print(f"  (a cap that only blocks losers is free; one that blocks winners costs)")
    print(f"    {'cap':>7} {'refused':>8} {'their net P&L':>15} {'win rate':>10}")
    for c in CANDIDATES:
        ref = [x for x in rows if x["risk"] > c and x["pnl"] is not None]
        if not ref:
            continue
        net = sum(x["pnl"] for x in ref)
        wr = 100 * sum(1 for x in ref if x["pnl"] > 0) / len(ref)
        print(f"    ₹{c:>6,} {len(ref):>8} {net:>+15,.0f} {wr:>9.0f}%")


if __name__ == "__main__":
    main()
