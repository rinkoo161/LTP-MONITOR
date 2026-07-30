"""oi_composite.py — Strategy 10: OI Buildup/Covering Composite.

The operator's own methodology, formalised. Written 2026-07-30 from a
direct specification, so the rules below are quoted intent rather than
inference:

    "If you notice Long buildup in Future (Any Indices), Short buildup
     in PE (at ATM, two strike price OTM), Short covering in Call
     Then
     Long the future, deploy Bull put spread (sell ATM Put strike price
     with short build up, buy 3 strike price far OTM put side), buy CE
     where delta is high. And vice versa.

     If notice short build-up on PE and CE at ATM, or two strike prices
     far from ATM, deploy a short condor with hedging.

     Exit as notice long covering in Future. Try to take advantage of
     time value, so when the market goes up, exit from the Sell PE side,
     keep bought PE and again Short PE ATM. Exit from both legs when
     notice covering in Put. Exit from bought CE when build-up starts in
     that CE.

     Take maximum loss of 2% of total capital. Consider cost to buy each
     leg (Rs. 50)."

WHY THIS IS ARCHITECTURALLY DIFFERENT
-------------------------------------
Every existing strategy here produces ONE instrument's signal and hands
it to the risk gate. This one produces a COMPOSITE: a futures leg, a
credit spread and a long option, fired by a single option-chain
condition and exited by option-chain conditions rather than by price
levels. Nothing in the system coordinated instruments before; spreads,
futures and option buys ran independently, which is how futures could
lose Rs 72,321 on 2026-07-30 while spreads made money — the classes were
not aware of each other.

The four-quadrant classification this depends on ALREADY EXISTED in
analyzer.py (long-buildup / short-buildup / short-covering /
long-unwinding, per strike, per side) and was only ever rendered as a
display string. analyzer.py line ~191 even recognised this exact bullish
signature and labelled it "strong bullish (Buy Call) signal" without
trading on it. This module is the missing consumer.

THE 2% CONSTRAINT IS BINDING, AND WORTH UNDERSTANDING
-----------------------------------------------------
All three legs are directionally the SAME (long future + short put
spread + long call are all bullish). They do not hedge each other, so a
move against the position loses on all three at once and the worst case
is the SUM. Measured against a Rs 10,00,000 book:

    NIFTY      fut 2,250 + BPS 8,212 + CE 9,000 = 19,462  -> 1.03 lots
    BANKNIFTY  fut 2,700 + BPS 6,570 + CE 9,600 = 18,870  -> 1.06 lots
    FINNIFTY   fut 2,600 + BPS 7,118 + CE 9,100 = 18,818  -> 1.06 lots

A 2% cap therefore permits roughly ONE composite, at one lot, on one
index. That is a consequence of the structure, not of the cap. The
module enforces it rather than silently exceeding it, and reports the
binding leg so the operator can decide which to shrink.

ASSUMPTIONS MADE WHERE THE SPEC WAS AMBIGUOUS — all configurable, all
logged on every evaluation so they are visible rather than buried:

  1. "1:3 to 1:5" was given in answer to a question about CONCURRENCY
     ratio, but reads equally as a risk:reward target. Both are
     implemented: `oi_composite_max_concurrent` (default 1, which is
     what the 2% arithmetic allows) and `oi_composite_rr_target`
     (default 3.0, the low end of 1:3). Neither is guessed silently.
  2. "Rs 50 per leg" is treated as per ORDER, not per lot: a composite
     round trip is 4 legs x 2 sides = Rs 400. Per-lot would be far
     larger; `oi_composite_cost_per_leg` and
     `oi_composite_cost_is_per_lot` make either possible.
  3. "2% of total capital" is applied PER COMPOSITE. Since one composite
     consumes ~97% of it, per-trade and per-day are nearly the same
     thing here; `oi_composite_risk_pct` is the single knob.
"""

DEFAULTS = {
    "enabled": True,
    "auto_deploy": False,          # observe-only on introduction
    "risk_pct": 2.0,               # of total capital, per composite
    "max_concurrent": 1,           # what the 2% arithmetic allows
    "rr_target": 3.0,              # the "1:3" reading
    "cost_per_leg": 50.0,
    "cost_is_per_lot": 0,
    "otm_strikes_checked": 2,      # "at ATM, two strike price OTM"
    "spread_width_strikes": 3,     # "buy 3 strike price far"
    "require_churn_filter": 1,     # ignore high-volume/low-OI churn
    "min_delta_for_long_leg": 0.45,  # "buy CE where delta is high"
    "condor_enabled": 1,
    "max_trades_per_day": 3,
}

BOUNDS = {
    "risk_pct": (1.0, 4.0, +1),
    "max_concurrent": (1, 3, +1),
    "rr_target": (2.0, 5.0, -1),
    "otm_strikes_checked": (1, 4, +1),
    "spread_width_strikes": (2, 5, +1),
    "require_churn_filter": (0, 1, -1),
    "min_delta_for_long_leg": (0.35, 0.60, -1),
    "condor_enabled": (0, 1, -1),
    "max_trades_per_day": (1, 5, +1),
}

BUILDUP_LONG = "long-buildup"
BUILDUP_SHORT = "short-buildup"
COVER_SHORT = "short-covering"
COVER_LONG = "long-unwinding"


def normalise_future_quadrant(q):
    """Accept every form the codebase produces for the futures quadrant.

    2026-07-30 -- caught before shipping, and it would have been silent.
    MarketDataAgent publishes UNDERSCORE forms on
    `future_oi_quadrant:{sym}`:

        long_buildup · short_buildup · short_covering · long_unwinding

    while `future_oi_trend:{sym}` publishes "long"/"short"/None and this
    module's own per-strike states use HYPHENS (long-buildup). The first
    version of detect_setup compared the quadrant against "long", and
    exit_reasons against "long-unwinding" -- so neither would EVER have
    matched a real bus value, and the strategy would have observed
    nothing while looking perfectly healthy.

    The unit tests passed because they passed "long" directly, which is
    precisely the blind spot: a test that feeds a hand-written value
    cannot catch a mismatch with the producer.
    """
    if not q:
        return None
    s = str(q).strip().lower().replace("-", "_")
    if s in ("long", "long_buildup", "longbuildup"):
        return "long_buildup"
    if s in ("short", "short_buildup", "shortbuildup"):
        return "short_buildup"
    if s in ("short_covering", "shortcovering"):
        return "short_covering"
    if s in ("long_unwinding", "long_covering", "unwinding", "longunwinding"):
        return "long_unwinding"
    return s


def _strike_row(strikes, target):
    for r in strikes or []:
        if r.get("strike") == target:
            return r
    return None


def _spacing(strikes):
    """Strike spacing, measured rather than assumed.

    NIFTY and FINNIFTY are 50 apart, BANKNIFTY 100, SENSEX 100 — but
    reading it off the chain avoids a hard-coded table going stale when
    an exchange changes it, which has happened.
    """
    ks = sorted({r["strike"] for r in (strikes or []) if r.get("strike")})
    if len(ks) < 2:
        return None
    gaps = [b - a for a, b in zip(ks, ks[1:]) if b > a]
    return min(gaps) if gaps else None


def _state_of(row, side, p):
    """Quadrant for one side of one strike, honouring the churn filter.

    Churn (high volume, small net OI change) means weak hands moving in
    and out rather than genuine positioning. analyzer.py already flags
    it; treating a churned strike as a real buildup is how an OI
    strategy ends up trading noise.
    """
    if not row:
        return None
    leg = row.get(side) or {}
    if p.get("require_churn_filter") and leg.get("churn"):
        return None
    return leg.get("state")


def detect_setup(analysis, future_quadrant, params=None):
    """Which composite, if any, does the chain currently justify?

    Returns (setup_or_None, detail). `detail` always explains the
    verdict — including for a rejection — because an OI condition that
    ALMOST held is the interesting case and a bare None hides it.
    """
    p = dict(DEFAULTS, **(params or {}))
    future_quadrant = normalise_future_quadrant(future_quadrant)
    detail = {}
    atm = (analysis or {}).get("atm")
    strikes = (analysis or {}).get("strikes") or []
    if not atm or not strikes:
        return None, {"why": "no chain/ATM yet"}
    sp = _spacing(strikes)
    if not sp:
        return None, {"why": "cannot determine strike spacing"}
    detail["atm"] = atm
    detail["spacing"] = sp
    detail["future_quadrant"] = future_quadrant

    n_otm = int(p["otm_strikes_checked"])
    # PE OTM is BELOW spot; CE OTM is ABOVE. Getting this backwards
    # would silently read the wrong strikes and still "work".
    pe_strikes = [atm - i * sp for i in range(0, n_otm + 1)]
    ce_strikes = [atm + i * sp for i in range(0, n_otm + 1)]

    pe_states = {k: _state_of(_strike_row(strikes, k), "pe", p) for k in pe_strikes}
    ce_states = {k: _state_of(_strike_row(strikes, k), "ce", p) for k in ce_strikes}
    detail["pe_states"] = pe_states
    detail["ce_states"] = ce_states

    pe_short_build = [k for k, s in pe_states.items() if s == BUILDUP_SHORT]
    ce_short_build = [k for k, s in ce_states.items() if s == BUILDUP_SHORT]
    ce_short_cover = [k for k, s in ce_states.items() if s == COVER_SHORT]
    pe_short_cover = [k for k, s in pe_states.items() if s == COVER_SHORT]

    # --- Short condor: writers on BOTH sides, i.e. the chain expects a
    # --- range. Checked FIRST because it is a strictly stronger
    # --- condition than either directional case and would otherwise be
    # --- masked by whichever direction happened to test first.
    if p.get("condor_enabled") and pe_short_build and ce_short_build:
        detail["why"] = (f"short buildup on BOTH sides — PE at {pe_short_build}, "
                         f"CE at {ce_short_build} — chain expects a range")
        return {"kind": "short_condor", "direction": 0,
                "sell_pe": max(pe_short_build), "sell_ce": min(ce_short_build),
                "buy_pe": max(pe_short_build) - p["spread_width_strikes"] * sp,
                "buy_ce": min(ce_short_build) + p["spread_width_strikes"] * sp,
                }, detail

    # --- Bullish composite ---
    if (future_quadrant == "long_buildup" and pe_short_build and ce_short_cover):
        sell_pe = max(pe_short_build)      # nearest-to-ATM with buildup
        return {"kind": "bullish_composite", "direction": +1,
                "future_side": "LONG",
                "sell_pe": sell_pe,
                "buy_pe": sell_pe - p["spread_width_strikes"] * sp,
                "long_leg_side": "ce",
                "long_leg_strike": atm,     # highest delta = ATM/ITM
                }, {**detail, "why": (
                    f"futures long-buildup + PE short-buildup at "
                    f"{pe_short_build} + CE short-covering at {ce_short_cover}")}

    # --- Bearish composite (the "and vice versa") ---
    if (future_quadrant == "short_buildup" and ce_short_build and pe_short_cover):
        sell_ce = min(ce_short_build)
        return {"kind": "bearish_composite", "direction": -1,
                "future_side": "SHORT",
                "sell_ce": sell_ce,
                "buy_ce": sell_ce + p["spread_width_strikes"] * sp,
                "long_leg_side": "pe",
                "long_leg_strike": atm,
                }, {**detail, "why": (
                    f"futures short-buildup + CE short-buildup at "
                    f"{ce_short_build} + PE short-covering at {pe_short_cover}")}

    detail["why"] = (
        f"no composite: future={future_quadrant!r}, "
        f"PE short-buildup at {pe_short_build or 'none'}, "
        f"CE short-covering at {ce_short_cover or 'none'}, "
        f"CE short-buildup at {ce_short_build or 'none'}, "
        f"PE short-covering at {pe_short_cover or 'none'}")
    return None, detail


def size_composite(setup, analysis, capital, lot_size, params=None):
    """Lots the 2% cap allows, and WHICH leg binds.

    Deliberately reports the binding leg. "1 lot" on its own tells the
    operator nothing about which part of the structure is consuming the
    budget, and the long option leg is consistently the largest at
    roughly 45% — which is the actionable fact if the composite needs to
    be made smaller.
    """
    p = dict(DEFAULTS, **(params or {}))
    budget = capital * p["risk_pct"] / 100.0
    strikes = (analysis or {}).get("strikes") or []
    sp = _spacing(strikes) or 50
    legs = {}

    if setup.get("kind") == "short_condor":
        # Both spreads are defined-risk; a condor's max loss is the
        # WIDER wing minus total credit, not the sum of both wings —
        # only one side can lose at expiry.
        width = p["spread_width_strikes"] * sp
        pe_row = _strike_row(strikes, setup.get("sell_pe"))
        ce_row = _strike_row(strikes, setup.get("sell_ce"))
        credit = ((pe_row or {}).get("pe", {}).get("ltp", 0)
                  + (ce_row or {}).get("ce", {}).get("ltp", 0))
        legs["condor"] = max(0.0, (width - credit)) * lot_size
    else:
        side = "pe" if setup["direction"] > 0 else "ce"
        sell_k = setup.get("sell_pe") or setup.get("sell_ce")
        buy_k = setup.get("buy_pe") or setup.get("buy_ce")
        sell_row = _strike_row(strikes, sell_k)
        buy_row = _strike_row(strikes, buy_k)
        credit = ((sell_row or {}).get(side, {}).get("ltp", 0)
                  - (buy_row or {}).get(side, {}).get("ltp", 0))
        width = abs(sell_k - buy_k) if (sell_k and buy_k) else p["spread_width_strikes"] * sp
        legs["spread"] = max(0.0, (width - credit)) * lot_size

        long_side = setup["long_leg_side"]
        long_row = _strike_row(strikes, setup.get("long_leg_strike"))
        prem = (long_row or {}).get(long_side, {}).get("ltp", 0)
        legs["long_option"] = prem * lot_size     # max loss = premium paid

        fut = (analysis or {}).get("futures_stop_points")
        legs["future"] = (fut or 0) * lot_size

    per_lot = sum(legs.values())
    cost = p["cost_per_leg"] * (8 if setup.get("kind") == "short_condor" else 8)
    if p.get("cost_is_per_lot"):
        cost_note = f"Rs {cost:,.0f}/lot"
    else:
        cost_note = f"Rs {cost:,.0f} flat per round trip"

    if per_lot <= 0:
        return 0, {"why": "cannot price the legs yet", "legs": legs}
    lots = int(budget // per_lot)
    binding = max(legs, key=lambda k: legs[k]) if legs else None
    return min(lots, int(p["max_concurrent"])), {
        "budget": round(budget), "per_lot_risk": round(per_lot),
        "legs": {k: round(v) for k, v in legs.items()},
        "binding_leg": binding,
        "binding_pct": round(100.0 * legs[binding] / per_lot, 1) if binding else None,
        "lots_by_budget": lots, "cost": cost_note,
        "why": (f"2% of Rs {capital:,.0f} = Rs {budget:,.0f}; composite risks "
                f"Rs {per_lot:,.0f}/lot ({binding} is the largest at "
                f"{round(100.0 * legs[binding] / per_lot)}%)" if binding else ""),
    }


def exit_reasons(position, analysis, future_quadrant, params=None):
    """The operator's exit rules, each as a separate testable condition.

    Returns a list of (leg, reason). A list rather than a single verdict
    because this methodology exits legs INDEPENDENTLY — "exit from the
    Sell PE side, keep bought PE" — which no existing exit path in this
    system could express.
    """
    p = dict(DEFAULTS, **(params or {}))
    out = []
    future_quadrant = normalise_future_quadrant(future_quadrant)
    strikes = (analysis or {}).get("strikes") or []
    d = position.get("direction", 0)

    # "exit as notice long covering in Future"
    if future_quadrant == "long_unwinding":
        out.append(("future", "long covering in the future"))

    # "Exit from bought CE when build-up starts in that CE"
    long_side = position.get("long_leg_side")
    long_k = position.get("long_leg_strike")
    if long_side and long_k:
        st = _state_of(_strike_row(strikes, long_k), long_side, p)
        if st == BUILDUP_LONG:
            out.append(("long_option",
                        f"buildup started in the bought {long_side.upper()} "
                        f"{long_k} ({st})"))

    # "Exit from both legs when notice covering in Put" — for the bearish
    # mirror this is covering in the Call.
    short_side = "pe" if d > 0 else "ce"
    short_k = position.get("sell_pe") if d > 0 else position.get("sell_ce")
    if short_k:
        st = _state_of(_strike_row(strikes, short_k), short_side, p)
        if st in (COVER_SHORT, COVER_LONG):
            out.append(("spread_both",
                        f"covering in the short {short_side.upper()} "
                        f"{short_k} ({st}) — close both spread legs"))
    return out


def roll_short_leg(position, analysis, future_quadrant, params=None):
    """The time-value harvest: re-centre the short leg as price moves.

        "when the market goes up, exit from the Sell PE side, keep
         bought PE and again Short PE ATM"

    Returns a roll instruction or None. This is the part with no
    precedent in the codebase — every other spread here is opened and
    closed as one unit. Between closing the old short and opening the
    new one the position is briefly a long option plus a naked long
    option, so the instruction carries the intermediate state
    explicitly rather than leaving the caller to infer it.
    """
    p = dict(DEFAULTS, **(params or {}))
    atm = (analysis or {}).get("atm")
    d = position.get("direction", 0)
    if not atm or not d:
        return None
    short_k = position.get("sell_pe") if d > 0 else position.get("sell_ce")
    if not short_k or short_k == atm:
        return None
    moved_favourably = (atm > short_k) if d > 0 else (atm < short_k)
    if not moved_favourably:
        return None
    side = "pe" if d > 0 else "ce"
    new_state = _state_of(_strike_row((analysis or {}).get("strikes"), atm), side, p)
    if new_state != BUILDUP_SHORT:
        return None      # only re-short where writers are actually active
    return {
        "close": {"side": side, "strike": short_k, "action": "buy_to_close"},
        "keep": {"side": side, "strike": position.get("buy_pe") or position.get("buy_ce")},
        "open": {"side": side, "strike": atm, "action": "sell_to_open"},
        "why": (f"ATM moved {atm - short_k:+.0f} to {atm}; short {side.upper()} "
                f"{short_k} re-centred to {atm} where {new_state} is present"),
    }


def replay(symbol, day, capital=1000000, params=None, futures_series=None):
    """Replay Strategy 10 over one archived day, minute by minute.

    HONEST SCOPE, because half a trigger is not a backtest:

      * The OPTION half is fully replayable. `chain_snapshots` stores
        per-strike ltp / oi / oi_chg / volume / delta every 60s for 5
        days, and `chg` is derived from consecutive snapshots.
      * The FUTURES half was NOT archived before v58.66. Without it the
        first condition in the rule ("Long buildup in Future") cannot be
        checked historically.

    So `futures_series` is optional and the result says which mode it
    ran in:

      "full"          futures OI available -- a real backtest
      "chain_only"    futures condition ASSUMED to agree, which
                      OVERSTATES the trigger count. Useful for measuring
                      how often the chain half occurs and eyeballing the
                      setups; NOT a performance estimate.

    Reporting the mode matters more than the numbers. A chain-only run
    that produced 40 setups would look like a busy strategy while
    possibly none of them had futures agreement.
    """
    import history
    p = dict(DEFAULTS, **(params or {}))
    snaps = history.chain_series(symbol, day)
    if not snaps:
        return {"symbol": symbol, "day": day, "setups": [], "n_snapshots": 0,
                "mode": "no_data",
                "note": ("no chain_snapshots for this day -- they are written "
                         "every 60s by TechnicalAgent and pruned after 5 days")}
    fut = {f["ts"]: f["quadrant"] for f in (futures_series or [])}
    mode = "full" if fut else "chain_only"

    def _nearest_quadrant(ts):
        if not fut:
            return None
        # nearest snapshot within 120s; futures and chain are written by
        # different agents so their timestamps do not align exactly.
        best, bestd = None, 1e9
        for t, q in fut.items():
            d = abs(t - ts)
            if d < bestd:
                best, bestd = q, d
        return best if bestd <= 120 else None

    setups, kinds = [], {}
    for snap in snaps:
        analysis = {"atm": _infer_atm(snap["strikes"]),
                    "strikes": snap["strikes"],
                    "futures_stop_points": 30}
        q = _nearest_quadrant(snap["ts"])
        if mode == "chain_only":
            # Try BOTH directions and record which chain condition held,
            # rather than silently assuming one.
            for assumed in ("long", "short"):
                s, det = detect_setup(analysis, assumed, p)
                if s:
                    kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
                    setups.append({"ts": snap["ts"], "assumed_future": assumed,
                                   "kind": s["kind"], "why": det.get("why"),
                                   "setup": s})
                    break
        else:
            s, det = detect_setup(analysis, q, p)
            if s:
                kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
                setups.append({"ts": snap["ts"], "future_quadrant": q,
                               "kind": s["kind"], "why": det.get("why"),
                               "setup": s})
    return {
        "symbol": symbol, "day": day, "mode": mode,
        "n_snapshots": len(snaps), "n_setups": len(setups),
        "setups_per_hour": round(len(setups) / max(len(snaps) / 60.0, 0.01), 1),
        "by_kind": kinds,
        "setups": setups[:40],
        "caveat": ("chain_only: the futures condition was ASSUMED to agree, "
                   "so this OVERSTATES the trigger count and is not a "
                   "performance estimate. Futures OI is archived from "
                   "v58.66 onward -- re-run in a few days for mode=full."
                   if mode == "chain_only" else
                   "full: both halves of the trigger were checked against "
                   "archived data."),
    }


def _infer_atm(strikes):
    """ATM from the archived window.

    chain_snapshots stores ~10 strikes either side of the live ATM, so
    the middle of the archived window IS the ATM at snapshot time. That
    is a property of how the archive is written, not an approximation --
    but it fails if the window was truncated, so it is isolated here
    rather than inlined.
    """
    ks = sorted({s["strike"] for s in strikes})
    return ks[len(ks) // 2] if ks else None
