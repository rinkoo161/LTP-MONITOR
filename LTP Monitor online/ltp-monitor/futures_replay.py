"""futures_replay.py — the futures research harness.

v59.0 Phase A §4.1. Three things that did not exist:

  replay_futures()   session-by-session, bar-by-bar, costed on every trade
  walk_forward()     6-month IS / 2-month OOS rolled forward, params chosen
                     INSIDE each IS window and each OOS window used once
  metrics()          the Phase A acceptance gates, including target-reach

`backtester.replay_futures` delegates here so `_replay_for()` and
`sweep_params()` can reach it by the existing convention, while the
research code stays out of a module that already drives live promotion.

WHAT THESE NUMBERS ARE, stated once and repeated in every result:

  * Futures candles were never archived. The replay uses INDEX 1-minute
    candles as the futures proxy. Intraday the basis is near-constant, so
    signal geometry and per-trade point moves are faithful; absolute
    levels are not, and notional-linked costs carry the basis error
    (~0.5-1%). Every result therefore carries `proxy="index"`.
  * Costs come from futures_costs.cost_round_trip on EVERY trade — never
    fee_per_lot, which Phase 0 showed understates futures cost ~10x and
    which charged these 40 real trades exactly zero.

No live path. Nothing here can place an order.
"""
import datetime as dt
import math

import futures_costs
import futures_strategies as fs
import history

SEC = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "SENSEX": "51"}


# ------------------------------------------------------------ data access

def load_sessions(symbol, start=None, end=None, min_bars=60):
    """1-minute bars grouped into IST sessions, ascending, causal order."""
    sid = SEC.get((symbol or "").upper())
    if not sid:
        raise ValueError(f"no index series mapped for {symbol!r}")
    c = history._conn()
    try:
        q = ("SELECT ts,o,h,l,c,v FROM candles WHERE security_id=? "
             "AND c IS NOT NULL")
        args = [sid]
        if start:
            q += " AND ts>=?"; args.append(int(start))
        if end:
            q += " AND ts<?"; args.append(int(end))
        rows = c.execute(q + " ORDER BY ts", args).fetchall()
    finally:
        c.close()
    sessions = {}
    for ts, o, h, l, cl, v in rows:
        d = dt.datetime.fromtimestamp(ts)
        if not (9 * 60 + 15 <= d.hour * 60 + d.minute <= 15 * 60 + 29):
            continue
        sessions.setdefault(d.strftime("%Y-%m-%d"), []).append(
            {"ts": ts, "o": o, "h": h, "l": l, "c": cl, "v": v})
    return [(day, bars) for day, bars in sorted(sessions.items())
            if len(bars) >= min_bars]


# --------------------------------------------------------------- the loop

def _exit_price(bar, side, stop, target):
    """Which level a bar resolves to, and why.

    A bar that spans both stop and target is charged as the STOP. Within
    one minute there is no way to know which printed first, and assuming
    the target is how a backtest flatters itself.
    """
    sign = 1 if side == "LONG" else -1
    hit_stop = stop is not None and (
        bar["l"] <= stop if side == "LONG" else bar["h"] >= stop)
    hit_tgt = target is not None and (
        bar["h"] >= target if side == "LONG" else bar["l"] <= target)
    if hit_stop:
        return stop, "stop"
    if hit_tgt:
        return target, "target"
    return None, None


def replay_futures(symbol, name, params=None, days=None, log=lambda m: None,
                   start=None, end=None, lot=None, require_volume=False):
    """Replay one strategy over one symbol. Returns {trades, metrics, ...}."""
    if name not in fs.STRATEGIES:
        raise KeyError(f"unknown futures strategy {name!r}")
    decide, _, _ = fs.STRATEGIES[name]
    p = fs.clamp(name, params)
    sessions = load_sessions(symbol, start, end)
    if days:
        sessions = sessions[-int(days):]
    if lot is None:
        lot, lot_src = futures_costs.lot_size(symbol)
    else:
        lot_src = "caller"

    trades = []
    no_volume_sessions = 0
    for day, bars in sessions:
        state = {"trades_today": 0, "require_volume": require_volume}
        open_pos = None
        for i, bar in enumerate(bars):
            if open_pos:
                px, why = _exit_price(bar, open_pos["side"],
                                      open_pos["stop"], open_pos["target"])
                lim = open_pos.get("exit_at_min")
                if px is None and lim is not None and fs._sess_minute(bar) >= lim:
                    px, why = bar["c"], "time"
                if px is None and i == len(bars) - 1:
                    px, why = bar["c"], "eod"
                if px is not None:
                    trades.append(_close(open_pos, px, why, bar, lot, symbol, p))
                    open_pos = None
                continue
            sig = decide(bars, i, state, p)
            if sig:
                open_pos = dict(sig, day=day, entry=bar["c"], entry_ts=bar["ts"],
                                entry_i=i)
        if state.get("no_volume"):
            no_volume_sessions += 1

    m = metrics(trades, lot)
    return {"symbol": symbol, "strategy": name, "params": p,
            "sessions": len(sessions), "trades": trades, "metrics": m,
            "lot_size": lot, "lot_size_source": lot_src,
            "proxy": "index", "require_volume": require_volume,
            "sessions_without_volume": no_volume_sessions,
            "caveat": ("futures candles were never archived; INDEX candles are "
                       "used as the futures proxy — intraday geometry faithful, "
                       "absolute levels and notional costs carry the basis error")}


def _close(pos, px, why, bar, lot, symbol, p):
    sign = 1 if pos["side"] == "LONG" else -1
    pts = (px - pos["entry"]) * sign
    gross = pts * lot
    cost = futures_costs.cost_round_trip(symbol, pos["entry"], px, 1, lot=lot)
    return {"day": pos["day"], "side": pos["side"], "entry": pos["entry"],
            "exit": px, "exit_reason": why, "points": pts,
            "gross": gross, "cost": cost, "net": gross - cost,
            "net_points": (gross - cost) / lot,
            "stop": pos.get("stop"), "target": pos.get("target"),
            "why": pos.get("why"), "entry_ts": pos["entry_ts"],
            "bars_held": bar and 0}


# ---------------------------------------------------------------- metrics

def _skew(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    if sd == 0:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((x - m) / sd) ** 3 for x in xs)


def metrics(trades, lot):
    """The Phase A gate metrics. Everything net of costs; never gross."""
    n = len(trades)
    out = {"trades": n}
    if not n:
        return dict(out, verdict="INSUFFICIENT", reason="no trades")
    nets = [t["net_points"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    mean = sum(nets) / n
    sd = (sum((x - mean) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    out["win_rate"] = 100 * len(wins) / n
    out["expectancy_points"] = mean
    out["net_total"] = sum(t["net"] for t in trades)
    out["gross_total"] = sum(t["gross"] for t in trades)
    out["cost_total"] = sum(t["cost"] for t in trades)
    out["t_stat"] = (mean / (sd / math.sqrt(n))) if sd > 0 else 0.0
    out["avg_win"] = (sum(t["net"] for t in wins) / len(wins)) if wins else 0.0
    out["avg_loss"] = (sum(t["net"] for t in losses) / len(losses)) if losses else 0.0
    out["payoff"] = abs(out["avg_win"] / out["avg_loss"]) if out["avg_loss"] else 0.0
    # A strategy with no target exits on time by design. Reporting 0%
    # reads as "nothing reached target", which is the defect this gate
    # exists to catch — a different statement from "there is no target".
    has_target = any(t.get("target") is not None for t in trades)
    out["has_target"] = has_target
    out["target_reach_rate"] = (100 * sum(
        1 for t in trades if t["exit_reason"] == "target") / n
        if has_target else None)
    out["skew"] = _skew(nets)
    # v59.0 item 1 — the shipped version divided cost by |sum of SIGNED
    # gross|, i.e. by the NET gross outcome. That denominator is small and
    # can approach zero (S14: -₹43,941 on ₹310,784 of cost -> 707%), so it
    # exploded while saying nothing about cost burden, and inverting it
    # implied per-trade costs of 12-36 points that do not exist. Measured
    # directly, cost is 9.14-9.22 pts for every strategy, as it must be:
    # one instrument, one cost function, always one lot.
    #
    # Cost drag is only meaningful against a POSITIVE gross edge. A
    # strategy whose gross is negative has no edge for cost to erode —
    # reporting a percentage there invites the reader to think the cost is
    # the problem when the entry is.
    out["gross_points_per_trade"] = sum(t["gross"] for t in trades) / n / lot
    out["cost_points_per_trade"] = out["cost_total"] / n / lot
    ge = out["gross_points_per_trade"]
    out["cost_vs_gross_edge_pct"] = (100 * out["cost_points_per_trade"] / ge
                                     if ge > 0 else None)
    out["cost_drag_pct"] = out["cost_vs_gross_edge_pct"]   # gate reads this
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t["net"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out["max_drawdown"] = dd
    out["max_drawdown_points"] = dd / lot if lot else None
    out["exit_mix"] = {}
    for t in trades:
        out["exit_mix"][t["exit_reason"]] = out["exit_mix"].get(t["exit_reason"], 0) + 1
    return out


def verdict(m, oos_ratio=None, is_expectancy=None, data_ok=True,
            data_reason=""):
    """Apply §4.3 as HARD gates. Below 200 trades is INSUFFICIENT, which
    is deliberately not FAIL: too small to score is a different statement
    from measured and bad, and the spec is explicit that 40 trades could
    not establish the old engine was bad either."""
    fails = []
    n = m.get("trades", 0)
    # A strategy whose required INPUT does not exist was not tested.
    # Recording FAIL would tell a later session it had been.
    if not data_ok:
        return "INSUFFICIENT", [data_reason or "required data unavailable"]
    if n < 200:
        return "INSUFFICIENT", [f"only {n} trades (<200 to score)"]
    if not (m.get("expectancy_points", 0) > 0):
        fails.append(f"expectancy {m.get('expectancy_points', 0):.3f} pts <= 0")
    if m.get("t_stat", 0) < 2.0:
        fails.append(f"t-stat {m.get('t_stat', 0):.2f} < 2.0")
    tr = m.get("target_reach_rate")
    if tr is None:
        pass                       # time-exit strategy: gate inapplicable
    elif tr < 15.0:
        fails.append(f"target-reach {tr:.1f}% < 15%")
    # The OOS/IS ratio is meaningless when IS expectancy is negative: a
    # strategy losing MORE out of sample can show a ratio above 1.0 and
    # nominally clear a >=0.5 gate. Evaluate only against a positive IS.
    if oos_ratio is not None and (is_expectancy or 0) > 0 and oos_ratio < 0.5:
        fails.append(f"walk-forward degradation {oos_ratio:.2f} < 0.5")
    return ("PASS" if not fails else "FAIL"), fails


# ----------------------------------------------------------- walk-forward

def walk_forward(symbol, name, grid=None, is_months=6, oos_months=2,
                 log=lambda m: None, require_volume=False):
    """6-month in-sample / 2-month out-of-sample, rolled forward.

    Parameters are chosen INSIDE each IS window only, and each OOS window
    is evaluated once with those parameters and never revisited. That is
    the whole discipline: an OOS window consulted twice is in-sample.
    """
    sessions = load_sessions(symbol)
    if not sessions:
        return {"folds": [], "error": "no sessions"}
    days = [d for d, _ in sessions]
    first = dt.date.fromisoformat(days[0])
    last = dt.date.fromisoformat(days[-1])
    folds = []
    is_len = dt.timedelta(days=int(is_months * 30.44))
    oos_len = dt.timedelta(days=int(oos_months * 30.44))
    cur = first
    while cur + is_len + oos_len <= last:
        folds.append((cur, cur + is_len, cur + is_len + oos_len))
        cur = cur + oos_len
    results = []
    for k, (a, b, c) in enumerate(folds, 1):
        to_ts = lambda d: int(dt.datetime.combine(d, dt.time(0, 0)).timestamp())
        best, best_m = None, None
        for cand in (grid or [None]):
            r = replay_futures(symbol, name, cand, start=to_ts(a), end=to_ts(b),
                               require_volume=require_volume)
            e = r["metrics"].get("expectancy_points", -1e9)
            if best_m is None or e > best_m:
                best, best_m = cand, e
        oos = replay_futures(symbol, name, best, start=to_ts(b), end=to_ts(c),
                             require_volume=require_volume)
        ins = replay_futures(symbol, name, best, start=to_ts(a), end=to_ts(b),
                             require_volume=require_volume)
        results.append({
            "fold": k, "is": [a.isoformat(), b.isoformat()],
            "oos": [b.isoformat(), c.isoformat()], "params": best,
            "is_expectancy": ins["metrics"].get("expectancy_points"),
            "oos_expectancy": oos["metrics"].get("expectancy_points"),
            "is_trades": ins["metrics"].get("trades"),
            "oos_trades": oos["metrics"].get("trades"),
            "oos_metrics": oos["metrics"],
        })
        _f = lambda v: "n/a" if v is None else f"{v:+.3f}"
        log(f"    fold {k}: IS {a}..{b} -> OOS {b}..{c}  "
            f"IS {_f(results[-1]['is_expectancy'])} / "
            f"OOS {_f(results[-1]['oos_expectancy'])} pts")
    # A fold with no trades has expectancy None, not 0. Treating it as 0
    # would let empty folds drag a ratio toward zero and read as
    # degradation that was never measured.
    scored = [f for f in results
              if f["is_expectancy"] is not None and f["oos_expectancy"] is not None]
    is_tot = sum(f["is_expectancy"] for f in scored)
    oos_tot = sum(f["oos_expectancy"] for f in scored)
    ratio = (oos_tot / is_tot) if is_tot else None
    return {"symbol": symbol, "strategy": name, "folds": results,
            "n_folds": len(results), "n_scored_folds": len(scored), "is_expectancy_sum": is_tot,
            "oos_expectancy_sum": oos_tot, "oos_is_ratio": ratio}
