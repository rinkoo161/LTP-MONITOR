"""backtester.py — replays the REAL analyzer/strategy code over locally
stored chain history. No proxies: the same wall ranking, spread entry
filters and exit rules that trade live are what get tested.

Strategy parameters are read through get_params()/versioning so retuned
versions can be validated before deployment (requirements 8-11).
"""
import json
import os
import store
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
import agents as _ag
import config
import analyzer as _an
import strategies as slib
import pa_strategies as pa
import ta_elliott as _ta

VERS_PATH = store.path("strategy_versions.json")

DEFAULT_PARAMS = {
    # wall_gap_frac raised 0.8 -> 2.0 on 2026-07-23. At 0.8 the short
    # strike only needed to be 0.8 of ONE strike gap from spot (just 40
    # pts on NIFTY, 80 on SENSEX) — inside normal intraday range, so the
    # short strike was breached constantly. Live data (2026-07-16..23):
    # "short strike breached" was the single worst exit category —
    # 4 trades, 0% win rate, -Rs3190 net (avg -Rs798, worse than any
    # other exit). 2.0 requires a genuine 2-strike buffer.
    #
    # credit_min_frac raised 0.15 -> 0.28 on 2026-07-24 — data-driven,
    # see SPREAD_BOUNDS in strategies.py for the full analysis. Real
    # spreads at 15-22% credit fraction were producing 4-5.6:1 risk:
    # reward against the trader; 0.28 sits toward the upper-middle of
    # the new (0.25, 0.40) bounds range.
    "bull_put_spread":  {"wall_gap_frac": 2.0, "credit_min_frac": 0.28,
                         "profit_capture": 0.60, "loss_mult": 1.5},
    "bear_call_spread": {"wall_gap_frac": 2.0, "credit_min_frac": 0.28,
                         "profit_capture": 0.60, "loss_mult": 1.5},
    "momentum_buy":     {"sl_frac": 0.70, "t1_frac": 1.60, "t2_frac": 2.05,
                         "min_confidence": 70, "trail_trigger": 1.05,
                         "trail_gap": 0.10},
    **{n: dict(p) for n, p in pa.PA_DEFAULTS.items()},
    # v58.29 — Strategy 9 lives in its own module and its own agent, so
    # it is registered here EXPLICITLY rather than via pa.PA_DEFAULTS.
    # Deliberate: adding it to PA_NAMES would put it into pa_enabled
    # and make PriceActionAgent evaluate it, which is exactly the
    # duplication this separation exists to avoid.
    "ta_elliott": dict(_ta.TA_ELLIOTT_DEFAULTS),
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
    params = None
    for ver in entry["versions"]:
        if ver["v"] == active:
            params = {**DEFAULT_PARAMS.get(name, {}), **ver["params"]}
            break
    if params is None:
        params = dict(DEFAULT_PARAMS.get(name, {}))
    return _clamp_to_current_bounds(name, params)


def _clamp_to_current_bounds(name, params):
    """Bug found 2026-07-24 from a live regression: raising a bounds
    floor in code (e.g. wall_gap_frac 0.4->1.5 on 2026-07-23, to stop
    short-strike breaches) only changes what FUTURE auto-tuning steps
    can move TOWARD — it does nothing for a version that was already
    tuned/persisted to an out-of-bounds value BEFORE the fix landed.
    get_params() was returning that stale value verbatim forever,
    since tune()'s bounds are only consulted during a tuning STEP, not
    on every read. Confirmed live: "short strike breached" recurred as
    the day's 3 largest losses (-3983/-1200/-748) a full day after the
    wall_gap_frac fix, on symbol/strategy combos whose persisted
    version predated it. This clamps every value against its current
    bounds on every read, regardless of what tune() did or didn't do —
    the single point everything (auto-deploy, backtests, PA strategies)
    goes through, so a stale persisted value can never silently outlive
    a bounds fix again."""
    bounds = (slib.SPREAD_BOUNDS.get(name) or pa.PA_BOUNDS.get(name)
              or _ta.TA_ELLIOTT_BOUNDS.get(name))
    if not bounds:
        return params
    clamped = dict(params)
    for key, (lo, hi, _relax_dir) in bounds.items():
        if key in clamped and isinstance(clamped[key], (int, float)) \
                and not isinstance(clamped[key], bool):
            new_val = min(hi, max(lo, clamped[key]))
            if new_val != clamped[key]:
                clamped[key] = new_val
    return clamped


def gate_verdict(name, symbol, versions=None):
    """(passes, detail) for one strategy/symbol under promotion_gate.

    Split out from is_live_enabled() so the dashboard and the reports can
    show WHY without re-deriving it — a second derivation is how the
    quadrant classifier and the market-session check both drifted.
    """
    import promotion_gate
    v = versions if versions is not None else load_versions()
    entry = (v.get(name, {}).get("symbols", {}) or {}).get(symbol)
    if not entry:
        return False, {"reason": "no backtest entry"}
    m = {}
    for ver in entry.get("versions") or []:
        if ver.get("v") == entry.get("active"):
            m = ver.get("results") or {}
    return promotion_gate.evaluate_entry(name, symbol, m)


def is_live_enabled(name, symbol):
    """May this strategy send a REAL order?

    v59.0 item 17 (2026-08-01) — this used to be nothing but a read of
    `live_enabled`, which the learning agent set from

        profitable_now = trades >= min_conf and net_pnl > 0

    A bare sign test on a P&L series carrying a systematic cost bias and
    an sd of ₹1,143/trade. It promoted 11 of 11 strategies to live; under
    a margin scaled to both error sources, 0 of 11 clear it, and not one
    of the eleven reaches t=2 against its own trade variance. So the sign
    test was not measuring edge — it was measuring noise, and promoting
    on it.

    The statistical gate is now an ADDITIONAL requirement, ANDed with
    what was here before. It can only ever withhold live permission,
    never grant it: a strategy still needs `live_enabled` and no manual
    disable, exactly as before.

    THIS DOES NOT STOP THE STRATEGIES. Both call sites are guarded by
    `not cfg["paper_mode"]`, so paper trading is untouched and all eleven
    keep running and generating the data that will eventually settle the
    question. Killing them would destroy the only route to an answer.
    """
    v = load_versions()
    entry = v.get(name, {}).get("symbols", {}).get(symbol)
    if not (entry and entry.get("live_enabled")
            and not entry.get("manually_disabled")):
        return False
    try:
        ok, _ = gate_verdict(name, symbol, v)
    except Exception:
        # An unavailable gate must not silently re-open live trading.
        # Failing closed is the only safe direction here, and this is a
        # deliberate exception to the house rule that a broken auxiliary
        # never blocks a path.
        return False
    return bool(ok)


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
        # v59.0 item 17 — the promotion gate needs the strategy's OWN
        # per-trade dispersion, and it must be the real thing. Derived
        # from win_rate/avg_win/avg_loss it is a LOWER bound (it ignores
        # dispersion inside each bucket), which would make every gate
        # decision optimistic in exactly the direction that promotes.
        "pnl_sd": round(statistics.pstdev(pnls), 0) if len(pnls) > 1 else 0.0,
        "max_win": round(max(pnls), 0),
        "max_loss": round(min(pnls), 0),
        "avg_risk_per_trade": round(statistics.mean(
            [abs(t.get("risk", 0)) for t in trades]), 0),
        "sl_hit_rate": round(100 * sum(1 for t in trades
                             if "loss" in t["reason"] or "stop" in t["reason"])
                             / len(pnls), 1),
        "equity_curve": [round(x, 0) for x in _cum(pnls)],
        # 2026-07-26 (v55) — for the Backtest-page chart overlay. Kept
        # deliberately lean (not the full trade dict) since this can
        # accumulate to hundreds of trades over a multi-year backtest;
        # only what a chart marker needs to be plotted and labeled.
        "trades_detail": [
            {"entry_ts": t.get("entry_ts"), "exit_ts": t.get("exit_ts"),
             "entry_spot": t.get("entry_spot"), "exit_spot": t.get("exit_spot"),
             "pnl": t["pnl"], "reason": t["reason"], "day": t["day"]}
            for t in trades if t.get("entry_ts") and t.get("exit_ts")
        ],
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
                    _tot = pnl_ps * open_sp["qty"]
                    open_sp["mfe"] = max(open_sp.get("mfe", 0), _tot)
                    open_sp["mae"] = min(open_sp.get("mae", 0), _tot)
                    # 2026-08-06 — THE SAME decision function the live
                    # monitor uses. This block previously modelled four
                    # exits (profit target / loss limit / strike breach
                    # / EOD) while the live path ALSO ran the defense
                    # zone, the per-share profit-lock ratchet,
                    # rupee_profit_floor and the time stop.
                    # rupee_profit_floor appeared 8 times in agents.py
                    # and ZERO times here.
                    #
                    # So this replay measured a DIFFERENT STRATEGY from
                    # the one running, and said so on the same days:
                    #     FINNIFTY bull_put  replay n=15 -3,081  47%
                    #                        LIVE   n=17 +4,702  82%
                    # Every promotion decision and tuner sweep for
                    # spreads inherited that error.
                    #
                    # market_is_open is False only on the final frame,
                    # which is what "EOD square-off" means in a replay.
                    reason = _ag.spread_exit_reason(
                        open_sp, pnl_ps, chain.get("spot"), cfg,
                        now_ts=ts, market_is_open=(ts != frames[-1][0]))
                    if reason:
                        trades.append({"day": day, "strategy": name,
                                       "pnl": round(pnl_ps * lot - fee, 0),
                                       "risk": open_sp["max_loss"] * lot,
                                       "reason": reason,
                                       # 2026-07-26 (v55) — added for the
                                       # Backtest-page chart overlay.
                                       # `ts`/`chain["spot"]` were ALREADY
                                       # available at every step of this
                                       # loop (needed for the entry/exit
                                       # logic itself) — this only stores
                                       # values that already existed,
                                       # it's not new computation.
                                       "entry_ts": open_sp["entry_ts"],
                                       "exit_ts": ts,
                                       "entry_spot": open_sp["entry_spot"],
                                       "exit_spot": chain["spot"]})
                        open_sp = None
                continue
            if ts - last_eval < 900:
                continue
            last_eval = ts
            # inject tunable params into the real evaluator
            slib_ev = _eval_with_params(name, analysis, p)
            if slib_ev and slib_ev.get("eligible"):
                # Shape it the way the LIVE spread dict is shaped, because
                # spread_exit_reason reads these by name. Reproducing the
                # SHAPE of a structure instead of its MEANING has silently
                # zeroed out whole strategies in this codebase before, so
                # these are the live field names, not replay-local ones.
                _credit = slib_ev["credit"]
                open_sp = {"legs": [dict(l, entry=l["ltp"])
                                    for l in slib_ev["legs"]],
                           "credit": _credit,
                           "max_loss": slib_ev["max_loss"],
                           "short": slib_ev["short_strike"],
                           "short_strike": slib_ev["short_strike"],
                           "width": slib_ev.get("width")
                                    or abs(slib_ev["legs"][0]["strike"]
                                           - slib_ev["legs"][1]["strike"]),
                           "symbol": symbol, "strategy": name,
                           "qty": lot,
                           "profit_target": _credit * p["profit_capture"],
                           "loss_limit": min(_credit * p["loss_mult"],
                                             slib_ev["max_loss"]),
                           "opened_ts": ts, "mfe": 0.0, "mae": 0.0,
                           "entry_ts": ts, "entry_spot": chain["spot"]}
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
                                   "reason": reason,
                                   "entry_ts": pos["entry_ts"], "exit_ts": ts,
                                   "entry_spot": pos["entry_spot"], "exit_spot": chain["spot"]})
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
                   "t2": entry * p["t2_frac"],
                   "entry_ts": ts, "entry_spot": chain["spot"]}
            day_count += 1
        log(f"  {day}: cumulative trades {len(trades)}")
    return trades


def _resample(c1, mins):
    out = []
    for i in range(0, len(c1), mins):
        chunk = c1[i:i + mins]
        # v58.31 — carry the bucket's opening timestamp. Purely
        # additive (replay_pa has never read these), but REQUIRED by
        # anything that needs to locate a resampled bar in time:
        # structure.zigzag_series() keys pivots by "time", and
        # ta_elliott.compute_state() keys its cache on the last
        # candle's time+close. Without it Strategy 9's replay raised
        # KeyError on the first bar — a gap that only surfaced against
        # the real resampler, never against synthetic fixtures.
        out.append({"open": chunk[0]["open"], "close": chunk[-1]["close"],
                    "high": max(x["high"] for x in chunk),
                    "low": min(x["low"] for x in chunk),
                    "time": chunk[0].get("time", chunk[0].get("ts", i)),
                    "ts": chunk[0].get("ts", chunk[0].get("time", i))})
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
        c1 = history.day_index_candles(symbol, day, for_compute=True)
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
                                             ("target-2" if hit_t2 else "EOD"),
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": spot})
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
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": ev["entry_spot"]}
                taken += 1
    return trades



def replay_ew_reversal(symbol, params=None, days=None, log=lambda m: None):
    """v58.31 — Strategy 8 replay. Separate from replay_pa() because
    ew_reversal is not dispatched by pa.evaluate() (it has its own
    module and its own signature), so replay_pa() silently produced
    ZERO trades for it — which meant is_live_enabled() could never
    return True and S8 could never graduate past paper no matter how
    it performed.

    NO-LOOKAHEAD: pivots are recomputed from the TRUNCATED window on
    every bar, so the detector only ever sees pivots ZigZag had already
    confirmed as of that bar. Computing them once over the full day and
    indexing in would hand the detector pivots from its own future and
    manufacture a backtest that cannot be reproduced live.
    """
    import ew_reversal, structure
    p = params or get_params("ew_reversal", symbol)
    cfg = config.load()
    lot = cfg["lot_sizes"].get(symbol, 75)
    fee = cfg.get("fee_per_lot", 40) * 2
    dev = cfg.get("s8_zigzag_deviation_pct", 0.5)
    trades = []
    for day in (days or _completed_days(history.index_days(symbol))):
        c1 = history.day_index_candles(symbol, day, for_compute=True)
        if len(c1) < 60:
            continue
        pos, taken = None, 0
        for i in range(30, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                d = pos["dir"]
                if not pos["t1_done"] and d * (spot - pos["t1"]) >= 0:
                    pos["t1_done"] = True
                    pos["stop"] = pos["entry"]
                hit_stop = d * (spot - pos["stop"]) <= 0
                hit_t2 = d * (spot - pos["t2"]) >= 0
                eod = i == len(c1) - 1
                if hit_stop or hit_t2 or eod:
                    pts = d * (spot - pos["entry"])
                    trades.append({"day": day, "strategy": "ew_reversal",
                                   "subtype": pos.get("subtype"),
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": "stop" if hit_stop else
                                             ("target-2" if hit_t2 else "EOD"),
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": spot})
                    pos = None
                continue
            pivots = structure.zigzag_series(win, dev)
            ev, _det = ew_reversal.evaluate(win, _resample(win, 5),
                                            _resample(win, 15), params=p,
                                            taken_today=taken, pivots=pivots)
            if ev:
                pos = {"dir": ev["dir"], "entry": ev["entry_spot"],
                       "stop": ev["stop_spot"], "stop0": ev["stop_spot"],
                       "t1": ev["t1_spot"], "t2": ev["t2_spot"],
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": ev["entry_spot"],
                       "subtype": ev.get("setup_subtype")}
                taken += 1
    return trades


def replay_ta_elliott(symbol, params=None, days=None, log=lambda m: None):
    """v58.31 — Strategy 9 replay. Same reasoning and same no-lookahead
    discipline as replay_ew_reversal above.

    compute_state() is memoised on the last 5m/15m candle (timestamp
    AND close), so the twelve GMMA EMAs and the rest are rebuilt only
    when the underlying candle actually changes rather than on all
    ~375 bars of the day.
    """
    import ta_elliott as _tae, structure
    p = params or get_params("ta_elliott", "_global")
    cfg = config.load()
    lot = cfg["lot_sizes"].get(symbol, 75)
    fee = cfg.get("fee_per_lot", 40) * 2
    dev = p.get("zigzag_deviation_pct", 0.5)
    trades = []
    for day in (days or _completed_days(history.index_days(symbol))):
        c1 = history.day_index_candles(symbol, day, for_compute=True)
        if len(c1) < 120:
            continue
        pos, taken = None, 0
        for i in range(60, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                d = pos["dir"]
                if not pos["t1_done"] and d * (spot - pos["t1"]) >= 0:
                    pos["t1_done"] = True
                    pos["stop"] = pos["entry"]
                hit_stop = d * (spot - pos["stop"]) <= 0
                hit_t2 = d * (spot - pos["t2"]) >= 0
                eod = i == len(c1) - 1
                if hit_stop or hit_t2 or eod:
                    pts = d * (spot - pos["entry"])
                    trades.append({"day": day, "strategy": "ta_elliott",
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": "stop" if hit_stop else
                                             ("target-2" if hit_t2 else "EOD"),
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": spot})
                    pos = None
                continue
            c5w, c15w = _resample(win, 5), _resample(win, 15)
            if len(c5w) < int(p.get("bb_period", 20)) + 3:
                continue
            state = _tae.compute_state(symbol, win, c5w, c15w, params=p)
            pivots = structure.zigzag_series(win, dev)
            ev, _conf = _tae.evaluate(state, win, params=p,
                                      taken_today=taken, pivots=pivots)
            if ev:
                pos = {"dir": ev["dir"], "entry": ev["entry_spot"],
                       "stop": ev["stop_spot"], "stop0": ev["stop_spot"],
                       "t1": ev["t1_spot"], "t2": ev["t2_spot"],
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": ev["entry_spot"]}
                taken += 1
    return trades


def _replay_for(name, symbol, params, days=None):
    """Dispatch to the correct replay function for any strategy name —
    used by sweep_params() so it works identically across spreads/
    momentum/PA without the caller needing to know which loop backs
    which strategy."""
    if name in ("bull_put_spread", "bear_call_spread"):
        return replay_spreads(symbol, name, params=params, days=days)
    if name == "momentum_buy":
        return replay_momentum(symbol, params=params, days=days)
    if name == "ew_reversal":
        return replay_ew_reversal(symbol, params=params, days=days)
    if name == "ta_elliott":
        return replay_ta_elliott(symbol, params=params, days=days)
    return replay_pa(symbol, name, params=params, days=days)


def replay_futures(symbol, name, params=None, days=None, log=lambda m: None):
    """Futures replay, following the replay_* convention so _replay_for()
    and sweep_params() can reach it.

    Delegates to futures_replay.py — the research harness lives outside
    this module because this module drives is_live_enabled(), and Phase A
    produces no live path. Uses the notional cost model, never
    fee_per_lot: Phase 0 measured the flat model understating futures
    cost ~10x, and it charged 40 real trades exactly zero.
    """
    import futures_costs
    import futures_replay as fr
    warn = futures_costs.warn_if_flat_cost_model(log=log)
    if warn:
        log("  (futures replay uses the notional model regardless)")
    return fr.replay_futures(symbol, name, params, days, log)


def warn_if_costs_disabled(log=lambda m: None):
    """Loud warning when `fee_per_lot` is 0.

    2026-07-29 — found on a live install. Every replay in this module
    costs trades at `cfg.get("fee_per_lot", 40)`, and `is_live_enabled()`
    gates real trading on replay profitability. With fees at zero every
    backtest overstates profit and a strategy can be promoted to live on
    numbers that assume trading is free. On a 10-lot spread that is
    ₹1,600 per round trip missing. Fails LOUD rather than silently
    producing flattering numbers.
    """
    fee = config.load().get("fee_per_lot", 40)
    if not fee:
        msg = ("fee_per_lot is 0 — every backtest below costs trades at "
               "ZERO and will overstate profit. is_live_enabled() reads "
               "these numbers, so a strategy could be promoted to live "
               "on free-trading assumptions. Set fee_per_lot in Settings.")
        log("WARNING: " + msg)
        return msg
    return None


def _bounds_for(name):
    import strategies as slib
    import pa_strategies as pa
    return (slib.SPREAD_BOUNDS.get(name) or pa.PA_BOUNDS.get(name)
            or _ta.TA_ELLIOTT_BOUNDS.get(name) or {})


def sweep_params(name, symbol, log=lambda m: None, candidates_per_param=3):
    """On-demand parameter OPTIMIZER — genuinely searches for a better
    value per tunable parameter, rather than the daily auto-tuner's
    single greedy nudge (_tune_pa/_revalidate in agents.py relax OR
    tighten one bounded step per day based on a simple trade-frequency/
    profitability trigger — it never actually tries several candidate
    values and compares results). Added specifically because a backtest
    run only re-tests the CURRENTLY ACTIVE parameters — it validates
    the existing response, it doesn't search for the best one.

    Coordinate-wise (not a full combinatorial grid, which would explode
    with 3-5 tunable params each needing its own multi-day chain
    replay): holds all params at the current best, varies ONE parameter
    at a time across `candidates_per_param` evenly-spaced points across
    its documented bound range, keeps whichever single change most
    improves net P&L, then moves to the next parameter using THAT as
    the new baseline. One pass over all parameters — a second pass
    would let param interactions get chased further, but at real
    backtest-replay cost per candidate; one pass already finds a
    genuinely searched optimum, not a random single guess.

    Returns {"baseline_params", "baseline_metrics", "tried" (every
    candidate attempted, for transparency — this is what "identify the
    best value" should actually show, not just the winner), "best_params",
    "best_metrics", "improved" (bool, using the SAME meaningful_improvement
    bar the daily tuner already uses, so a sweep result and a daily-
    tuner result are held to the identical standard)}.
    """
    p = get_params(name, symbol)
    bounds = _bounds_for(name)
    days = _completed_days(
        history.chain_days(symbol) if name in ("bull_put_spread", "bear_call_spread")
        else history.index_days(symbol, 250))
    baseline_trades = _replay_for(name, symbol, p, days)
    baseline_m = metrics(baseline_trades)
    baseline_pnl = baseline_m.get("net_pnl", 0) if baseline_m.get("trades") else -float("inf")
    best_params, best_pnl = dict(p), baseline_pnl
    tried = [{"params": dict(p), "net_pnl": (None if baseline_pnl == -float("inf") else baseline_pnl),
             "trades": baseline_m.get("trades", 0), "label": "baseline (current active version)"}]
    log(f"[sweep] {name}/{symbol} baseline: {baseline_m.get('trades', 0)} trades, "
        f"net {baseline_m.get('net_pnl', 0)}")
    for key, (lo, hi, _relax_dir) in bounds.items():
        cur_val = p.get(key)
        if not isinstance(cur_val, (int, float)) or isinstance(cur_val, bool):
            continue   # skip non-numeric / boolean flags — nothing to sweep across a range
        step_n = max(2, candidates_per_param)
        candidates = [round(lo + (hi - lo) * i / (step_n - 1), 4) for i in range(step_n)]
        for cand in candidates:
            if abs(cand - best_params.get(key, cand)) < 1e-9:
                continue
            trial = dict(best_params)
            trial[key] = cand
            log(f"[sweep] {name}/{symbol}: trying {key}={cand}")
            m = metrics(_replay_for(name, symbol, trial, days))
            pnl = m.get("net_pnl", 0) if m.get("trades") else None
            tried.append({"params": dict(trial), "net_pnl": pnl,
                         "trades": m.get("trades", 0), "label": f"{key}={cand}"})
            if pnl is not None and pnl > best_pnl:
                best_pnl, best_params = pnl, trial
    best_metrics = (metrics(_replay_for(name, symbol, best_params, days))
                    if best_params != p else baseline_m)
    improved = meaningful_improvement(
        baseline_m.get("net_pnl", 0) if baseline_m.get("trades") else 0, best_pnl)
    log(f"[sweep] {name}/{symbol} done: {len(tried)} candidates tried, "
        f"best net {best_pnl} ({'IMPROVED' if improved else 'no meaningful improvement'})")
    return {"baseline_params": p, "baseline_metrics": baseline_m,
           "tried": tried, "best_params": best_params, "best_metrics": best_metrics,
           "improved": improved}


def audit_today(name, symbol, real_trades, log=lambda m: None,
               match_tolerance_sec=300):
    """v58.9 (item 6) — retroactive candle-by-candle audit for TODAY,
    per explicit request repeated multiple times ("Apply Loop -
    Analysis, learn and adopt", "backtest for each candle and each
    strategy to identify the gap"). Replays the EXACT strategy rules
    against today's own archived data (reusing `_replay_for()` and
    `get_params()` from the v56 optimizer, scoped via `days=[today]` —
    both already existed and needed no changes), then compares that
    idealized rule-replay against what ACTUALLY happened live today —
    surfacing three kinds of gap:

      - `missed_by_live`: the pure rules found a valid setup today, but
        no real trade was ever entered near that time — could be a
        cooldown/gate/margin constraint (working as intended) OR a
        genuine timing/data-lag issue worth investigating further.
      - `unexpected_in_live`: a real trade happened today that the pure
        rules, replayed against the same day, would NOT have entered —
        could be an AI-influenced decision, a since-changed parameter,
        or a genuine live-vs-backtest data discrepancy.
      - `matched`: real and replayed trades that align — the system
        behaved as its own rules say it should have.

    Matching is by entry-time proximity (default 5 minutes) rather than
    exact equality, since a live entry and its backtest-replay
    counterpart won't share the identical tick.

    `real_trades` is the caller's own list of already-loaded closed
    trades (not fetched here) — this function does no I/O against the
    live trade log itself, keeping it a pure, testable function; the
    caller (LearningAgent's daily cycle, or an on-demand API call)
    supplies whatever trade list is appropriate.

    Returns {"day", "backtest_trades", "backtest_metrics", "real_trades",
    "real_net_pnl", "matched", "missed_by_live", "unexpected_in_live",
    "gap_summary"}.
    """
    today = _now_ist_date()
    real_today = [t for t in (real_trades or [])
                 if t.get("closed_date") == today and
                 t.get("symbol") == symbol and
                 (t.get("strategy") or t.get("source")) == name]

    params = get_params(name, symbol)
    bt_trades = _replay_for(name, symbol, params, days=[today])
    bt_metrics = metrics(bt_trades)

    matched, missed = [], []
    used_real_idx = set()
    for bt in bt_trades:
        bt_entry = bt.get("entry_ts")
        found = None
        if bt_entry is not None:
            for i, rt in enumerate(real_today):
                if i in used_real_idx:
                    continue
                rt_ts = rt.get("opened_ts")
                if rt_ts is not None and abs(rt_ts - bt_entry) <= match_tolerance_sec:
                    found = i
                    break
        if found is not None:
            matched.append({"backtest": bt, "real": real_today[found]})
            used_real_idx.add(found)
        else:
            missed.append(bt)
    unexpected = [rt for i, rt in enumerate(real_today) if i not in used_real_idx]

    real_net_pnl = round(sum(t.get("pnl", 0) for t in real_today), 0)
    bt_net_pnl = bt_metrics.get("net_pnl", 0) if bt_metrics.get("trades") else 0
    gap = round(bt_net_pnl - real_net_pnl, 0)
    gap_summary = (f"backtest replay: {bt_metrics.get('trades', 0)} trade(s), "
                  f"net \u20b9{bt_net_pnl:.0f} | real: {len(real_today)} trade(s), "
                  f"net \u20b9{real_net_pnl:.0f} | gap \u20b9{gap:.0f} | "
                  f"{len(missed)} rule-valid setup(s) live never took | "
                  f"{len(unexpected)} live trade(s) the rules wouldn't have made")
    log(f"[audit] {symbol} {name} {today}: {gap_summary}")

    return {"day": today, "backtest_trades": bt_trades, "backtest_metrics": bt_metrics,
           "real_trades": real_today, "real_net_pnl": real_net_pnl,
           "matched": matched, "missed_by_live": missed,
           "unexpected_in_live": unexpected, "gap_summary": gap_summary}


def _now_ist_date():
    import agents
    return agents.now_ist().strftime("%Y-%m-%d")


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
