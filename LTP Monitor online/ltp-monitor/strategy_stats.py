#!/usr/bin/env python3
"""strategy_stats.py — per-strategy performance from the trade journal.

2026-08-06. Written after reading a QuantConnect leaderboard that ranked
by OOS 3-month Sharpe: its #2 strategy had a -4.00% five-year CAGR and a
-0.80 one-year Sharpe, and its #6 had an 89.10% drawdown. Three months
is ~60 observations, so that ranking is mostly luck, and it hides the
two things that actually end an account — drawdown and a long-run
negative edge.

So this reports RISK-ADJUSTED return WITH drawdown, and refuses to
collapse them into one number. `recovery` (net / |maxDD|) is the closest
thing to a single verdict: below 1.0 the strategy has not yet earned
back its own worst run.

DELIBERATELY NOT ANNUALISED. The journal is weeks old, not years.
Annualising a 3-week Sharpe produces an impressive figure that means
nothing, which is the exact error this module exists to avoid — so
`sharpe` here is per-trade (mean / stdev of trade P&L) and is labelled
as such everywhere it surfaces.

Reads the same `trades.jsonl` the journal and the probability engine
read; no new persistence.
"""
import json
import math
import os
import statistics as st


def _kind(t):
    if t.get("leg") == "SPREAD":
        return "spread"
    if t.get("kind") or t.get("side") in ("LONG", "SHORT"):
        return "future"
    if t.get("leg") in ("CE", "PE"):
        return "option"
    return "other"


def _label(t):
    s = t.get("strategy") or t.get("setup")
    return s if s else f"({_kind(t)}, unattributed)"


def _metrics(pnls):
    """Cumulative-curve metrics for one ordered list of trade P&Ls."""
    eq = peak = 0.0
    maxdd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
    mean = st.mean(pnls)
    sd = st.pstdev(pnls) if len(pnls) > 1 else 0.0
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    return {
        "n": len(pnls),
        "net": round(sum(pnls)),
        "mean": round(mean),
        # per-trade, NOT annualised — see the module docstring
        "sharpe": round(mean / sd, 2) if sd else None,
        "maxdd": round(maxdd),
        # net / |maxDD|. Below 1.0 the strategy has not earned back its
        # own worst run. None when it has never had a drawdown, which on
        # a short journal means "not enough history", not "infinite".
        "recovery": round(sum(pnls) / abs(maxdd), 2) if maxdd else None,
        "win_pct": round(100 * len(wins) / len(pnls)),
        "avg_win": round(st.mean(wins)) if wins else 0,
        "avg_loss": round(st.mean(losses)) if losses else 0,
        "payoff": (round(st.mean(wins) / abs(st.mean(losses)), 2)
                   if wins and losses else None),
    }


def performance(trades_file=None, min_trades=3, since=None):
    """[{strategy, kind, ...metrics}], worst-first is the caller's choice.

    `min_trades` exists because a 1-trade "strategy" with a +100% win
    rate is noise that would top any sort. Rows below the threshold are
    returned separately under `thin` rather than dropped — a bucket that
    silently vanishes is how "this strategy has no losses" gets
    believed.
    """
    path = trades_file or os.path.expanduser("~/.ltp-monitor/trades.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    if since:
        rows = [t for t in rows if str(t.get("closed_date", "")) >= since]
    # Chronological, because maxdd depends on ORDER. Sorting by close
    # date+time is what makes the drawdown figure meaningful rather than
    # an artifact of file order.
    rows.sort(key=lambda t: (str(t.get("closed_date")), str(t.get("closed"))))

    groups = {}
    for t in rows:
        groups.setdefault(_label(t), {"kind": _kind(t), "pnls": []})
        groups[_label(t)]["pnls"].append(t.get("pnl", 0) or 0)

    out, thin = [], []
    for name, g in groups.items():
        rec = {"strategy": name, "kind": g["kind"], **_metrics(g["pnls"])}
        (out if len(g["pnls"]) >= min_trades else thin).append(rec)
    out.sort(key=lambda r: -r["net"])
    thin.sort(key=lambda r: -r["net"])
    total = [t.get("pnl", 0) or 0 for t in rows]
    return {
        "strategies": out,
        "thin": thin,
        "min_trades": min_trades,
        "total": {**_metrics(total), "strategy": "ALL", "kind": "all"} if total else None,
        "note": ("per-trade Sharpe (mean/stdev of trade P&L), NOT annualised — "
                 "the journal spans weeks, and annualising it would produce a "
                 "figure that means nothing. recovery = net / |max drawdown|; "
                 "below 1.0 the strategy has not earned back its own worst run."),
    }
