"""backtester.py — replays the REAL analyzer/strategy code over locally
stored chain history. No proxies: the same wall ranking, spread entry
filters and exit rules that trade live are what get tested.

Strategy parameters are read through get_params()/versioning so retuned
versions can be validated before deployment (requirements 8-11).
"""
import json
import os
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
def _now(): return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _completed_days(day_list):
    """Exclude today's IST calendar date from a list of day strings.

    The chain/index archives write data live during market hours, so
    today shows up as a "day" with data long before the session has
    actually closed. Replaying it treats the most recent captured
    frame as if it were EOD and force-exits an open position on
    partial, still-changing data — producing a spurious low-sample
    trade row (e.g. a spread strategy showing "1 day, 2 trades") that
    doesn't reflect a real, closed trading day. Only fully-closed days
    should count toward backtest stats."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return [d for d in day_list if d != today]

import statistics
import history
import config
import analyzer as _an
import strategies as slib
import pa_strategies as pa

VERS_PATH = os.path.expanduser("~/.ltp-monitor/strategy_versions.json")

DEFAULT_PARAMS = {
    "bull_put_spread":  {"wall_gap_frac": 0.8, "credit_min_frac": 0.15,
                         "profit_capture": 0.60, "loss_mult": 1.5},
    "bear_call_spread": {"wall_gap_frac": 0.8, "credit_min_frac": 0.15,
                         "profit_capture": 0.60, "loss_mult": 1.5},
    "momentum_buy":     {"sl_frac": 0.70, "t1_frac": 1.60, "t2_frac": 2.05,
                         "min_confidence": 70, "trail_trigger": 1.05,
                         "trail_gap": 0.10},
    **{n: dict(p) for n, p in pa.PA_DEFAULTS.items()},
}


def load_versions():
    if os.path.exists(VERS_PATH):
        return json.load(open(VERS_PATH))
    v = {}
    save_versions(v)
    return v


def save_versions(v):
    os.makedirs(os.path.dirname(VERS_PATH), exist_ok=True)
    json.dump(v, open(VERS_PATH, "w"), indent=1)


def _symbol_entry(v, name, symbol):
    """Versions are per (strategy, symbol) — NIFTY and BANKNIFTY often need
    different parameters (their backtest results diverge sharply), so a
    single global version would over-fit one index at the expense of
    another. live_enabled is a hard profitability gate, separate from
    which version is "active" for reference."""
    node = v.setdefault(name, {}).setdefault("symbols", {})
    if symbol not in node:
        node[symbol] = {"active": 1, "live_enabled": False,
                        "manually_disabled": False, "tuning_attempts": 0,
                        "tuning_exhausted": False, "next_tune_at": None,
                        "versions": [{"v": 1, "params": DEFAULT_PARAMS.get(name, {}),
                                     "reason": "initial", "created": _now(),
                                     "last_tested": None, "results": None}]}
    return node[symbol]


def meaningful_improvement(old_pnl, new_pnl, threshold=0.15):
    """A candidate version only counts as worth keeping if it clears a
    real bar — not just any direction of movement. Prevents daily
    backtest runs from spawning a new version every single day over
    noise-level differences."""
    if new_pnl > 0 and old_pnl <= 0:
        return True                                    # flipped profitable
    if old_pnl <= 0 and new_pnl <= 0:
        if old_pnl == 0:
            return False
        return (new_pnl - old_pnl) >= abs(old_pnl) * threshold   # loss shrank enough
    if old_pnl > 0:
        return (new_pnl - old_pnl) >= abs(old_pnl) * threshold   # gain grew enough
    return False


def get_params(name, symbol=None):
    v = load_versions()
    if symbol is None:
        # legacy/global caller (e.g. momentum_buy) — use a fixed pseudo-symbol
        symbol = "_global"
    entry = _symbol_entry(v, name, symbol)
    active = entry["active"]
    for ver in entry["versions"]:
        if ver["v"] == active:
            return {**DEFAULT_PARAMS.get(name, {}), **ver["params"]}
    return DEFAULT_PARAMS.get(name, {})


def is_live_enabled(name, symbol):
    v = load_versions()
    entry = v.get(name, {}).get("symbols", {}).get(symbol)
    return bool(entry and entry.get("live_enabled") and
               not entry.get("manually_disabled"))


# ------------------------------------------------------------ metrics
def metrics(trades):
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    days = {t["day"] for t in trades}
    return {
        "trades": len(trades),
        "days_tested": len(days),
        "net_pnl": round(sum(pnls), 0),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "wl_ratio": round(len(wins) / max(1, len(losses)), 2),
        "avg_win": round(statistics.mean(wins), 0) if wins else 0,
        "avg_loss": round(statistics.mean(losses), 0) if losses else 0,
        "max_win": round(max(pnls), 0),
        "max_loss": round(min(pnls), 0),
        "avg_risk_per_trade": round(statistics.mean(
            [abs(t.get("risk", 0)) for t in trades]), 0),
        "sl_hit_rate": round(100 * sum(1 for t in trades
                             if "loss" in t["reason"] or "stop" in t["reason"])
                             / len(pnls), 1),
        "equity_curve": [round(x, 0) for x in _cum(pnls)],
    }


def _cum(xs):
    tot, out = 0, []
    for x in xs:
        tot += x
        out.append(tot)
    return out


# ------------------------------------------------------------ replay
def replay_spreads(symbol, name, params=None, days=None, log=lambda m: None):
    """Walk each stored chain day; evaluate entries every 15 min with the
    REAL strategies.evaluate (params injected); manage exits per minute."""
    p = params or get_params(name, symbol)
    cfg = config.load()
    lot = cfg["lot_sizes"].get(symbol, 75)
    fee = cfg.get("fee_per_lot", 40) * 4
    trades = []
    for day in (days or _completed_days(history.chain_days(symbol))):
        frames = history.day_chain_frames(symbol, day)
        if len(frames) < 30:
            continue
        open_sp, last_eval = None, 0
        for ts, chain in frames:
            analysis = _an.analyze(chain)
            if open_sp:
                pnl_ps = 0
                ok = True
                for leg in open_sp["legs"]:
                    row = next((r for r in chain["rows"]
                                if r["strike"] == leg["strike"]), None)
                    ltp = row and row[leg["leg"].lower()].get("ltp")
                    if not ltp:
                        ok = False
                        break
                    d = (leg["entry"] - ltp) if leg["action"] == "SELL" else (ltp - leg["entry"])
                    pnl_ps += d
                if ok:
                    reason = None
                    if pnl_ps >= open_sp["credit"] * p["profit_capture"]:
                        reason = "profit target"
                    elif pnl_ps <= -min(open_sp["credit"] * p["loss_mult"],
                                        open_sp["max_loss"]):
                        reason = "loss limit"
                    elif ((open_sp["legs"][0]["leg"] == "PE" and
                           chain["spot"] < open_sp["short"]) or
                          (open_sp["legs"][0]["leg"] == "CE" and
                           chain["spot"] > open_sp["short"])):
                        reason = "strike breach"
                    elif ts == frames[-1][0]:
                        reason = "EOD"
                    if reason:
                        trades.append({"day": day, "strategy": name,
                                       "pnl": round(pnl_ps * lot - fee, 0),
                                       "risk": open_sp["max_loss"] * lot,
                                       "reason": reason})
                        open_sp = None
                continue
            if ts - last_eval < 900:
                continue
            last_eval = ts
            # inject tunable params into the real evaluator
            slib_ev = _eval_with_params(name, analysis, p)
            if slib_ev and slib_ev.get("eligible"):
                open_sp = {"legs": [dict(l, entry=l["ltp"])
                                    for l in slib_ev["legs"]],
                           "credit": slib_ev["credit"],
                           "max_loss": slib_ev["max_loss"],
                           "short": slib_ev["short_strike"]}
        log(f"  {day}: cumulative trades {len(trades)}")
    return trades


def _eval_with_params(name, analysis, p):
    """Call strategies.evaluate with a permissive regime, then re-apply the
    tunable entry filters from params (wall gap, credit fraction)."""
    ev = slib.evaluate(name, analysis, {"regime": "rangebound"})
    if not ev or not ev.get("eligible"):
        return ev
    strikes = sorted(s["strike"] for s in analysis["strikes"])
    gap = strikes[1] - strikes[0]
    spot, wall = analysis["spot"], ev["short_strike"]
    if abs(spot - wall) < gap * p["wall_gap_frac"]:
        return {"eligible": False}
    if ev["credit"] < ev["width"] * p["credit_min_frac"]:
        return {"eligible": False}
    return ev


def replay_momentum(symbol, params=None, days=None, log=lambda m: None):
    """Rule-engine momentum buying: ATM entry when bias+state align,
    exits per SL/T1/T2/trailing — same maths as the live engine."""
    p = params or get_params("momentum_buy", symbol)
    cfg = config.load()
    lot = cfg["lot_sizes"].get(symbol, 75)
    fee = cfg.get("fee_per_lot", 40) * 2
    trades = []
    for day in (days or _completed_days(history.chain_days(symbol))):
        frames = history.day_chain_frames(symbol, day)
        if len(frames) < 30:
            continue
        pos, day_count = None, 0
        for ts, chain in frames:
            analysis = _an.analyze(chain)
            if pos:
                row = next((r for r in chain["rows"]
                            if r["strike"] == pos["strike"]), None)
                ltp = row and row[pos["leg"]].get("ltp")
                if not ltp:
                    continue
                pos["peak"] = max(pos["peak"], ltp)
                if pos["peak"] >= pos["entry"] * p["trail_trigger"]:
                    pos["sl"] = max(pos["sl"], pos["peak"] * (1 - p["trail_gap"]))
                reason = None
                if ltp <= pos["sl"]:
                    reason = "stoploss/trail"
                elif ltp >= pos["t2"]:
                    reason = "target-2"
                elif ts == frames[-1][0]:
                    reason = "EOD"
                if reason:
                    trades.append({"day": day, "strategy": "momentum_buy",
                                   "pnl": round((ltp - pos["entry"]) * lot - fee, 0),
                                   "risk": (pos["entry"] - pos["entry"] * p["sl_frac"]) * lot,
                                   "reason": reason})
                    pos = None
                continue
            if day_count >= 3 or analysis["confidence"] < p["min_confidence"]:
                continue
            bias, state = analysis["bias"], analysis["market_state"]
            leg = "ce" if "BULL" in bias else "pe" if "BEAR" in bias else None
            if not leg or not state.startswith("TRENDING"):
                continue
            row = next((r for r in chain["rows"]
                        if r["strike"] == analysis["atm"]), None)
            entry = row and row[leg].get("ltp")
            if not entry:
                continue
            pos = {"strike": analysis["atm"], "leg": leg, "entry": entry,
                   "peak": entry, "sl": entry * p["sl_frac"],
                   "t2": entry * p["t2_frac"]}
            day_count += 1
        log(f"  {day}: cumulative trades {len(trades)}")
    return trades


def _resample(c1, mins):
    out = []
    for i in range(0, len(c1), mins):
        chunk = c1[i:i + mins]
        out.append({"open": chunk[0]["open"], "close": chunk[-1]["close"],
                    "high": max(x["high"] for x in chunk),
                    "low": min(x["low"] for x in chunk)})
    return out


def replay_pa(symbol, name, params=None, days=None, log=lambda m: None):
    """Replay a price-action strategy over stored index 1m days.
    Option P&L approximated as 0.5-delta ATM buy on the spot move
    (stated approximation; validated live via paper trades)."""
    p = params or get_params(name, symbol)
    cfg = config.load()
    lot = cfg["lot_sizes"].get(symbol, 75)
    fee = cfg.get("fee_per_lot", 40) * 2
    trades = []
    for day in (days or _completed_days(history.index_days(symbol))):
        c1 = history.day_index_candles(symbol, day)
        if len(c1) < 60:
            continue
        # Precompute once per day (O(n)) instead of letting evaluate()
        # recompute anchor/EMA over the growing window on every single
        # minute (O(n²)) — this is the change that took the backtest
        # from ~35 minutes to a small fraction of that.
        closes_full = [c["close"] for c in c1]
        ema_periods = set()
        if name == "vwap_pullback":
            ema_periods.add(int(p.get("resume_ema", 9)))
        elif name == "ema_mtf":
            ema_periods.add(int(p.get("fast", 9)))
            ema_periods.add(int(p.get("slow", 20)))
        precomputed = {"anchor": pa._anchor(c1),
                      "ema": {n: pa._ema(closes_full, n) for n in ema_periods}}
        pos, taken = None, 0
        for i in range(10, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                d = pos["dir"]
                if not pos["t1_done"] and d * (spot - pos["t1"]) >= 0:
                    pos["t1_done"] = True
                    pos["stop"] = pos["entry"]          # breakeven trail
                hit_stop = d * (spot - pos["stop"]) <= 0
                hit_t2 = d * (spot - pos["t2"]) >= 0
                eod = i == len(c1) - 1
                if hit_stop or hit_t2 or eod:
                    pts = d * (spot - pos["entry"])
                    trades.append({"day": day, "strategy": name,
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": "stop" if hit_stop else
                                             ("target-2" if hit_t2 else "EOD")})
                    pos = None
                continue
            ev = pa.evaluate(name, win,
                             _resample(win, 5), _resample(win, 15),
                             params=p, taken_today=taken,
                             precomputed=precomputed)
            if ev:
                pos = {"dir": ev["dir"], "entry": ev["entry_spot"],
                       "stop": ev["stop_spot"], "stop0": ev["stop_spot"],
                       "t1": ev["t1_spot"], "t2": ev["t2_spot"],
                       "t1_done": False}
                taken += 1
    return trades


def run_all(symbol, log=lambda m: None):
    """Backtest every strategy on all stored days → results dict."""
    out = {}
    for name in ("bull_put_spread", "bear_call_spread"):
        log(f"[backtest] {name} on {symbol}")
        out[name] = metrics(replay_spreads(symbol, name, log=log))
    log(f"[backtest] momentum_buy on {symbol}")
    out["momentum_buy"] = metrics(replay_momentum(symbol, log=log))
    for name in pa.PA_NAMES:
        log(f"[backtest] {name} on {symbol}")
        out[name] = metrics(replay_pa(symbol, name, log=log))
    return out
