"""institutional_engine.py — Feature #5 (Institutional Activity Engine),
whole-market cross-domain aggregation.

Per the spec's own explicit instruction: does NOT rebuild or duplicate
the Option Chain Intelligence Engine (Feature #4). Every function here
CONSUMES existing outputs rather than recomputing anything:

  - bias:{symbol}       (market_bias.py, Feature #2) — MACD/RSI/ADX-
                        regime/Supertrend/Ichimoku/spot-trend/futures-
                        trend/OI-PCR/VIX, already weighted into one
                        "components" dict in [-1, +1] per factor.
  - analysis:{symbol}   (analyzer.analyze(), Feature #4) — per-strike
                        institutional_activity/premium_intelligence/
                        changes/iv_greeks/strike_strength, plus the
                        chain-wide pcr_engine/smart_money/ai_output/
                        narrative.
  - future_ohlc / future_oi_trend / future_oi_quadrant (agents.py's
    futures pipeline, extended across this project) — futures trend/
    OI-buildup, already computed per tick.
  - levels:{symbol}     (support_resistance.py, Feature #3) — merged
                        R1-R3/S1-S3.

Market Breadth (Advance/Decline, Sector Strength, Index Strength) is a
KNOWN, DISCLOSED gap — no NIFTY-50-constituent breadth data source
exists anywhere in this system (same honest-gap precedent already set
by Feature #2's own Market Breadth omission in market_bias.py). Its
weight is excluded from the score entirely and reported in
`unavailable`, never faked.

Design split, matching the spec's own separate output fields:
  - Institutional BIAS (direction) reuses bias:{symbol}["bias"]
    directly — Feature #2 already computed a full directional read;
    recomputing direction here would be exactly the duplication the
    spec explicitly forbids.
  - Institutional SCORE (0-100) is genuinely new: a MAGNITUDE/
    conviction read (how much institutional activity, not which way),
    built from OI migration, volume expansion, PCR trend, IV
    behaviour, strike-strength concentration, futures/spot agreement,
    and the existing technical confidence — all pulled from data
    already computed elsewhere.
"""

SCORE_WEIGHTS = {
    "futures_confirmation": 0.15,
    "oi_migration": 0.15,
    "volume_expansion": 0.10,
    "vwap_position": 0.05,
    "pcr_trend": 0.15,
    "iv_behaviour": 0.10,
    "strike_strength": 0.15,
    "technical_confirmation": 0.15,
    # market_breadth intentionally absent — no data source exists
}


def _clip01(x):
    return max(0.0, min(1.0, x))


def _futures_confirmation(bias_components):
    """Spot/futures agreement strength, reusing market_bias.py's own
    spot_trend/future_trend components (already in [-1, +1]) rather
    than re-deriving from raw ticks."""
    spot = bias_components.get("spot_trend")
    fut = bias_components.get("future_trend")
    if spot is None or fut is None:
        return None
    same_dir = (spot >= 0) == (fut >= 0)
    magnitude = min(abs(spot), abs(fut))
    return _clip01(magnitude) if same_dir else _clip01(magnitude * 0.3)


def _oi_migration_strength(smart_money):
    """Magnitude of this cycle's OI repositioning, reusing Feature #4's
    smart_money_engine output directly (oi_migration.into/out_of) —
    no new OI comparison logic."""
    migration = (smart_money or {}).get("oi_migration") or {}
    moves = (migration.get("into") or []) + (migration.get("out_of") or [])
    if not moves:
        return None
    total = sum(abs(m.get("oi_chg_pct") or 0) for m in moves)
    return _clip01(total / (len(moves) * 100))


def _volume_expansion_strength(smart_money):
    """Reuses Feature #4's volume_breakout list — presence and count
    of breakout strikes this cycle, not a new volume calculation."""
    breakouts = (smart_money or {}).get("volume_breakout") or []
    if not breakouts:
        return None
    return _clip01(len(breakouts) / 5)


def _vwap_position_strength(spot, vwap):
    """Distance of spot from VWAP as a participation signal — reuses
    whatever spot/vwap values the caller already has (e.g. from
    spot_hist-derived VWAP, Feature #1), no new tracking added here."""
    if spot is None or vwap is None or not vwap:
        return None
    return _clip01(abs(spot - vwap) / vwap * 20)


def _pcr_trend_strength(pcr_engine):
    """Reuses Feature #4's pcr_engine trend (vs_prev preferred, vs_open
    as fallback) — magnitude of the PCR move, not a new PCR calc."""
    trend = (pcr_engine or {}).get("trend") or {}
    t = trend.get("vs_prev") or trend.get("vs_open")
    if not t or t.get("change") is None:
        return None
    return _clip01(abs(t["change"]) / 0.5)


def _iv_behaviour_strength(strikes):
    """Counts how many strikes triggered an IV expansion/crush flag
    this cycle (Feature #4's iv_greeks_engine, per leg) — active IV
    repositioning across the chain reads as institutional conviction,
    regardless of direction."""
    if not strikes:
        return None
    flagged = 0
    total = 0
    for s in strikes:
        for leg in ("ce", "pe"):
            ivg = (s.get(leg) or {}).get("iv_greeks")
            if ivg is None:
                continue
            total += 1
            if ivg.get("iv_expansion") or ivg.get("iv_crush"):
                flagged += 1
    if total == 0:
        return None
    return _clip01(flagged / total * 3)


def _strike_strength_concentration(strikes):
    """Average strike_strength percentile across the window (Feature
    #4's strike_strength_engine) — high average = broad institutional
    concentration this cycle, not a single-strike anomaly."""
    if not strikes:
        return None
    vals = []
    for s in strikes:
        for leg in ("ce", "pe"):
            ss = (s.get(leg) or {}).get("strike_strength")
            if ss and ss.get("pct") is not None:
                vals.append(ss["pct"])
    if not vals:
        return None
    return _clip01((sum(vals) / len(vals)) / 100)


def institutional_participation_score(bias, analysis, spot=None, vwap=None):
    """The spec's own Institutional Participation Score (0-100),
    assembled ENTIRELY from data already computed by Feature #2
    (bias)/Feature #4 (analysis) — no new indicator or OI/volume
    calculation performed here. Market Breadth is always reported
    unavailable (no data source exists in this system).

    Returns {"score": 0-100, "label": <band>, "components": {...},
    "unavailable": [...]}."""
    bias_components = (bias or {}).get("components") or {}
    smart_money = (analysis or {}).get("smart_money") or {}
    pcr_engine = (analysis or {}).get("pcr_engine") or {}
    strikes = (analysis or {}).get("strikes") or []

    raw = {
        "futures_confirmation": _futures_confirmation(bias_components),
        "oi_migration": _oi_migration_strength(smart_money),
        "volume_expansion": _volume_expansion_strength(smart_money),
        "vwap_position": _vwap_position_strength(spot, vwap),
        "pcr_trend": _pcr_trend_strength(pcr_engine),
        "iv_behaviour": _iv_behaviour_strength(strikes),
        "strike_strength": _strike_strength_concentration(strikes),
        # Technical Confirmation reuses Feature #2's own confidence
        # directly (already a 0-100 synthesis of MACD/RSI/ADX-regime/
        # Supertrend/Ichimoku/spot+futures/OI-PCR/VIX agreement) rather
        # than re-deriving technical agreement from scratch.
        "technical_confirmation": (
            _clip01((bias or {}).get("confidence", 0) / 100)
            if bias and bias.get("confidence") is not None else None),
    }

    unavailable = ["market_breadth"]
    components = {}
    for key, val in raw.items():
        if val is None:
            unavailable.append(key)
        else:
            components[key] = round(val, 3)

    if not components:
        return {"score": 0, "label": "No Institutional Activity",
               "components": {}, "unavailable": unavailable}

    total_weight = sum(SCORE_WEIGHTS[k] for k in components)
    weighted = sum(components[k] * SCORE_WEIGHTS[k] for k in components) / total_weight
    score = round(weighted * 100)

    if score < 20:
        label = "No Institutional Activity"
    elif score < 40:
        label = "Weak Participation"
    elif score < 60:
        label = "Moderate Participation"
    elif score < 80:
        label = "Strong Participation"
    else:
        label = "Very Strong Institutional Participation"

    return {"score": score, "label": label, "components": components,
           "unavailable": unavailable}


def money_flow(bias, analysis):
    """Money Flow Direction (Bullish/Bearish/Neutral) + whether money
    is Increasing/Exiting/flat. Direction reuses bias:{symbol}'s own
    bias label directly (Feature #2) rather than recomputing a
    separate directional read — this is exactly the duplication the
    spec's own instructions forbid. The increasing/exiting read is new
    but built entirely from Feature #4's already-computed OI migration
    and volume-breakout data.

    IMPORTANT: direction and flow-state are INDEPENDENT axes, matching
    the spec's own framing (it lists them as two separate outputs, not
    one derived from the other). "Money Increasing" means net OI is
    BUILDING (fresh positioning forming) regardless of which direction
    that positioning favors — aggressive fresh call-writing into a
    bearish move is genuinely "Bearish + Money Increasing" (aggressive
    fresh bearish conviction), not "Money Exiting". "Money Exiting"
    specifically means net UNWINDING dominates (capital leaving
    positions, long or short), independent of bias direction too."""
    bias_label = (bias or {}).get("bias", "Neutral")
    if "Bullish" in bias_label:
        direction = "Bullish"
    elif "Bearish" in bias_label:
        direction = "Bearish"
    else:
        direction = "Neutral"

    smart_money = (analysis or {}).get("smart_money") or {}
    migration = smart_money.get("oi_migration") or {}
    into = migration.get("into") or []
    out_of = migration.get("out_of") or []
    net_in = sum(m.get("oi_chg_pct") or 0 for m in into)
    net_out = sum(abs(m.get("oi_chg_pct") or 0) for m in out_of)
    has_volume_expansion = bool(smart_money.get("volume_breakout"))

    if not into and not out_of:
        flow_state = "No Significant Flow"
    elif net_in > net_out and (has_volume_expansion or net_in > 30):
        flow_state = "Money Increasing"
    elif net_out > net_in and (has_volume_expansion or net_out > 30):
        flow_state = "Money Exiting"
    else:
        flow_state = "No Significant Flow"

    return {"direction": direction, "flow_state": flow_state}


def per_strike_institutional_read(analysis):
    """Feature #5's explicit "do analysis per strike" ask: rather than
    only a chain-wide summary, pulls the institutional_activity/
    premium_intelligence Feature #4 ALREADY computed per strike (no new
    per-strike logic here) specifically at the strikes that matter
    institutionally — ATM, and the #1 support/resistance walls — since
    that's where the docx's own examples anchor ("Put writers becoming
    aggressive near ATM", "Call writers shifted from 25400 to 25500").
    Returns a list of {"label": "ATM CE", "strike": ..., "activity":
    ..., "note": ...} entries, empty if analysis is incomplete."""
    if not analysis:
        return []
    atm = analysis.get("atm")
    strikes = analysis.get("strikes") or []
    signal_lines = analysis.get("signal_lines") or {}
    by_strike = {s["strike"]: s for s in strikes}

    targets = []
    if atm is not None:
        targets.append(("ATM", atm))
    r_levels = signal_lines.get("R") or []
    s_levels = signal_lines.get("S") or []
    if r_levels:
        targets.append(("R1", r_levels[0]["level"]))
    if s_levels:
        targets.append(("S1", s_levels[0]["level"]))

    out = []
    seen = set()
    for label, strike in targets:
        row = by_strike.get(strike)
        if not row or strike in seen:
            continue
        seen.add(strike)
        for leg in ("ce", "pe"):
            d = row.get(leg) or {}
            activity = d.get("institutional_activity")
            if not activity:
                continue
            out.append({
                "label": f"{label} {leg.upper()}",
                "strike": strike,
                "activity": activity,
                "note": d.get("premium_intelligence"),
            })
    return out


def breakout_validation(regime, bias, analysis, institutional_score, direction):
    """Spec's Breakout/Breakdown Validation — `direction` is "bullish"
    (validates a breakout) or "bearish" (validates a breakdown, mirror
    logic). Six confirmation conditions, EXACTLY as the spec lists
    them, each pulled from data already computed elsewhere:
      1. Spot confirms    — regime:{symbol}'s own or_position/
                            or_expansion (RegimeAgent, already
                            classifies opening-range breaks — not
                            re-detected here).
      2. Future confirms  — bias's future_trend component agreeing in
                            direction with meaningful magnitude.
      3. Volume confirms  — Feature #4's volume_breakout list.
      4. Institutional participation confirms — this module's own
                            institutional_score (increment 1) >= 40.
      5. Option Chain confirms — Feature #4's pcr_engine trend
                            direction + a resistance/support shift in
                            the breakout's favor (smart_money_engine).
      6. Technical confirmation — bias's own bias label + confidence.
    A condition is skipped (not counted for or against) when its
    underlying data isn't available, rather than defaulting to fail —
    same graceful-degradation convention as every other engine here.
    Returns {"status": "Confirmed"/"Weak"/"False" + " Breakout"/
    "Breakdown", "confirmed": n, "checked": n, "details": {...}}."""
    bullish = direction == "bullish"
    details = {}

    or_position = (regime or {}).get("or_position")
    or_expansion = (regime or {}).get("or_expansion")
    if or_position is not None:
        wanted = "above" if bullish else "below"
        details["spot_confirms"] = (or_position == wanted and
                                    (or_expansion is None or or_expansion >= 1.0))

    bias_components = (bias or {}).get("components") or {}
    future_trend = bias_components.get("future_trend")
    if future_trend is not None:
        details["future_confirms"] = ((future_trend > 0.1) if bullish
                                      else (future_trend < -0.1))

    smart_money = (analysis or {}).get("smart_money") or {}
    if "volume_breakout" in smart_money:
        details["volume_confirms"] = bool(smart_money.get("volume_breakout"))

    if institutional_score is not None:
        details["institutional_confirms"] = institutional_score >= 40

    pcr_engine = (analysis or {}).get("pcr_engine") or {}
    trend = (pcr_engine.get("trend") or {}).get("vs_prev") or \
            (pcr_engine.get("trend") or {}).get("vs_open")
    shift = smart_money.get("resistance_shift" if bullish else "support_shift")
    if trend or shift is not None:
        pcr_agrees = (trend and trend.get("direction") ==
                     ("rising" if bullish else "falling"))
        shift_agrees = shift is not None and (
            shift["to"] > shift["from"] if bullish else shift["to"] < shift["from"])
        details["option_chain_confirms"] = bool(pcr_agrees or shift_agrees)

    bias_label = (bias or {}).get("bias")
    confidence = (bias or {}).get("confidence")
    if bias_label is not None and confidence is not None:
        wanted_word = "Bullish" if bullish else "Bearish"
        details["technical_confirms"] = (wanted_word in bias_label and confidence >= 50)

    checked = len(details)
    confirmed = sum(1 for v in details.values() if v)
    word = "Breakout" if bullish else "Breakdown"
    if checked == 0:
        status = f"Unvalidated {word} (insufficient data)"
    elif confirmed >= max(5, checked - 1):
        status = f"Confirmed {word}"
    elif confirmed >= max(3, round(checked * 0.5)):
        status = f"Weak {word}"
    else:
        status = f"False {word}"

    return {"status": status, "confirmed": confirmed, "checked": checked,
           "details": details}


def detect_events(bias, analysis, future_oi_trend, flow, score,
                  breakout, breakdown):
    """The spec's Institutional Detection event list — every event
    built from data ALREADY computed above (this module's own score/
    money-flow, Feature #4's smart_money/institutional_activity,
    Feature #2's bias, and the futures OI-buildup classification
    already in future_oi_trend:{symbol}) rather than any new market
    calculation. Returns {event_name: {"active": bool, "confidence":
    0-100}}."""
    smart_money = (analysis or {}).get("smart_money") or {}
    bias_label = (bias or {}).get("bias", "Neutral")
    bullish = "Bullish" in bias_label
    bearish = "Bearish" in bias_label
    increasing = flow["flow_state"] == "Money Increasing"
    exiting = flow["flow_state"] == "Money Exiting"

    activity_counts = {}
    for s in (analysis or {}).get("strikes") or []:
        for leg in ("ce", "pe"):
            act = (s.get(leg) or {}).get("institutional_activity")
            if act:
                activity_counts[act] = activity_counts.get(act, 0) + 1
    unwinding_dominant = (activity_counts.get("Call Unwinding", 0) +
                         activity_counts.get("Put Unwinding", 0)) > sum(
        v for k, v in activity_counts.items()
        if k not in ("Call Unwinding", "Put Unwinding")) if activity_counts else False

    migration = smart_money.get("oi_migration") or {}
    has_into, has_out = bool(migration.get("into")), bool(migration.get("out_of"))
    into_total = sum(abs(m.get("oi_chg_pct") or 0) for m in migration.get("into") or [])
    out_total = sum(abs(m.get("oi_chg_pct") or 0) for m in migration.get("out_of") or [])
    balanced_rotation = (has_into and has_out and into_total and out_total and
                        0.5 <= into_total / out_total <= 2.0)

    events = {}
    events["aggressive_buyers"] = {
        "active": bullish and increasing and score >= 50,
        "confidence": score if (bullish and increasing) else max(0, score - 30)}
    events["aggressive_sellers"] = {
        "active": bearish and increasing and score >= 50,
        "confidence": score if (bearish and increasing) else max(0, score - 30)}
    events["smart_money_accumulation"] = {
        "active": not bearish and increasing and bool(smart_money.get("strong_put_writing")),
        "confidence": score if (not bearish and bool(smart_money.get("strong_put_writing"))) else 20}
    events["smart_money_distribution"] = {
        "active": not bullish and increasing and bool(smart_money.get("strong_call_writing")),
        "confidence": score if (not bullish and bool(smart_money.get("strong_call_writing"))) else 20}
    events["fresh_long_positions"] = {
        "active": future_oi_trend == "long",
        "confidence": 75 if future_oi_trend == "long" else 10}
    events["fresh_short_positions"] = {
        "active": future_oi_trend == "short",
        "confidence": 75 if future_oi_trend == "short" else 10}
    events["profit_booking"] = {
        "active": unwinding_dominant and exiting,
        "confidence": 65 if (unwinding_dominant and exiting) else 15}
    events["position_rotation"] = {
        "active": balanced_rotation,
        "confidence": 60 if balanced_rotation else 10}
    events["hedging_activity"] = {
        "active": bool(smart_money.get("strong_call_writing")) and
                 bool(smart_money.get("strong_put_writing")),
        "confidence": 70 if (smart_money.get("strong_call_writing") and
                            smart_money.get("strong_put_writing")) else 10}
    events["false_breakout"] = {
        "active": "False Breakout" in breakout["status"],
        "confidence": 80 if "False Breakout" in breakout["status"] else 10}
    events["trap_formation"] = {
        "active": (breakout["status"].startswith(("Confirmed", "Weak")) and
                  bool(smart_money.get("strong_call_writing"))) or
                 (breakdown["status"].startswith(("Confirmed", "Weak")) and
                  bool(smart_money.get("strong_put_writing"))),
        "confidence": 55}
    events["breakout_confirmation"] = {
        "active": breakout["status"] == "Confirmed Breakout",
        "confidence": round(breakout["confirmed"] / breakout["checked"] * 100)
        if breakout["checked"] else 0}
    events["breakdown_confirmation"] = {
        "active": breakdown["status"] == "Confirmed Breakdown",
        "confidence": round(breakdown["confirmed"] / breakdown["checked"] * 100)
        if breakdown["checked"] else 0}
    return events


def market_participation(bias, regime, flow, events, score, breakout, breakdown):
    """The spec's Market Participation classification — ONE label from
    Accumulation / Distribution / Trend Following / Range Trading /
    Profit Booking / Short Covering / Hedging / No Participation.

    Priority-ordered (most specific/actionable signal wins), built
    entirely from fields already computed above — this is a genuinely
    new synthesis step, not a new market calculation, and is flagged
    here (like every other judgment-call heuristic in this codebase)
    as a first-pass rule set to tune against real outcomes, not a
    validated model."""
    regime_label = (regime or {}).get("regime")

    if events["hedging_activity"]["active"]:
        return "Hedging"
    if events["profit_booking"]["active"]:
        return "Profit Booking"
    if events["aggressive_buyers"]["active"] and "Bearish" not in ((bias or {}).get("bias") or ""):
        # aggressive buying arriving specifically where recent activity
        # had been unwinding/writing-dominant reads as shorts covering
        # rather than fresh accumulation — a lighter-weight distinction
        # than fully re-deriving buildup history here.
        if events["fresh_short_positions"]["active"] is False and \
                flow["flow_state"] == "Money Increasing" and \
                regime_label in ("trending-up", "gap-and-fade", "mixed"):
            pass  # falls through to Accumulation/Trend Following below
    if score < 20:
        return "No Participation"
    if regime_label in ("rangebound", "choppy") and score < 50:
        return "Range Trading"
    if flow["flow_state"] == "Money Increasing":
        if "Bullish" in ((bias or {}).get("bias") or ""):
            if breakout["status"] == "Confirmed Breakout" and \
                    regime_label == "trending-up":
                return "Trend Following"
            return "Accumulation"
        if "Bearish" in ((bias or {}).get("bias") or ""):
            if breakdown["status"] == "Confirmed Breakdown" and \
                    regime_label == "trending-down":
                return "Trend Following"
            return "Distribution"
    if flow["flow_state"] == "Money Exiting":
        return "Profit Booking"
    return "Range Trading" if score < 50 else "No Participation"


def generate_commentary(bias, analysis, score, participation, flow,
                        breakout, breakdown, events):
    """Feature #5's AI Commentary — rule-based (not an LLM call, same
    zero-latency-always-available approach as Feature #4's narrative
    generator), matching the spec's own example phrasing exactly.
    Built entirely from fields already computed above."""
    lines = []
    bias_label = (bias or {}).get("bias", "Neutral")
    smart_money = (analysis or {}).get("smart_money") or {}

    if participation == "Accumulation":
        lines.append("Institutions continue accumulating long positions.")
    elif participation == "Distribution":
        lines.append("Institutions appear to be distributing into strength.")
    elif participation == "Hedging":
        lines.append("Both call and put writers are active — reads as "
                     "hedging rather than a directional bet.")
    elif participation == "Profit Booking":
        lines.append("Unwinding dominates this cycle — reads as profit "
                     "booking rather than fresh positioning.")
    elif participation == "Range Trading":
        lines.append("No dominant institutional lean — price is being "
                     "range-traded between existing walls.")
    elif participation == "No Participation":
        lines.append("Institutional participation is minimal this cycle.")

    if events["fresh_long_positions"]["active"]:
        lines.append("Fresh buying observed across futures and ATM Calls.")
    elif events["fresh_short_positions"]["active"]:
        lines.append("Fresh selling observed across futures and ATM Puts.")

    if smart_money.get("strong_call_writing"):
        strike = smart_money["strong_call_writing"][0]["strike"]
        lines.append(f"Aggressive Call writing suggests resistance near {strike}.")
    if smart_money.get("strong_put_writing"):
        strike = smart_money["strong_put_writing"][0]["strike"]
        lines.append(f"Aggressive Put writing suggests support near {strike}.")

    if smart_money.get("volume_breakout") and score >= 40:
        lines.append("Volume expansion confirms institutional participation.")

    # Which side to comment on: use the validation's OWN spot_confirms
    # detail (RegimeAgent's or_position reading) rather than the bias
    # label — bias can lag a fresh breakout attempt, but spot_confirms
    # is a direct "is this actually happening right now" read.
    breakout_attempted = breakout["details"].get("spot_confirms") is True
    breakdown_attempted = breakdown["details"].get("spot_confirms") is True
    if breakout_attempted and "False" in breakout["status"]:
        lines.append("Current breakout lacks institutional confirmation.")
    elif breakdown_attempted and "False" in breakdown["status"]:
        lines.append("Current breakdown lacks institutional confirmation.")
    elif breakout["status"] == "Confirmed Breakout":
        lines.append("Breakout is confirmed across spot, futures, and "
                     "option-chain positioning.")
    elif breakdown["status"] == "Confirmed Breakdown":
        lines.append("Breakdown is confirmed across spot, futures, and "
                     "option-chain positioning.")

    if events["trap_formation"]["active"]:
        lines.append("Structure resembles a trap — the move is not backed "
                     "by the writers on the opposite side.")

    return lines[:6]


def institutional_output(bias, analysis, spot=None, vwap=None,
                         regime=None, future_oi_trend=None):
    """The spec's own consolidated OUTPUT shape — Institutional Score,
    Institutional Bias, Current Activity, Money Flow, Accumulation/
    Distribution, Breakout Status, Breakdown Status, Support Shift,
    Resistance Shift, Participation Strength, Confidence%, AI
    Commentary — assembled entirely from analyze_institutional()'s own
    result (which is itself entirely reused/derived data), just
    reshaped into the spec's exact field names for callers (Market
    Bias/Risk/Trade Recommendation/AI Narrative Engines) that expect
    this specific structure."""
    full = analyze_institutional(bias, analysis, spot, vwap, regime, future_oi_trend)
    smart_money = (analysis or {}).get("smart_money") or {}
    participation = market_participation(
        bias, regime, {"direction": full["money_flow"],
                       "flow_state": full["money_flow_state"]},
        full["events"], full["institutional_score"],
        full["breakout_validation"], full["breakdown_validation"])
    commentary = generate_commentary(
        bias, analysis, full["institutional_score"], participation,
        {"direction": full["money_flow"], "flow_state": full["money_flow_state"]},
        full["breakout_validation"], full["breakdown_validation"], full["events"])

    active_events = [name for name, e in full["events"].items() if e["active"]]

    return {
        "institutional_score": full["institutional_score"],
        "institutional_bias": full["institutional_bias"],
        "current_activity": active_events,
        "money_flow": full["money_flow"],
        "money_flow_state": full["money_flow_state"],
        "market_participation": participation,
        "breakout_status": full["breakout_validation"]["status"],
        "breakdown_status": full["breakdown_validation"]["status"],
        "support_shift": smart_money.get("support_shift"),
        "resistance_shift": smart_money.get("resistance_shift"),
        "participation_strength": full["participation_label"],
        "confidence_pct": (bias or {}).get("confidence", 0),
        "ai_commentary": commentary,
        "per_strike": full["per_strike"],
        "events_detail": full["events"],
        "score_components": full["score_components"],
        "score_unavailable": full["score_unavailable"],
    }


def analyze_institutional(bias, analysis, spot=None, vwap=None,
                          regime=None, future_oi_trend=None):
    """Increment-2 entry point — extends increment 1's output
    additively with Breakout/Breakdown Validation and the full
    Institutional Detection event list. `regime` and `future_oi_trend`
    are new optional parameters (both default None, so increment-1
    callers that don't pass them still get everything they did
    before, just without the breakout/event fields)."""
    score = institutional_participation_score(bias, analysis, spot, vwap)
    flow = money_flow(bias, analysis)
    per_strike = per_strike_institutional_read(analysis)
    breakout = breakout_validation(regime, bias, analysis, score["score"], "bullish")
    breakdown = breakout_validation(regime, bias, analysis, score["score"], "bearish")
    events = detect_events(bias, analysis, future_oi_trend, flow,
                           score["score"], breakout, breakdown)
    return {
        "institutional_score": score["score"],
        "participation_label": score["label"],
        "score_components": score["components"],
        "score_unavailable": score["unavailable"],
        "institutional_bias": (bias or {}).get("bias", "Neutral"),
        "money_flow": flow["direction"],
        "money_flow_state": flow["flow_state"],
        "per_strike": per_strike,
        "breakout_validation": breakout,
        "breakdown_validation": breakdown,
        "events": events,
    }
