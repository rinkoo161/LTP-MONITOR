"""Strategy library — defined-risk credit spreads driven by OI walls.

Phase 1: two strategies, PAPER MODE ONLY.
  bull_put_spread : sell PE at S1 support wall, buy hedge 1-2 strikes below.
                    Works in trending-up and rangebound regimes.
  bear_call_spread: sell CE at R1 resistance wall, buy hedge above.
                    Works in trending-down and rangebound regimes.

Each evaluate() returns None (not eligible) or a spread dict:
  {name, symbol, legs:[{action,leg,strike,ltp,security_id}], credit,
   max_loss, width, short_strike, regime, reasons:[...]}
Amounts are per-share; multiply by qty for rupees.
"""

SPREAD_BOUNDS = {
    # (lo, hi, relax_direction) — relax moves TOWARD the permissive end
    # wall_gap_frac floor raised 0.4 -> 1.5 on 2026-07-23. relax_dir is -1,
    # meaning the auto-tuner pushes this value DOWN whenever it wants more
    # signals — which would have silently undone the 0.8->2.0 default fix
    # and walked straight back into the short-strike-breach zone that
    # produced 0% win rate / -Rs3190 across 4 trades in live data.
    # A hard floor of 1.5 strike gaps keeps the tuner out of that zone.
    #
    # credit_min_frac floor raised 0.08 -> 0.25 on 2026-07-24, same
    # underlying pattern: relax_dir=-1 means the tuner pushes this DOWN
    # too, toward MORE permissive (lower minimum credit) whenever it
    # wants more signals — directly opposite of what's needed. Data-
    # driven: computed the actual risk:reward on 7 real spreads opened
    # live on 2026-07-24, all clustering at 15-22% credit fraction
    # (near the old 0.08-0.15 range) produced 4-5.6:1 risk:reward
    # AGAINST the trader (e.g. NIFTY bear_call: Rs15.2 credit risking
    # Rs84.8, a 5.6:1 ratio) — directly matching the complaint "picking
    # spreads where loss projection is higher compared to profits".
    # The two spreads with a naturally higher credit fraction (35-44%)
    # had a far more reasonable 1.2-1.8:1 ratio. 0.25 is a middle
    # ground: meaningfully better than 0.08-0.15 without demanding the
    # rarer 35%+ setups that would cut trade frequency drastically.
    # 2026-08-06 — profit_capture and loss_mult added to BOUNDS so the
    # tuner can actually sweep them. They were in DEFAULT_PARAMS but not
    # here, and live ignored them entirely (enter_spread used
    # spread_profit_target_pct / spread_loss_limit_multiple).
    #
    # Ranges BRACKET the live values (0.18 / 1.0) rather than reaching
    # for a wider search. relax_dir is -1 for profit_capture: a LOWER
    # target exits sooner and turns over more, which is the permissive
    # direction. It is +1 for loss_mult, where a HIGHER multiple sits a
    # stop further away and lets more trades survive. Both floors are
    # deliberately conservative — these have never been swept against a
    # backtest that matched live, so the first sweeps are exploration,
    # not optimisation.
    "bull_put_spread":  {"wall_gap_frac": (1.5, 4.0, -1), "credit_min_frac": (0.25, 0.40, -1),
                         "profit_capture": (0.10, 0.45, -1), "loss_mult": (0.6, 1.5, 1)},
    "bear_call_spread": {"wall_gap_frac": (1.5, 4.0, -1), "credit_min_frac": (0.25, 0.40, -1),
                         "profit_capture": (0.10, 0.45, -1), "loss_mult": (0.6, 1.5, 1)},
}


def tune(name, params, direction):
    """Bounded relax(+1)/tighten(-1) step for spread entry filters —
    same mechanics as pa_strategies.tune()."""
    from backtester import DEFAULT_PARAMS
    p = dict(params)
    changes = []
    for key, (lo, hi, relax_dir) in SPREAD_BOUNDS.get(name, {}).items():
        step_dir = relax_dir * direction
        cur = p.get(key, DEFAULT_PARAMS[name][key])
        step = (hi - lo) * 0.2 * step_dir
        new = round(min(hi, max(lo, cur + step)), 3)
        if new != cur:
            p[key] = new
            changes.append(f"{key} {cur}->{new}")
    return p, changes


REGIME_FIT = {
    "bull_put_spread":  ("trending-up", "rangebound", "mixed"),
    "bear_call_spread": ("trending-down", "rangebound", "mixed"),
}

META = {
    "bull_put_spread": {
        "title": "Bull Put Spread (OI wall)",
        "desc": ("Sells the PE at the strongest support wall (S1) and buys a "
                 "lower PE as the hedge. Collects premium; profits if the "
                 "index stays above the wall. Time decay works for you."),
        "bias": "neutral-bullish",
    },
    "bear_call_spread": {
        "title": "Bear Call Spread (OI wall)",
        "desc": ("Sells the CE at the strongest resistance wall (R1) and buys "
                 "a higher CE as the hedge. Collects premium; profits if the "
                 "index stays below the wall. Time decay works for you."),
        "bias": "neutral-bearish",
    },
}


def _row(analysis, strike):
    return next((s for s in analysis.get("strikes", [])
                 if s["strike"] == strike), None)


def _wall(analysis, side):
    """First (strongest) signal line level for a side: 'R' or 'S'."""
    lines = (analysis.get("signal_lines") or {}).get(side) or []
    return lines[0]["level"] if lines else None


def evaluate(name, analysis, regime, candles=None, params=None):
    """Return an entry-ready spread or None, with human-readable reasons.

    `candles` (2026-07-27, item 9) — optional 5-minute index candles
    (the SAME `regime_candles:{symbol}` RegimeAgent already fetches
    every cycle for its own regime classification, reused here rather
    than a new API call) enabling an OPTIONAL liquidity-sweep/FVG
    confluence check ON TOP OF the OI-wall selection, per the roadmap's
    own framing. The OI wall stays the PRIMARY mechanism deciding WHICH
    strike to sell — this only asks "does recent price action ALSO
    support this wall," an additional confirmation layer, not a
    replacement. Callers without candle data (backtester replay, older
    call sites) simply pass None and get the exact same behavior as
    before this was added — this parameter is additive, not a breaking
    change to any existing caller.
    """
    if name not in META:
        return None
    reasons, spot = [], analysis.get("spot")
    reg = (regime or {}).get("regime", "unknown")
    if reg not in REGIME_FIT[name]:
        return {"eligible": False,
                "reasons": [f"regime '{reg}' not suited "
                            f"(needs {'/'.join(REGIME_FIT[name])})"]}
    strikes = sorted(s["strike"] for s in analysis.get("strikes", []))
    if len(strikes) < 4 or not spot:
        return {"eligible": False, "reasons": ["not enough chain data"]}
    gap = strikes[1] - strikes[0]
    # `params` (2026-08-08) — when a caller supplies parameters, USE
    # THEM. Until now this function ignored everything the caller had
    # and re-read the PERSISTED version off disk, which quietly broke
    # the auto-tuner: backtester._eval_with_params() advertises that it
    # "inject[s] tunable params into the real evaluator", but it could
    # only re-apply the same two filters AFTERWARDS. A candidate that
    # RELAXED wall_gap_frac was still gated here on the incumbent's
    # tighter value, so relaxation could never show more trades than
    # the incumbent — the search could ratchet tighter and never come
    # back. Measured on NIFTY bear_call_spread, same frames/days:
    # persisted 4.0/0.40 -> 0 of 60 eligible; 1.5/0.25 -> 5 of 60.
    #
    # Two (symbol, strategy) pairs had already reached the restrictive
    # corner of SPREAD_BOUNDS and were producing ~0 trades, which the
    # promotion gate reads as "no edge" when it means "never evaluated".
    #
    # params=None keeps the disk read, so the live agent (agents.py
    # :5122) and both app.py endpoints are byte-identical.
    if params is None:
        try:
            from backtester import get_params
            _p = get_params(name, analysis.get("symbol"))
        except Exception:
            _p = {}
    else:
        _p = params
    wall_gap_frac = _p.get("wall_gap_frac", 2.0)
    credit_min_frac = _p.get("credit_min_frac", 0.28)

    if name == "bull_put_spread":
        wall = _wall(analysis, "S")
        side, hedge_dir = "pe", -1
        if not wall or wall >= spot:
            return {"eligible": False,
                    "reasons": ["no valid S1 support wall below spot"]}
        dist_ok = (spot - wall) >= gap * wall_gap_frac
        reasons.append(f"S1 wall {wall:.0f} is {spot - wall:.0f} pts below spot")
    else:
        wall = _wall(analysis, "R")
        side, hedge_dir = "ce", +1
        if not wall or wall <= spot:
            return {"eligible": False,
                    "reasons": ["no valid R1 resistance wall above spot"]}
        dist_ok = (wall - spot) >= gap * wall_gap_frac
        reasons.append(f"R1 wall {wall:.0f} is {wall - spot:.0f} pts above spot")
    if not dist_ok:
        return {"eligible": False,
                "reasons": reasons + [f"wall too close to spot (< {wall_gap_frac} strike gaps)"]}

    # 2026-07-27 (item 9) — liquidity-sweep/FVG confluence, opt-in
    # (`spread_require_liquidity_confluence`, default off): a genuine
    # new entry requirement, not a bug fix, so it doesn't silently
    # change behavior for anyone already running these strategies.
    import config as _cfg
    cfg = _cfg.load()
    if cfg.get("spread_require_liquidity_confluence", False) and candles:
        import structure
        direction = "support" if name == "bull_put_spread" else "resistance"
        proximity = cfg.get("spread_liquidity_proximity_pct", 0.3)
        confluence = structure.wall_confluence(candles, wall, direction,
                                               proximity_pct=proximity)
        if not confluence["confirmed"]:
            return {"eligible": False,
                    "reasons": reasons + confluence["reasons"]}
        reasons.append("confluence: " + "; ".join(confluence["reasons"]))

    hedge = wall + hedge_dir * gap * 2          # 2 strikes further out
    srow, hrow = _row(analysis, wall), _row(analysis, hedge)
    if not hrow:                                 # fall back to 1 strike out
        hedge = wall + hedge_dir * gap
        hrow = _row(analysis, hedge)
    if not (srow and hrow):
        return {"eligible": False,
                "reasons": reasons + ["hedge strike outside loaded chain"]}
    s_ltp = srow[side].get("ltp") or 0
    h_ltp = hrow[side].get("ltp") or 0
    credit = round(s_ltp - h_ltp, 2)
    width = abs(hedge - wall)
    max_loss = round(width - credit, 2)
    if credit <= 0 or max_loss <= 0:
        return {"eligible": False,
                "reasons": reasons + ["no net credit at current prices"]}
    if credit < width * credit_min_frac:
        return {"eligible": False,
                "reasons": reasons + [f"credit ₹{credit} too thin "
                                      f"(<{credit_min_frac*100:.0f}% of ₹{width:.0f} width)"]}
    reasons.append(f"credit ₹{credit} vs max loss ₹{max_loss} "
                   f"({credit / width * 100:.0f}% of width)")
    return {
        "eligible": True, "name": name, "symbol": analysis["symbol"],
        "legs": [
            {"action": "SELL", "leg": side.upper(), "strike": wall,
             "ltp": s_ltp, "security_id": srow[side].get("security_id")},
            {"action": "BUY", "leg": side.upper(), "strike": hedge,
             "ltp": h_ltp, "security_id": hrow[side].get("security_id")},
        ],
        "credit": credit, "max_loss": max_loss, "width": width,
        "short_strike": wall, "regime": reg, "reasons": reasons,
    }
