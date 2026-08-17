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


def _completed_days(day_list, symbol=None, kind="opt", log=None):
    """Days that are BOTH finished and fully recorded.

    Exclude today's IST calendar date: the archives write live during
    market hours, so today shows up as a "day" long before the session
    closes. Replaying it treats the latest captured frame as EOD and
    force-exits on partial, still-changing data — a spurious low-sample
    trade row that doesn't reflect a real closed day.

    v59.81 extends the same principle from "not finished yet" to "not
    fully recorded". A host sleep on 2026-08-13 left a 3.6-hour hole in
    the middle of live trading; the day would otherwise have been
    replayed tomorrow as a complete session and counted as one full
    independent day toward the promotion gate's 10-day requirement. A
    day with a hole in it is not a completed day.

    The test is RELATIVE (a fraction of the median day for that symbol),
    never an absolute bar count: contract sizes, strike counts and
    session lengths all change, and a fixed threshold would silently
    rot. Passing no `symbol` keeps the pre-v59.81 behaviour exactly, so
    callers that genuinely want every day are unaffected.

    Every exclusion is LOGGED. A day silently vanishing from the
    evidence base is the same class of problem as a partial day
    silently entering it."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    days = [d for d in day_list if d != today]
    if not symbol or not days:
        return days
    try:
        import config as _cfg
        pct = float(_cfg.load().get("partial_day_min_coverage_pct", 60)) / 100.0
        cov = history.day_bar_coverage(symbol, kind)
        # The norm must come from days that HAVE data. Snapshot retention
        # prunes old days to zero, and including those zeros halves the
        # median — measured on the real archive, six 0-bar July days
        # dragged NIFTY's median to 166, which is exactly what the
        # 2026-08-13 incident day recorded, so the day this filter exists
        # to catch would have passed. Zero-coverage days are excluded
        # too, but for a different reason and with a different message:
        # they cannot be replayed at all, which is not the same as being
        # thin.
        present = sorted(v for v in (cov.get(d, 0) for d in days) if v > 0)
        if len(present) < 3:
            return days          # too few real days to have a norm
        median = present[len(present) // 2]
        floor = median * pct
        kept, thin, empty = [], [], []
        for d in days:
            n = cov.get(d, 0)
            if n <= 0:
                empty.append(d)
            elif n < floor:
                thin.append(d)
            else:
                kept.append(d)
        dropped = thin + empty
        if log and thin:
            log(f"[coverage] {symbol}/{kind}: excluding {len(thin)} "
                f"UNDER-RECORDED day(s) vs median {median} — "
                + ", ".join(f"{d} ({cov.get(d, 0)})" for d in thin[:8])
                + (" …" if len(thin) > 8 else ""))
        if log and empty:
            log(f"[coverage] {symbol}/{kind}: {len(empty)} day(s) have NO "
                f"retained data (pruned or never captured) — "
                + ", ".join(empty[:8]) + (" …" if len(empty) > 8 else ""))
        # Never return an empty set because of this filter: a symbol
        # whose whole archive is thin should still be replayable, just
        # visibly so. Losing every day would look like "no signals".
        return kept or days
    except Exception:
        return days              # coverage is a QUALITY check, never a blocker

import statistics
import collections
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
    # 2026-08-06 — profit_capture 0.60 -> 0.18 and loss_mult 1.5 -> 1.0,
    # SEEDED AT WHAT LIVE ACTUALLY USES. These two were the only tuner
    # params live never read: strategies.evaluate() fetches
    # wall_gap_frac/credit_min_frac via get_params (so those WERE
    # connected), but enter_spread computed its target from
    # cfg["spread_profit_target_pct"] (18%) and its limit from
    # cfg["spread_loss_limit_multiple"] (1.0), ignoring these entirely.
    #
    # So the tuner swept 60%/1.5 while live ran 18%/1.0 — a 3.3x
    # difference in when a spread takes profit, and the reason replay
    # spreads rode to "market closing" while live's turned over in
    # minutes.
    #
    # Reseeding to the live values rather than pointing live at 0.60
    # is deliberate: switching live to a 60% profit target would be a
    # large, unmeasured behaviour change in the direction the replay
    # showed LOSING money. Deploy changes nothing today; the tuner can
    # now move these, within bounds, on evidence.
    "bull_put_spread":  {"wall_gap_frac": 2.0, "credit_min_frac": 0.28,
                         "profit_capture": 0.18, "loss_mult": 1.0},
    "bear_call_spread": {"wall_gap_frac": 2.0, "credit_min_frac": 0.28,
                         "profit_capture": 0.18, "loss_mult": 1.0},
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


def version_worthy(old_pnl, new_pnl, threshold=0.15):
    """May a candidate configuration become a persisted VERSION?

    v59.67, operator requirement (2026-08-09): a version exists only when
    the changed configuration produced a POSITIVE net result that
    meaningfully improves on the incumbent — never for a smaller loss,
    never for an equal or worse positive. meaningful_improvement() alone
    accepted "loss shrank 15%", and every path that used it minted a
    version for it: the live install accumulated 85 versions of which 67
    had non-positive results — 4 MB of dead weight parsed by every
    get_params() call.

    Refusing the version loses no information: the candidate's
    evaluation is already in trial_log (N counts configurations TRIED,
    not configurations kept), and the tuner's own attempt/cooldown
    bookkeeping still advances on refusal.
    """
    if new_pnl is None or new_pnl <= 0:
        return False
    return meaningful_improvement(old_pnl or 0, new_pnl, threshold)


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
    # v59.66 — per-day P&L sums, for the promotion gate's clustered
    # sampling term. Same-day trades on one index share one day's regime,
    # so the DAY is the independent observation, not the trade (third-eye
    # Tier 1: 313 trades over 17 days is ~17 observations, not 313).
    by_day = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0) + t["pnl"]
    daily_vals = list(by_day.values())
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
        # v59.66 — dispersion of the per-DAY sums (see by_day above). The
        # gate forms its standard error from this, not pnl_sd, so that
        # correlated same-day trades cannot inflate the evidence count.
        "pnl_sd_day": (round(statistics.pstdev(daily_vals), 0)
                       if len(daily_vals) > 1 else 0.0),
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
    # 2026-08-06, PHASE 1b — mirror the LIVE entry admission rules.
    # v59.36 made the EXIT decision shared, and the replay still
    # disagreed with live (+4,702 vs -3,477 on FINNIFTY). The exits were
    # no longer the difference; the ENTRIES were. `_auto_spreads` gates
    # every entry on five rules and this replay had NONE of them:
    #
    #   60s evaluation gate         replay used 900s -> 15x fewer entries
    #   max_concurrent_spreads      replay held ONE at a time
    #   per-(sym,name) cooldown     replay had none
    #   consecutive-loss halt       replay had none
    #   max_spread_capital_pct      see the caveat below
    #
    # Read from the SAME config keys the live agent reads, so a change
    # in Settings moves both together instead of only one.
    eval_gap = 60
    max_open = int(cfg.get("max_concurrent_spreads", 2))
    cooldown = cfg.get("spread_reentry_cooldown_min", 15) * 60
    stop_n = cfg.get("spread_stop_after_consecutive_losses", 2)
    for day in (days or _completed_days(history.chain_days(symbol),
                                    symbol, "opt", log)):
        # expiry= and as_of= (2026-08-08) — without them this replay ran
        # on a chain blended across every expiry on record (231 of 244
        # NIFTY strikes exist in more than one, and frames are keyed by
        # strike alone, so premiums overwrote each other) and analyze()
        # measured days_to_expiry from TODAY rather than from `day`,
        # which zeroed IV and greeks on every historical frame. See
        # v59.53 in ROADMAP.md.
        frames = history.day_chain_frames(
            symbol, day, expiry=history.front_expiry_on(symbol, day))
        if len(frames) < 30:
            continue
        open_list, last_eval = [], 0
        cd_until, consec = 0, 0
        for ts, chain in frames:
            analysis = _an.analyze(chain, as_of=day)
            for open_sp in list(open_list):
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
                        # 2026-08-06 — premium-aware, matching the LIVE
                        # path. The flat model understates an options
                        # round trip by Rs 26-47/symbol because it omits
                        # the bid-ask spread entirely, and a 2-leg
                        # spread crosses that spread FOUR times.
                        _fee = _ag.realistic_fees(
                            "option", symbol, 1, open_sp["credit"],
                            _ag.spread_exit_value(open_sp["credit"], pnl_ps),
                            cfg, legs=2)
                        trades.append({"day": day, "strategy": name,
                                       "pnl": round(pnl_ps * lot - _fee, 0),
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
                        open_list.remove(open_sp)
                        # Consecutive-loss halt and the re-entry
                        # cooldown are LIVE gates; a replay without them
                        # keeps re-entering a losing setup all day and
                        # reports a drawdown live could never have taken.
                        if (trades[-1]["pnl"] or 0) < 0:
                            consec += 1
                        else:
                            consec = 0
            if ts - last_eval < eval_gap:
                continue
            last_eval = ts
            if not _runway_ok(ts, cfg):
                continue          # v59.78: no runway to the target
            if len(open_list) >= max_open:
                continue
            if ts < cd_until:
                continue
            if stop_n and consec >= stop_n:
                continue
            # inject tunable params into the real evaluator
            slib_ev = _eval_with_params(
                name, analysis, p,
                regime=historical_regime(symbol, ts),
                candles=history.candles_before(
                    f"{symbol}_SPOT_5m", ts, limit=400) or None)
            if slib_ev and slib_ev.get("eligible"):
                # Shape it the way the LIVE spread dict is shaped, because
                # spread_exit_reason reads these by name. Reproducing the
                # SHAPE of a structure instead of its MEANING has silently
                # zeroed out whole strategies in this codebase before, so
                # these are the live field names, not replay-local ones.
                _credit = slib_ev["credit"]
                # LIVE stamps the cooldown on successful ENTRY:
                #     r = self.enter_spread(ev)
                #     if r.get("ok"): self._spread_cd[cd_key] = time.time()
                # The first cut of this stamped it on EXIT instead, which
                # let the replay open TEN clones of the same spread
                # back-to-back before any closed — visible as the same
                # "short strike breached (spot NNNNN)" reason repeating
                # exactly 10 times. Entry-stamped is what live does and
                # it is what naturally limits concurrency per pair.
                cd_until = ts + cooldown
                open_list.append({"legs": [dict(l, entry=l["ltp"])
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
                        # 2026-08-06 phase 1d — LIVE reads
                        # spread_profit_target_pct (18%) and
                        # spread_loss_limit_multiple (1.0). The tuner's
                        # profit_capture (60%) / loss_mult (1.5) appear
                        # ONLY in backtester.py and strategy_docs.py —
                        # agents.py reads NEITHER. Using them here made
                        # the replay demand 3.3x more profit than live
                        # before taking it, so replay spreads rode to
                        # "market closing" holding slots while live's
                        # turned over in minutes ("captured ₹6.5 of
                        # ₹33.45" = 19%). Read what LIVE reads.
                           # Tuned value, which now MEANS the same thing
                           # live means by spread_profit_target_pct.
                           "profit_target": round(_credit * p["profit_capture"], 2),
                           "loss_limit": round(min(_credit * p["loss_mult"],
                                                   slib_ev["max_loss"]), 2),
                           "opened_ts": ts, "mfe": 0.0, "mae": 0.0,
                           "entry_ts": ts, "entry_spot": chain["spot"]})
        log(f"  {day}: cumulative trades {len(trades)}")
    return trades


def historical_regime(sym, as_of_ts, _cache={}):
    """The regime as the LIVE classifier would have seen it at `as_of_ts`.

    2026-08-06, phase 1d. `_eval_with_params` called
    `slib.evaluate(name, analysis, {"regime": "rangebound"})` — a
    HARDCODED permissive regime with no candles — while live passes the
    real regime and `regime_candles:{sym}`. Eligibility was therefore
    decided on different inputs, which is why the portfolio replay took
    2.9 spreads/day against live's 7.0 and no conclusion could be drawn
    about either.

    This does NOT reimplement the classifier. `RegimeAgent._classify`
    reaches the market only through `self._fetch_candles(d, sym, tf)`,
    so feeding that method archived bars runs the ENTIRE live
    classification — ADX, ATR, opening-range expansion, multi-timeframe
    alignment, the warm-up rules — over history. A second
    implementation would drift, which is the failure this codebase has
    already had with the market-session check, the news regexes and the
    OI quadrant classifier.

    Bars are truncated at `as_of_ts`, so the replay cannot see candles
    that had not printed yet. Cached per (symbol, 5-minute bucket)
    because `_classify` is not cheap and the regime does not change
    within a bar.
    """
    bucket = int(as_of_ts // 300)
    ck = (sym, bucket)
    if ck in _cache:
        return _cache[ck]

    class _ArchiveRegime(_ag.RegimeAgent):
        def _fetch_candles(self, d, sym_, tf):
            # The SAME reader RegimeAgent's own market-closed fallback
            # uses — history.candles_before(f"{sym}_SPOT_{tf}m", ...) —
            # so the replay reads the identical rows the live agent
            # would have read, truncated at as_of_ts.
            return history.candles_before(
                f"{sym_}_SPOT_{tf}m", as_of_ts, limit=400) or []

    try:
        agent = _ArchiveRegime(_ag.Bus(), {})
        r = agent._classify(sym, None)
    except Exception as e:                       # pragma: no cover
        r = {"regime": "unknown", "error": f"{type(e).__name__}: {e}"}
    _cache[ck] = r
    return r


def replay_portfolio(symbols=None, names=None, days=None, log=lambda m: None):
    """Walk ALL symbols and strategies TOGETHER against ONE shared slot
    count and ONE shared capital pool — the way live actually runs.

    2026-08-06, phase 1c. `replay_spreads(symbol, name)` walks a single
    pair in isolation, so `max_concurrent_spreads` (10 slots) and
    `max_spread_capital_pct` (60% of capital) get applied as though that
    pair owned the entire book. Measured consequence:

        LIVE     7.0 spreads/day across the WHOLE book
        per-pair replay  ~17/day for FINNIFTY bull_put ALONE

    Ten times the setups live could ever have taken. Those extra trades
    were net NEGATIVE, which raised the hypothesis this function exists
    to test: that live's profitability comes from being CAPACITY
    CONSTRAINED — the caps acting as an accidental filter — rather than
    from setup quality. If so, "trade more" loses money.

    Exits go through agents.spread_exit_reason, the same function the
    live monitor calls (v59.36). Entry admission mirrors _auto_spreads:
    60s evaluation gate, portfolio slot count, per-(symbol,strategy)
    re-entry cooldown, consecutive-loss halt, and the capital cap
    computed on the SAME basis live uses
    (margin_per_lot_spread x lots vs backtest_capital).
    """
    cfg = config.load()
    symbols = symbols or ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
    names = names or ["bull_put_spread", "bear_call_spread"]
    eval_gap = 60
    max_open = int(cfg.get("max_concurrent_spreads", 2))
    cooldown = cfg.get("spread_reentry_cooldown_min", 15) * 60
    stop_n = cfg.get("spread_stop_after_consecutive_losses", 2)
    capital = cfg.get("backtest_capital", 200000)
    margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
    max_cap_pct = cfg.get("max_spread_capital_pct", 60.0)
    fee_per = cfg.get("fee_per_lot", 40) * 4

    day_sets = [set(history.chain_days(s)) for s in symbols]
    all_days = sorted(_completed_days(sorted(set.union(*day_sets))))
    if days:
        all_days = [d for d in all_days if d in set(days)]

    trades, skipped = [], collections.Counter()
    for day in all_days:
        frames = {}
        for sym in symbols:
            # see the note in replay_spreads() — one expiry per day, and
            # analyze() below counts from `day`, not from today.
            for ts, chain in history.day_chain_frames(
                    sym, day, expiry=history.front_expiry_on(sym, day)):
                frames.setdefault(ts, {})[sym] = chain
        stamps = sorted(frames)
        if len(stamps) < 30:
            continue
        open_list, cd, consec, last_eval = [], {}, collections.Counter(), 0
        for ts in stamps:
            here = frames[ts]
            # ---- manage every open spread first (slots free before entries)
            for sp in list(open_list):
                chain = here.get(sp["symbol"])
                if not chain:
                    continue
                pnl_ps, ok = 0.0, True
                for leg in sp["legs"]:
                    row = next((r for r in chain["rows"]
                                if r["strike"] == leg["strike"]), None)
                    ltp = row and row[leg["leg"].lower()].get("ltp")
                    if not ltp:
                        ok = False
                        break
                    pnl_ps += ((leg["entry"] - ltp) if leg["action"] == "SELL"
                               else (ltp - leg["entry"]))
                if not ok:
                    continue
                tot = pnl_ps * sp["qty"]
                sp["mfe"] = max(sp.get("mfe", 0), tot)
                sp["mae"] = min(sp.get("mae", 0), tot)
                reason = _ag.spread_exit_reason(
                    sp, pnl_ps, chain.get("spot"), cfg,
                    now_ts=ts, market_is_open=(ts != stamps[-1]))
                if reason:
                    _fee = _ag.realistic_fees(
                        "option", sp["symbol"], 1, sp["credit"],
                        _ag.spread_exit_value(sp["credit"], pnl_ps),
                        cfg, legs=2)
                    pnl = round(tot - _fee, 0)
                    trades.append({"day": day, "symbol": sp["symbol"],
                                   "strategy": sp["strategy"], "pnl": pnl,
                                   "reason": reason, "entry_ts": sp["opened_ts"],
                                   "exit_ts": ts})
                    open_list.remove(sp)
                    k = f"{sp['symbol']}:{sp['strategy']}"
                    consec[k] = consec[k] + 1 if pnl < 0 else 0
            # ---- then consider new entries, against the SHARED limits
            if ts - last_eval < eval_gap:
                continue
            last_eval = ts
            if not _runway_ok(ts, cfg):
                continue          # v59.78: no runway to the target
            for sym in symbols:
                chain = here.get(sym)
                if not chain:
                    continue
                analysis = _an.analyze(chain, as_of=day)
                for name in names:
                    if len(open_list) >= max_open:
                        skipped["max_concurrent"] += 1
                        continue
                    k = f"{sym}:{name}"
                    if ts - cd.get(k, 0) < cooldown:
                        skipped["on_cooldown"] += 1
                        continue
                    if stop_n and consec[k] >= stop_n:
                        skipped["consec_loss_halt"] += 1
                        continue
                    # Recomputed from the CURRENT open list every time —
                    # live had a bug where a stale pre-cycle figure let
                    # several spreads each pass the same check.
                    deployed = sum(margin_per_lot * (x.get("lots") or 1)
                                   for x in open_list)
                    if capital > 0 and (deployed / capital * 100) >= max_cap_pct:
                        skipped["capital_concentration"] += 1
                        continue
                    pp = get_params(name, sym)
                    ev = _eval_with_params(
                        name, analysis, pp,
                        regime=historical_regime(sym, ts),
                        candles=history.candles_before(
                            f"{sym}_SPOT_5m", ts, limit=400) or None)
                    if not (ev and ev.get("eligible")):
                        continue
                    lot = cfg["lot_sizes"].get(sym, 75)
                    credit = ev["credit"]
                    open_list.append({
                        "legs": [dict(l, entry=l["ltp"]) for l in ev["legs"]],
                        "credit": credit, "max_loss": ev["max_loss"],
                        "short_strike": ev["short_strike"],
                        "width": ev.get("width") or abs(ev["legs"][0]["strike"]
                                                        - ev["legs"][1]["strike"]),
                        "symbol": sym, "strategy": name, "qty": lot, "lots": 1,
                        # See the note in replay_spreads: LIVE reads
                        # spread_profit_target_pct / spread_loss_limit_multiple.
                        "profit_target": round(credit * pp["profit_capture"], 2),
                        "loss_limit": round(min(credit * pp["loss_mult"],
                                                ev["max_loss"]), 2),
                        "opened_ts": ts, "mfe": 0.0, "mae": 0.0})
                    cd[k] = ts        # live stamps on ENTRY, not on exit
        log(f"  {day}: cumulative {len(trades)}")
    return {"trades": trades, "skipped": dict(skipped), "days": all_days}


def _eval_with_params(name, analysis, p, regime=None, candles=None):
    """Call strategies.evaluate, then re-apply the tunable entry filters
    from params (wall gap, credit fraction).

    2026-08-06, phase 1d — `regime` used to be HARDCODED to
    {"regime": "rangebound"} with no candles, while live passes the real
    regime and `regime_candles:{sym}`. Eligibility was decided on
    different inputs from live, which is why the portfolio replay took
    2.9 spreads/day against live's 7.0 and neither number could be
    trusted against the other. Callers now pass what
    backtester.historical_regime() reconstructs from archived candles
    through the LIVE classifier.

    The permissive default remains ONLY for callers that genuinely have
    no timestamp context; it is no longer what the replays use.
    """
    # params=p (2026-08-08) — this is what makes the docstring above
    # true. Without it evaluate() re-read the persisted version and
    # gated on THAT, so `p` could only ever tighten the result further;
    # a relaxing candidate was unmeasurable. The re-checks below are now
    # a strict no-op when evaluate() got the same `p`, and are kept as a
    # backstop for the default-params path.
    ev = slib.evaluate(name, analysis, regime or {"regime": "rangebound"},
                       candles=candles, params=p)
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
    for day in (days or _completed_days(history.chain_days(symbol),
                                    symbol, "opt", log)):
        # see the note in replay_spreads() — same two defects.
        frames = history.day_chain_frames(
            symbol, day, expiry=history.front_expiry_on(symbol, day))
        if len(frames) < 30:
            continue
        pos, day_count = None, 0
        for ts, chain in frames:
            analysis = _an.analyze(chain, as_of=day)
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


def _runway_ok(ts, cfg):
    """v59.78 — mirror of the live entry-runway guard
    (agents.minutes_to_squareoff): the replays must refuse the same
    late entries live refuses, or the tuner learns from trades live
    cannot take. `ts` is the frame/bar epoch timestamp."""
    import agents as _ag
    return _ag.minutes_to_squareoff(ts, cfg) >= int(
        cfg.get("min_entry_runway_min", 30) or 0)


def _edge_ok_pa(ev, lot, fee, cfg):
    """Tier 2 feasibility for the spot-proxy replays (v59.73): designed
    gross = |t1 − entry| × 0.5Δ × lot against this replay's own
    round-trip cost. Same ratio and config key as the live gates
    (edge_feasibility.min_ratio) — the admission bar must match live or
    the replay measures a different strategy (the 2026-08-06 lesson,
    entry-side)."""
    import edge_feasibility as ef
    designed = abs(float(ev.get("t1_spot") or 0)
                   - float(ev.get("entry_spot") or 0)) * 0.5 * lot
    return ef.feasible(designed, fee, cfg)[0]


def _atm_premium_at(symbol, ts, spot, leg, _cache={}):
    """Real archived ATM premium for `symbol` at `ts`, or None.

    v59.88. Reads the same `chain_snapshots` archive the spread replay
    walks, so the number is observed rather than assumed. Cached per
    (symbol, 5-min bucket) because the PA replays evaluate every bar and
    the chain only moves every 60s.
    """
    if not (ts and spot):
        return None
    key = (symbol, int(ts) // 300, leg)
    if key in _cache:
        return _cache[key]
    try:
        import history
        snap = history.get_chain_snapshot_map(symbol, int(ts))
        strikes = sorted({k for (k, _lg) in (snap or {})})
        prem = None
        if strikes:
            atm = min(strikes, key=lambda k: abs(k - float(spot)))
            prem = ((snap.get((atm, leg)) or {}).get("ltp")) or None
    except Exception:
        prem = None
    _cache[key] = prem
    return prem


def _reachable_ok_pa(ev, symbol, ts, cfg):
    """v59.88 — replay/live parity for the v59.86 reachability gate.

    The live gate states the target as a percentage of PREMIUM; these
    replays are spot-proxy and carry only `entry_spot`/`t1_spot`.
    Applying the live threshold to spot values would compare a spot
    quantity against a premium one — the exact dimensional bug
    analyzer.option_stop_geometry's comment documents, where every
    "volatility-based" stop turned out to be the clamp.

    So instead of approximating the gate, look the REAL premium up from
    the chain archive and call the SAME function live calls. The spot
    move is translated at the same 0.5 delta `_edge_ok_pa` already uses
    for costs, so both admission checks share one convention.

    Fails OPEN when the archive has no chain for that moment (pruned or
    never captured): a replay must not invent a refusal live would not
    have made. Those days are logged by the coverage filter already.
    """
    entry_spot = float(ev.get("entry_spot") or 0)
    t1_spot = float(ev.get("t1_spot") or 0)
    if not (entry_spot > 0 and t1_spot > 0):
        return True
    leg = "ce" if t1_spot > entry_spot else "pe"
    prem = _atm_premium_at(symbol, ts, entry_spot, leg)
    if not prem or prem <= 0:
        return True
    # The strategy's OWN spot target is not what live gates. PriceAction
    # and MTF signals are routed through analyzer.option_stop_geometry
    # (agents.py, "routed through analyzer.option_stop_geometry()"),
    # which DISCARDS the spot target and rebuilds target1 on the premium
    # as entry x (1 + stop_pct x 2). Gating the strategy's spot target
    # would therefore check a target live never uses — measured at 3% of
    # entries blocked here against 73% of live signals, which is what
    # exposed the mistake. Build the same geometry from the same inputs.
    import analyzer as _an
    import edge_feasibility as ef
    reg = historical_regime(symbol, ts) or {}
    _sl, _t1, _t2, _meta = _an.option_stop_geometry(
        prem, cfg=cfg, atr_pct=reg.get("atr_pct"), spot=entry_spot)
    if not _t1:
        return True
    return ef.target_reachable(prem, _t1, cfg)[0]


def _bar_exit(pos, bar, is_last):
    """Intrabar exit resolution for the spot-proxy replays (v59.69,
    third-eye Tier 3). The old test compared CLOSES only: a bar that
    pierced the stop and closed back inside did not stop out, a bar
    that tagged T2 and closed back did not take profit, and every exit
    filled at the close rather than the level — highs/lows were
    computed by _resample and never consulted. Rules, matching
    futures_replay's documented conservatism:

      * detection uses the bar's high/low, not its close;
      * a bar that spans BOTH stop and target is charged as the STOP —
        within one minute there is no way to know which printed first,
        and assuming the target is how a backtest flatters itself;
      * a T1 ratchet earned from THIS bar's own extremes takes effect
        from the NEXT bar — crediting a same-bar T1-then-breakeven
        escape would assume the favourable print came first, the same
        flattery in a different coat;
      * stop/target exits fill AT THE LEVEL; only EOD fills at the close.

    Returns (exit_px, reason) or (None, None). Mutates pos's t1/stop
    ratchet exactly once per bar — call it once.
    """
    d = pos["dir"]
    hi = bar.get("high", bar["close"])
    lo = bar.get("low", bar["close"])
    fav = hi if d > 0 else lo
    adv = lo if d > 0 else hi
    stop_now = pos["stop"]              # before any ratchet from this bar
    if not pos["t1_done"] and d * (fav - pos["t1"]) >= 0:
        pos["t1_done"] = True
        pos["stop"] = pos["entry"]      # breakeven trail — from next bar
    if d * (adv - stop_now) <= 0:
        return stop_now, "stop"
    if d * (fav - pos["t2"]) >= 0:
        return pos["t2"], "target-2"
    if is_last:
        return bar["close"], "EOD"
    return None, None


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
    for day in (days or _completed_days(history.index_days(symbol),
                                    symbol, "idx", log)):
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
        pos, pending, taken = None, None, 0
        for i in range(10, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": name,
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            if pending is not None:
                # v59.69 — fill ONE BAR AFTER the signal, at this bar's
                # open. The signal was computed from the previous bar's
                # close; filling at that same close assumed transacting
                # on the print that generated the signal (third-eye
                # Tier 1/3 lookahead item). Levels stay as designed at
                # signal time; the entry price is what was gettable.
                fill = win[-1].get("open", spot)
                pos = {"dir": pending["dir"], "entry": fill,
                       "stop": pending["stop_spot"], "stop0": pending["stop_spot"],
                       "t1": pending["t1_spot"], "t2": pending["t2_spot"],
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": fill}
                pending = None
                taken += 1
                # v59.72 (R2 finding M3) — inspect the FILL BAR's own
                # range immediately: a stop pierced on the fill bar was
                # carried unobserved (one flattering bar per trade), and
                # a fill on the day's last bar vanished with no EOD exit.
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": name,
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            if name == "sg_ema":
                # v59.66 — sg_ema is NOT dispatched by pa.evaluate() (it
                # has its own evaluate_sg_ema with a pivots argument), so
                # this loop silently produced ZERO trades for it — the
                # same bug replay_ew_reversal's docstring records for S8,
                # surviving here because run_all() called this function
                # for all six PA names. Pivots are recomputed from the
                # TRUNCATED window per bar (no lookahead), default zigzag
                # deviation — the identical call the live S7 loop makes
                # (agents.py `structure.zigzag_series(pack["c1"])`).
                # ai_bias is None, exactly live's state before the bias
                # engine has computed — that gate reports "skipped", so
                # the replay admits ≥ live entries on that one gate.
                import structure
                pivots = structure.zigzag_series(win)
                ev, _gates = pa.evaluate_sg_ema(
                    win, _resample(win, 5), _resample(win, 15),
                    params=p, taken_today=taken, pivots=pivots)
            else:
                ev = pa.evaluate(name, win,
                                 _resample(win, 5), _resample(win, 15),
                                 params=p, taken_today=taken,
                                 precomputed=precomputed)
            if ev and not _edge_ok_pa(ev, lot, fee, cfg):
                ev = None             # Tier 2: designed edge below cost
            if ev and not _reachable_ok_pa(ev, symbol, win[-1]["ts"], cfg):
                ev = None             # v59.86 parity: target unreachable
            if ev and not _runway_ok(win[-1]["ts"], cfg):
                ev = None             # v59.78: no runway to the target
            if ev:
                pending = ev          # fills next bar — see above
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
    for day in (days or _completed_days(history.index_days(symbol),
                                    symbol, "idx", log)):
        c1 = history.day_index_candles(symbol, day, for_compute=True)
        if len(c1) < 60:
            continue
        pos, pending, taken = None, None, 0
        for i in range(30, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": "ew_reversal",
                                   "subtype": pos.get("subtype"),
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            if pending is not None:
                # v59.69 — next-bar-open fill; see replay_pa.
                fill = win[-1].get("open", spot)
                pos = {"dir": pending["dir"], "entry": fill,
                       "stop": pending["stop_spot"], "stop0": pending["stop_spot"],
                       "t1": pending["t1_spot"], "t2": pending["t2_spot"],
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": fill,
                       "subtype": pending.get("setup_subtype")}
                pending = None
                taken += 1
                # v59.72 (R2 finding M3) — inspect the FILL BAR's own
                # range immediately: a stop pierced on the fill bar was
                # carried unobserved (one flattering bar per trade), and
                # a fill on the day's last bar vanished with no EOD exit.
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": "ew_reversal",
                                   "subtype": pos.get("subtype"),
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            pivots = structure.zigzag_series(win, dev)
            ev, _det = ew_reversal.evaluate(win, _resample(win, 5),
                                            _resample(win, 15), params=p,
                                            taken_today=taken, pivots=pivots)
            if ev and not _edge_ok_pa(ev, lot, fee, cfg):
                ev = None             # Tier 2: designed edge below cost
            if ev and not _reachable_ok_pa(ev, symbol, win[-1]["ts"], cfg):
                ev = None             # v59.86 parity: target unreachable
            if ev and not _runway_ok(win[-1]["ts"], cfg):
                ev = None             # v59.78: no runway to the target
            if ev:
                pending = ev          # fills next bar
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
    for day in (days or _completed_days(history.index_days(symbol),
                                    symbol, "idx", log)):
        c1 = history.day_index_candles(symbol, day, for_compute=True)
        if len(c1) < 120:
            continue
        pos, pending, taken = None, None, 0
        for i in range(60, len(c1)):
            win = c1[:i + 1]
            spot = win[-1]["close"]
            if pos:
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": "ta_elliott",
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            if pending is not None:
                # v59.69 — next-bar-open fill; see replay_pa.
                fill = win[-1].get("open", spot)
                pos = {"dir": pending["dir"], "entry": fill,
                       "stop": pending["stop_spot"], "stop0": pending["stop_spot"],
                       "t1": pending["t1_spot"], "t2": pending["t2_spot"],
                       "t1_done": False, "entry_ts": win[-1]["ts"],
                       "entry_spot": fill}
                pending = None
                taken += 1
                # v59.72 (R2 finding M3) — inspect the FILL BAR's own
                # range immediately: a stop pierced on the fill bar was
                # carried unobserved (one flattering bar per trade), and
                # a fill on the day's last bar vanished with no EOD exit.
                exit_px, why = _bar_exit(pos, win[-1], i == len(c1) - 1)
                if exit_px is not None:
                    pts = pos["dir"] * (exit_px - pos["entry"])
                    trades.append({"day": day, "strategy": "ta_elliott",
                                   "pnl": round(pts * 0.5 * lot - fee, 0),
                                   "risk": abs(pos["entry"] - pos["stop0"]) * 0.5 * lot,
                                   "reason": why,
                                   "entry_ts": pos["entry_ts"], "exit_ts": win[-1]["ts"],
                                   "entry_spot": pos["entry_spot"], "exit_spot": exit_px})
                    pos = None
                continue
            c5w, c15w = _resample(win, 5), _resample(win, 15)
            if len(c5w) < int(p.get("bb_period", 20)) + 3:
                continue
            state = _tae.compute_state(symbol, win, c5w, c15w, params=p)
            pivots = structure.zigzag_series(win, dev)
            ev, _conf = _tae.evaluate(state, win, params=p,
                                      taken_today=taken, pivots=pivots)
            if ev and not _edge_ok_pa(ev, lot, fee, cfg):
                ev = None             # Tier 2: designed edge below cost
            if ev and not _reachable_ok_pa(ev, symbol, win[-1]["ts"], cfg):
                ev = None             # v59.86 parity: target unreachable
            if ev and not _runway_ok(win[-1]["ts"], cfg):
                ev = None             # v59.78: no runway to the target
            if ev:
                pending = ev          # fills next bar
    return trades


def _replay_for(name, symbol, params, days=None, source="replay"):
    """Dispatch to the correct replay function for any strategy name —
    used by sweep_params() so it works identically across spreads/
    momentum/PA without the caller needing to know which loop backs
    which strategy.

    2026-08-08 — this is now also the ONE place a parameter set is
    recorded to trial_log. It has to be one place: the deflated Sharpe
    in Part 4 of the strategy-reset memo needs N, the count of
    configurations evaluated, and N was unrecoverable precisely because
    evaluations happened down several paths and only ACCEPTED results
    were ever persisted. LearningAgent's daily tuner used to call
    replay_spreads()/replay_pa() directly and would have been invisible
    here; those two call sites now come through this function instead,
    so a future strategy family cannot be added to the search without
    also being counted.
    """
    if name in ("bull_put_spread", "bear_call_spread"):
        out = replay_spreads(symbol, name, params=params, days=days)
    elif name == "momentum_buy":
        out = replay_momentum(symbol, params=params, days=days)
    elif name == "ew_reversal":
        out = replay_ew_reversal(symbol, params=params, days=days)
    elif name == "ta_elliott":
        out = replay_ta_elliott(symbol, params=params, days=days)
    else:
        out = replay_pa(symbol, name, params=params, days=days)
    try:
        import trial_log
        trial_log.record(name, symbol, params, metrics(out), source)
    except Exception as e:
        # Reported, never swallowed — a recorder that silently stops
        # working recreates exactly the gap this exists to close.
        print(f"  ⚠ trial_log skipped for {name}/{symbol}: "
              f"{type(e).__name__}: {e}")
    return out


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

    # 2026-08-17 — the audit runs from LearningAgent's EOD cycle
    # (~15:35) but today's option candles are synced by BacktestAgent's
    # LATER job (sync_day_chain, ~15:45+). So every daily audit replayed
    # a day with no data, got zero replay trades, and reported every
    # live trade as one "the rules wouldn't have made". Confirmed live
    # 2026-08-17: at 15:48 the audit said 0 replay trades / 1 rogue live
    # trade; after the sync the same code said 11 replay trades and the
    # live 12:34 entry MATCHED a rule-valid 12:31 setup. The audit was
    # manufacturing a divergence out of an empty table, daily.
    #
    # Zero-because-no-data must be distinguishable from zero-because-
    # rules-rejected — and "the data" differs by strategy family. The
    # first cut checked chain_days() unconditionally; test_audit_today
    # immediately caught that this brands every PA/momentum audit
    # insufficient, because those replays run on INDEX candles, which
    # chain_days() (option candles) says nothing about. The existence
    # check must mirror _replay_for's actual input per family.
    import history as _h
    import strategies as _st
    if name in getattr(_st, "META", {}):
        # spread replays reconstruct the option chain for the day
        missing = today not in set(_h.chain_days(symbol))
        what = "day chain"
    else:
        # PA/momentum replays run on the index candle series
        _c = _h._conn()
        n_idx = _c.execute(
            """SELECT COUNT(*) FROM candles c JOIN instruments i
               ON i.security_id=c.security_id
               WHERE i.symbol=? AND i.kind='idx'
               AND date(c.ts,'unixepoch','+5 hours','+30 minutes')=?""",
            (symbol, today)).fetchone()[0]
        _c.close()
        missing = n_idx == 0
        what = "index candles"
    if missing:
        gap_summary = (f"insufficient data — {symbol} {what} for {today} "
                      f"not yet synced (audit ran before the 15:45 sync "
                      f"job); re-run after sync for the real comparison")
        log(f"[audit] {symbol} {name} {today}: {gap_summary}")
        return {"day": today, "backtest_trades": [], "backtest_metrics": {},
               "real_trades": [t for t in (real_trades or [])
                               if t.get("closed_date") == today],
               "real_net_pnl": 0, "matched": [], "missed_by_live": [],
               "unexpected_in_live": [], "insufficient_data": True,
               "gap_summary": gap_summary}

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
    """Backtest every strategy on all stored days → results dict.

    v59.66 (third-eye Tier 1) — two structural fixes:

    * Every replay goes through _replay_for(), not the replay_* functions
      directly. That (a) records each daily baseline evaluation in
      trial_log so N for the deflated Sharpe stops undercounting — this
      function was the one remaining path around the recorder — and
      (b) dispatches ew_reversal/sg_ema to their real engines instead of
      replay_pa, which cannot evaluate them and returned zero-trade
      baselines that agents._tune_pa then wrote over real results.

    * Each metrics dict carries an `oos` sub-dict: the same trades cut to
      days STRICTLY AFTER the active version's adoption date. That is the
      only slice promotion_gate.evaluate_entry() will score — the full-
      sample numbers stay for display, but they are the in-sample optimum
      of the parameter search and must never gate a live order.
    """
    vers = load_versions()
    out = {}
    for name in (["bull_put_spread", "bear_call_spread", "momentum_buy"]
                 + list(pa.PA_NAMES)):
        log(f"[backtest] {name} on {symbol}")
        # v59.74 — per-STRATEGY isolation. One try wrapped the whole
        # symbol at the caller, so ew_reversal's KeyError('time') on
        # 2026-08-09 voided every strategy's results for every symbol —
        # spreads included — and the tuner marched on against empty
        # dicts. A broken replay now yields an ERROR STUB whose
        # trades=None keeps the refresh guard from overwriting real
        # persisted results, and the failure is in the log, not
        # swallowed into an all-or-nothing symbol error.
        try:
            trades = _replay_for(name, symbol, None, source="daily_baseline")
        except Exception as e:
            log(f"[backtest] {name} on {symbol} FAILED: "
                f"{type(e).__name__}: {e}")
            out[name] = {"trades": None,
                         "replay_error": f"{type(e).__name__}: {e}"}
            continue
        m = metrics(trades)
        entry = (vers.get(name, {}).get("symbols", {}) or {}).get(symbol) or {}
        adopted = None
        for ver in entry.get("versions") or []:
            if ver.get("v") == entry.get("active"):
                adopted = (ver.get("created") or "")[:10] or None
                # v59.72 (R2 finding M8) — v1 "initial" carries the
                # shipped DEFAULTS; its auto-stamped created date is
                # entry-creation, not a fit. Treating it as an adoption
                # collapsed a 60-day archive to "3 OOS days" for
                # never-tuned strategies. Defaults were never fitted:
                # every archived day is out-of-sample for them.
                if ver.get("v") == 1 and                         str(ver.get("reason") or "").startswith("initial"):
                    adopted = None
        # No version entry (or no created stamp) means the params are the
        # shipped defaults, never fitted to this archive — every day is
        # out-of-sample for them.
        oos_trades = (trades if not adopted
                      else [t for t in trades if (t.get("day") or "") > adopted])
        oos = metrics(oos_trades)
        for heavy in ("trades_detail", "equity_curve"):
            oos.pop(heavy, None)   # strategy_versions.json is 4 MB already
        oos["window"] = ("all archived days (params never fitted/adopted)"
                         if not adopted else
                         f"days after {adopted} (v{entry.get('active')} adoption)")
        m["oos"] = oos
        out[name] = m
    return out
