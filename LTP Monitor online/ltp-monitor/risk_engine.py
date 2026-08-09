"""risk_engine.py — Feature #9's Institutional Portfolio Risk Engine.

Per the spec's own explicit instruction: "DO NOT create a new Risk
Engine... the following components already exist... DO NOT duplicate
them. Extend them." This module is purely additive on top of the
existing RiskAgent (agents.py) and sizing.py — it computes NEW numbers
(portfolio Greeks, the weighted AI Risk Score) that didn't exist
before, built entirely from data those existing systems already
produce. Nothing here recomputes stop-losses, position sizing, or any
of the already-built gate checks.

This increment covers two of the spec's many sections: Greeks Risk
(portfolio-level Net Delta/Gamma/Theta/Vega) and the weighted AI Risk
Score composite (Portfolio 25% / Liquidity 20% / Greeks 15% /
Volatility 15% / Institutional 10% / Technical 10% / News 5%). The
remaining sections (Trade Quality Score, Liquidity/Strike-Quality
scoring in full, Time/Theta/Correlation Risk, Dynamic real-time
monitoring, Live Portfolio Monitor, DB persistence, weekly analytics)
are deliberately NOT attempted in this pass — flagged, not silently
skipped, per the spec's own "stop after implementation for review"
instruction.
"""


def trade_quality_score(*, institutional, technical, chain_row, leg, atm,
                        strike, spot, strike_interval, probability, regime):
    """Trade Quality Score (0-100), per the spec — DISTINCT from the
    portfolio-wide AI Risk Score: this scores ONE proposed trade's own
    setup quality (Market Structure/Institutional/Technical/Option
    Chain/Strike/Liquidity/Volatility/Probability/Historical), not the
    portfolio's overall risk level. Every component reuses an
    existing engine's output directly:
      - Market Structure: regime's own trending/choppy read
        (RegimeAgent, already computed).
      - Institutional Confirmation: Feature #5's own score.
      - Technical Alignment: Feature #7's own confidence_pct.
      - Option Chain Strength + Strike Quality + Liquidity: this
        module's own strike_quality() above, which itself already
        combines OI/Volume/Greeks/bid-ask — not recomputed a second
        way here, just read directly (Option Chain Strength and
        Liquidity are its own sub-components, Strike Quality is its
        tier score).
      - Volatility: inverted (calm conditions raise trade quality,
        chaotic conditions lower it) — reuses Feature #7's own
        volatility_pct.
      - Probability: Feature #8's own AI Probability Engine estimate.
      - Historical Similar Trades: the probability estimate's own
        sample_size, expressed as a confidence-in-data multiplier
        rather than a separate score (more data behind the probability
        number IS the "historical similar trades" signal, not a
        different one).
    Missing components are excluded and weights renormalized, same
    graceful-degradation convention as compute_ai_risk_score()."""
    components = {}

    regime_label = (regime or {}).get("regime")
    if regime_label:
        components["market_structure"] = (85 if regime_label in
                                          ("trending-up", "trending-down") else
                                          40 if regime_label in ("choppy", "rangebound") else 60)

    if institutional and institutional.get("institutional_score") is not None:
        components["institutional"] = institutional.get("institutional_score")

    if technical and technical.get("confidence_pct") is not None:
        components["technical"] = technical.get("confidence_pct")

    sq = strike_quality(chain_row, leg, atm, strike, spot, strike_interval)
    if not sq["unavailable"]:
        components["option_chain_strength"] = sq["components"]["oi"] * 0.5 + \
            sq["components"]["volume"] * 0.5
        components["strike_quality"] = sq["components"]["tier"]
        components["liquidity"] = sq["components"]["liquidity"]

    if technical and technical.get("volatility_pct") is not None:
        components["volatility"] = max(0, 100 - technical["volatility_pct"])

    if probability and not probability.get("unavailable"):
        components["probability"] = probability["probability_pct"]
        # sample size acts as a confidence multiplier on how much this
        # component should count, folded into the weight below rather
        # than the score itself, so a probability of 90% from n=2
        # doesn't get treated the same as 90% from n=50.
        conf_label = probability.get("confidence_in_estimate")
        historical_weight_mult = {"low": 0.3, "medium": 0.7, "high": 1.0}.get(conf_label, 0.3)
    else:
        historical_weight_mult = 0

    weights = {"market_structure": 15, "institutional": 15, "technical": 15,
              "option_chain_strength": 15, "strike_quality": 15,
              "liquidity": 10, "volatility": 5,
              "probability": round(10 * historical_weight_mult)}

    available = {k: v for k, v in components.items() if k in weights and v is not None}
    unavailable = [k for k in weights if k not in available]
    if not available or sum(weights[k] for k in available) == 0:
        return {"score": None, "unavailable": True, "components": {}}

    total_weight = sum(weights[k] for k in available)
    score = round(sum(available[k] * weights[k] for k in available) / total_weight)
    return {"score": score, "unavailable": False,
           "components": {k: round(v, 1) for k, v in available.items()},
           "unavailable_components": unavailable}


def dynamic_risk_check(position, current_row, current_institutional,
                       current_technical, smart_money=None):
    """Dynamic Risk Monitoring, per explicit request: options markets
    move fast — a trade that looked good at entry can degrade within
    seconds, so risk needs continuous reassessment, not just a point-
    in-time check when the trade was opened. Compares CURRENT
    conditions against what was captured at ENTRY time (the position
    dict's own entry_* snapshot fields, added alongside this function)
    to detect meaningful degradation — every signal here reuses data
    an existing engine already computes every cycle; nothing new is
    calculated, this is purely a THEN-vs-NOW comparison.

    Never auto-executes anything — returns suggested actions for a
    caller (ExecutionAgent, or a human via the dashboard) to act on,
    matching this whole feature's human-in-the-loop principle. Multiple
    signals can fire in one check.

    Returns a list of {"signal": str, "severity": "low"/"medium"/
    "high", "suggested_action": "Reduce Position"/"Move Stop"/
    "Exit Partial"/"Exit Complete", "detail": str}."""
    events = []
    direction = "bullish" if (position.get("leg") or "").upper() == "CE" else "bearish"
    leg_data = (current_row or {}).get((position.get("leg") or "").lower()) or {}

    # Institutional Exit: institutional bias was AGREEING with this
    # position's direction at entry, now shows the opposite.
    if position.get("entry_institutional_agreement") == 1 and current_institutional:
        bias = current_institutional.get("institutional_bias") or ""
        opposing = ("Bearish" in bias if direction == "bullish" else "Bullish" in bias)
        if opposing:
            events.append({"signal": "Institutional Exit", "severity": "high",
                          "suggested_action": "Exit Partial",
                          "detail": f"Institutional bias flipped to {bias}, "
                                   f"against this {direction} position"})

    # Technical confirmation lost: same pattern, technical side.
    if position.get("entry_technical_agreement") == 1 and current_technical:
        tbias = current_technical.get("technical_bias") or ""
        opposing = ("Bearish" in tbias if direction == "bullish" else "Bullish" in tbias)
        if opposing:
            events.append({"signal": "Technical Confirmation Lost", "severity": "medium",
                          "suggested_action": "Move Stop",
                          "detail": f"Technical bias flipped to {tbias}"})

    # VWAP Break: price crossed VWAP against the position's direction.
    # Reuses Feature #7's own VWAP engine cross detection directly.
    vwap_data = ((current_technical or {}).get("indicator_summary") or {}).get("vwap") or {}
    cross = vwap_data.get("cross")
    if cross and ((cross == "bearish" and direction == "bullish") or
                 (cross == "bullish" and direction == "bearish")):
        events.append({"signal": "VWAP Break", "severity": "medium",
                      "suggested_action": "Move Stop",
                      "detail": f"Price crossed VWAP {cross}, against this {direction} position"})

    # Gamma Shift: current gamma vs entry gamma changed sharply — a
    # large gamma swing means the position's delta (and therefore P&L
    # sensitivity) is no longer what it was when sized at entry.
    entry_gamma = position.get("entry_gamma")
    current_gamma = leg_data.get("gamma")
    if entry_gamma and current_gamma is not None and entry_gamma != 0:
        gamma_change_pct = abs(current_gamma - entry_gamma) / abs(entry_gamma) * 100
        if gamma_change_pct > 50:
            events.append({"signal": "Gamma Shift", "severity": "low",
                          "suggested_action": "Reduce Position",
                          "detail": f"Gamma changed {gamma_change_pct:.0f}% since entry "
                                   f"({entry_gamma:.4f} -> {current_gamma:.4f})"})

    # OI Shift: this specific strike shows OI actively LEAVING right
    # now — reuses Feature #4's own smart_money oi_migration data
    # (already computed every TechnicalAgent cycle), checking whether
    # THIS position's exact strike is in the "out_of" list.
    if smart_money:
        out_of = (smart_money.get("oi_migration") or {}).get("out_of") or []
        strike = position.get("strike")
        matching = next((m for m in out_of if m.get("strike") == strike), None)
        if matching:
            events.append({"signal": "OI Shift", "severity": "medium",
                          "suggested_action": "Reduce Position",
                          "detail": f"OI leaving this exact strike "
                                   f"({matching.get('oi_chg_pct', 0):.0f}%)"})

    # Liquidity Collapse: bid-ask spread widened sharply since entry.
    entry_liquidity = position.get("entry_liquidity_score")
    bid, ask = leg_data.get("bid"), leg_data.get("ask")
    if entry_liquidity is not None and bid and ask and ask > bid:
        current_spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        current_liquidity_score = min(100, round(current_spread_pct * 50))
        if current_liquidity_score - entry_liquidity > 30:
            events.append({"signal": "Liquidity Collapse", "severity": "high",
                          "suggested_action": "Exit Complete",
                          "detail": f"Liquidity score degraded from "
                                   f"{entry_liquidity} to {current_liquidity_score}"})

    return events


def expected_loss(positions, probabilities_by_symbol):
    """Expected Loss across open positions, for the Live Portfolio
    Monitor. Reuses the AI Probability Engine's own win-probability
    estimate for each position (`probabilities_by_symbol[symbol]`,
    already computed at entry time and stored — see ai_probability_
    engine.py) rather than a new statistical model: expected loss per
    position = (1 - win probability) × potential loss if stopped out.
    Positions with no probability estimate available (a genuine gap —
    e.g. entered before this system tracked it, or too new a pattern
    to have a probability yet) are excluded from the total and counted
    separately, not assumed to be risk-free.

    Returns {"total_expected_loss": float, "positions_included": n,
    "positions_excluded": n}."""
    total = 0.0
    included = 0
    excluded = 0
    for sym, pos in (positions or {}).items():
        prob = probabilities_by_symbol.get(sym)
        if not prob or prob.get("unavailable") or prob.get("probability_pct") is None:
            excluded += 1
            continue
        loss_if_stopped = (pos.get("entry", 0) - pos.get("stoploss", 0)) * pos.get("qty", 0)
        if loss_if_stopped <= 0:
            excluded += 1
            continue
        win_pct = prob["probability_pct"] / 100
        total += (1 - win_pct) * loss_if_stopped
        included += 1
    return {"total_expected_loss": round(total, 0),
           "positions_included": included, "positions_excluded": excluded}


def backfill_iv_history(symbol, days_back=90):
    """Backfills long-window ATM IV history from ALREADY-PERSISTED
    backtest candle data — per direct user question ("why can't we
    build a true 30-90 day IV Rank, we have backtest data"). Reuses
    the EXACT SAME reconstruction pipeline `backtester.py`'s own
    replay functions already use (`history.chain_days()`/`day_chain_
    frames()`) to rebuild a full option chain for each historical day
    with coverage, then runs the SAME `analyzer.analyze()` every live
    cycle already uses (which fills IV via Black-Scholes when the
    persisted candle data doesn't carry it directly, using the exact
    same solver already relied on elsewhere) — no new IV calculation
    method, no new data source, purely wiring together three things
    that already existed independently.

    Takes the LAST reconstructed frame of each day (closest to EOD) as
    that day's representative reading — matching the standard
    convention for a daily IV series (an intraday minute-by-minute IV
    series would be far more data than a percentile calculation needs,
    and expensive to reconstruct for no real benefit). Results are
    CACHED in `daily_atm_iv` (history.py) so this expensive
    reconstruction only ever needs to run once per historical day, not
    on every percentile lookup — call this once (e.g. an overnight job
    or on demand), not from a live request path.

    Actual depth achieved depends entirely on how much historical
    option-candle data has genuinely been fetched and persisted for
    this symbol — if that's 2 years, this can build a true 2-year IV
    history; if it's 2 weeks, that's what's available. This function
    is honest about that via its own return value (`days_processed`),
    not silently assuming a fixed window.

    TODAY IS ALWAYS EXCLUDED. `chain_days()` reads distinct dates out of
    `candles`, which is written live, so on any trading day it returns
    today alongside the completed ones. Taking `frames[-1]` of a session
    still in progress would cache a mid-morning reading as that day's
    EOD IV — and because the result is upserted and every later run
    skips a day it already has, that wrong value would be PERMANENT and
    silent. The guard lives here rather than at the call site so the
    function is correct for any caller (2026-08-08).

    Returns {"days_processed": n, "days_skipped_cached": n,
    "days_skipped_no_data": n, "days_skipped_incomplete": n,
    "days_skipped_expiry_day": n}."""
    import history
    import analyzer as _an
    from datetime import datetime, timedelta
    processed = skipped_cached = skipped_no_data = skipped_incomplete = 0
    skipped_expiry_day = 0
    import store as _store
    cutoff = (_store.ist_now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    today = _store.ist_now().strftime("%Y-%m-%d")   # v59.71 — IST, not host
    for day in history.chain_days(symbol):
        if day < cutoff:
            continue
        if day >= today:
            skipped_incomplete += 1
            continue
        # Expiry day is unusable and must not be cached (2026-08-08).
        # Measured: with days_to_expiry = 1 the ATM Black-Scholes solve
        # returns 1.2 / 0.4 (NIFTY 07-28, 08-04) and 0.5 (FINNIFTY
        # 07-28) against a ~10% norm — a near-zero-time option gives a
        # degenerate root, not a low volatility. Caching those would be
        # WORSE than the empty table this wiring set out to fix: an
        # absent series degrades gracefully via the existing fallback,
        # whereas a 0.4 sitting in the series silently drags every
        # percentile that gates a trade.
        _exp = history.front_expiry_on(symbol, day)
        if _exp:
            try:
                _y, _m, _d = (int(x) for x in _exp.split("-"))
                _dy, _dm, _dd = (int(x) for x in day.split("-"))
                if (datetime(_y, _m, _d) - datetime(_dy, _dm, _dd)).days + 1 <= 1:
                    skipped_expiry_day += 1
                    continue
            except (ValueError, TypeError):
                pass
        c = history._conn()
        row = c.execute("SELECT atm_iv FROM daily_atm_iv WHERE symbol=? AND date=?",
                        (symbol, day)).fetchone()
        c.close()
        if row is not None:
            skipped_cached += 1
            continue
        # 2026-08-08 — must pass the expiry that was FRONT on `day`.
        # Without it the reconstruction blends every expiry on record
        # and stamps the frame with a long-past one, so days_to_expiry
        # is negative, the Black-Scholes fallback never solves, and IV
        # comes back 0 for every day — which is exactly why this table
        # was still empty after the function was wired to the EOD job.
        frames = history.day_chain_frames(
            symbol, day, expiry=history.front_expiry_on(symbol, day))
        if not frames:
            skipped_no_data += 1
            continue
        ts, chain = frames[-1]   # last frame of the day = EOD reading
        try:
            # as_of=day, not today — see the note in analyzer.analyze().
            analysis = _an.analyze(chain, as_of=day)
        except Exception:
            skipped_no_data += 1
            continue
        atm = analysis.get("atm")
        strikes = analysis.get("strikes") or []
        row_at_atm = next((s for s in strikes if s.get("strike") == atm), None)
        if not row_at_atm:
            skipped_no_data += 1
            continue
        ce_iv = (row_at_atm.get("ce") or {}).get("iv")
        pe_iv = (row_at_atm.get("pe") or {}).get("iv")
        ivs = [v for v in (ce_iv, pe_iv) if v]
        if not ivs:
            skipped_no_data += 1
            continue
        atm_iv = sum(ivs) / len(ivs)
        # Store the TENOR alongside the level (2026-08-09). Without it
        # the series is not self-describing and a 5-day reading is
        # indistinguishable from a 28-day one — which is term structure,
        # not volatility. _dte was already computed above for the
        # expiry-day guard; reuse it rather than recomputing.
        _tenor = None
        if _exp:
            try:
                _y2, _m2, _d2 = (int(x) for x in _exp.split("-"))
                _dy2, _dm2, _dd2 = (int(x) for x in day.split("-"))
                _tenor = (datetime(_y2, _m2, _d2)
                          - datetime(_dy2, _dm2, _dd2)).days + 1
            except (ValueError, TypeError):
                _tenor = None
        history.upsert_daily_atm_iv(symbol, day, atm_iv, _tenor)
        processed += 1
    return {"days_processed": processed, "days_skipped_cached": skipped_cached,
           "days_skipped_no_data": skipped_no_data,
           "days_skipped_incomplete": skipped_incomplete,
           "days_skipped_expiry_day": skipped_expiry_day}


def iv_percentile(current_iv, iv_history, source_label=None):
    """IV Percentile/Rank, per the spec. This function doesn't know
    WHICH data source `iv_history` came from — the short-window
    `history.get_iv_history()` (chain_snapshots, pruned after 5 days)
    or the long-window `history.get_daily_atm_iv_history()` (backfilled
    from persisted backtest candle data via `backfill_iv_history()`,
    potentially spanning months or years) — so the caller supplies
    `source_label` describing what was actually used. Bug found and
    fixed 2026-07-26: an earlier version hardcoded a "5-day" caveat
    directly in this function regardless of the real data behind it,
    which became actively MISLEADING once the long-window backfill
    existed (a genuine 90-day percentile would have been mislabeled as
    only a 5-day one). Callers using the short-window source should
    pass an honest label noting that limitation; callers using the
    backfilled long-window source can honestly describe its real span.

    Percentile = % of historical readings AT OR BELOW today's IV
    (standard percentile-rank definition). Returns {"percentile": 0-100
    or None, "sample_size": n, "window_label": str, "unavailable":
    bool}."""
    if current_iv is None or not iv_history:
        return {"percentile": None, "sample_size": 0,
               "window_label": "no IV history available yet", "unavailable": True}
    n = len(iv_history)
    below_or_equal = sum(1 for v in iv_history if v <= current_iv)
    percentile = round(below_or_equal / n * 100)
    return {"percentile": percentile, "sample_size": n,
           "window_label": source_label or f"{n}-reading window (source unspecified)",
           "unavailable": False}


def dynamic_spread_profit_target_pct(cfg, avg_iv, iv_pctl_result=None,
                                     regime_label=None, adx=None):
    """v58.9 — per explicit request: the fixed ~10-15% profit capture
    (retuned down from an unreachable 60% target on 2026-07-22, see
    that fix's own comment in agents.py) is conservative and keeps win
    rate high, but leaves real upside on the table on days IV genuinely
    supports capturing more. Reuses `iv_percentile()` above, which
    existed fully built and tested but had ZERO callers anywhere in
    the codebase before this — this is that function's first actual
    use.

    Three-tier fallback, always returns something sensible rather than
    silently falling back to the flat default without ever trying to
    use real IV data:
      1. Percentile-based (best) — if `iv_pctl_result` has enough
         sample size to be meaningful, band by percentile: <30th ->
         low, 30-70th -> normal, >70th -> elevated.
      2. Absolute IV level (fallback if no percentile history yet) —
         reuses the SAME thresholds analyzer.py's `iv_status` already
         uses elsewhere (>25 elevated, <12 subdued, else normal) so
         this doesn't invent a second, different definition of what
         counts as "elevated."
      3. Flat configured default (last resort, no IV reading at all).

    Elevated-IV band additionally checks whether the trend looks
    STABLE (a real regime — trending-up/trending-down, not rangebound/
    choppy/mixed — combined with a reasonably strong ADX) before
    reaching for the top of the requested 40-50% range; an elevated-IV
    but non-trending session gets the more conservative 40%, per the
    explicit request's own wording ("elevated AND trend is stable").

    Returns (target_pct, basis_string) — the basis is always attached
    to the spread record so it's auditable later (why THIS trade got
    THIS target), not a silent number.
    """
    low_pct = cfg.get("spread_target_low_iv_pct", 20.0)
    normal_pct = cfg.get("spread_target_normal_iv_pct", 30.0)
    elevated_stable_pct = cfg.get("spread_target_elevated_iv_stable_pct", 50.0)
    elevated_unstable_pct = cfg.get("spread_target_elevated_iv_pct", 40.0)

    def _elevated_target():
        stable = (regime_label in ("trending-up", "trending-down") and
                 (adx or 0) >= 25)
        return (elevated_stable_pct if stable else elevated_unstable_pct,
               "stable trend" if stable else "no confirmed stable trend")

    if iv_pctl_result and not iv_pctl_result.get("unavailable") and \
            iv_pctl_result.get("sample_size", 0) >= 20:
        p = iv_pctl_result["percentile"]
        if p < 30:
            return low_pct, f"IV percentile {p} (low, {iv_pctl_result['window_label']})"
        if p > 70:
            pct, note = _elevated_target()
            return pct, f"IV percentile {p} (elevated, {note}, {iv_pctl_result['window_label']})"
        return normal_pct, f"IV percentile {p} (normal, {iv_pctl_result['window_label']})"

    if avg_iv:
        if avg_iv > 25:
            pct, note = _elevated_target()
            return pct, f"IV {avg_iv}% (elevated by absolute level, {note}, no percentile history yet)"
        if avg_iv < 12:
            return low_pct, f"IV {avg_iv}% (subdued by absolute level, no percentile history yet)"
        return normal_pct, f"IV {avg_iv}% (normal by absolute level, no percentile history yet)"

    return cfg.get("spread_profit_target_pct", 10.0), "fixed (no IV reading available at all)"


def strike_quality(chain_row, leg, atm, strike, spot, strike_interval=50):
    """Strike Quality score (0-100), per the spec: ATM highest, 1
    strike ITM high, 1 strike OTM medium, deep ITM lower, far OTM
    reject — combined with OI/Volume/Greeks/Liquidity. Reuses the SAME
    per-leg data (oi/volume/delta/bid/ask) analyzer.py already
    computes for every strike in the chain — no new fetch. `chain_row`
    is one row from the live chain's "rows" list (the strike being
    evaluated); `leg` is "ce"/"pe". `strike_interval` is the gap
    between consecutive listed strikes for this symbol (50 for NIFTY,
    100 for BANKNIFTY/SENSEX, etc. — default 50, callers should pass
    the real value when known).

    Bug found in testing: moneyness was originally computed from
    spot's distance to the candidate strike, divided by the (candidate
    strike - ATM) gap — conflating two different reference points. The
    ATM strike itself would incorrectly classify as "Deep ITM" any
    time spot wasn't sitting EXACTLY on a strike (the normal case,
    e.g. spot=23705 with strikes at 23700/23750/...). Fixed: tier is
    now based purely on how many STRIKE-STEPS this candidate is from
    the ATM strike (`(strike - atm) / strike_interval`), independent
    of spot's exact position within that interval."""
    if not chain_row or strike is None or atm is None:
        return {"score": None, "tier": None, "unavailable": True}

    steps = (strike - atm) / strike_interval if strike_interval else 0
    # CE: strikes BELOW ATM are ITM (positive moneyness), above are
    # OTM. PE is the mirror.
    moneyness = -steps if leg == "ce" else steps
    if -0.5 <= moneyness <= 0.5:
        tier, tier_score = "ATM", 100
    elif 0.5 < moneyness <= 1.5:
        tier, tier_score = "1 ITM", 80
    elif -1.5 <= moneyness < -0.5:
        tier, tier_score = "1 OTM", 60
    elif moneyness > 1.5:
        tier, tier_score = "Deep ITM", 40
    else:
        tier, tier_score = "Far OTM", 10

    leg_data = chain_row.get(leg) or {}
    oi, volume = leg_data.get("oi") or 0, leg_data.get("volume") or 0
    bid, ask = leg_data.get("bid") or 0, leg_data.get("ask") or 0
    # OI/Volume contribution: presence of real open interest and
    # volume at this strike (not just theoretical moneyness) — scaled
    # against a reasonable "actively traded" reference rather than an
    # absolute count, since liquid strike OI varies hugely by symbol.
    oi_component = min(100, round((oi / 50000) * 100)) if oi else 0
    volume_component = min(100, round((volume / 5000) * 100)) if volume else 0
    liquidity_component = 0
    if bid and ask and ask > bid:
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        liquidity_component = max(0, 100 - round(spread_pct * 30))

    score = round(tier_score * 0.4 + oi_component * 0.2 +
                 volume_component * 0.2 + liquidity_component * 0.2)
    return {"score": score, "tier": tier, "unavailable": False,
           "components": {"tier": tier_score, "oi": oi_component,
                          "volume": volume_component, "liquidity": liquidity_component}}


def time_risk(now_ist, expiry_date=None):
    """Time Risk multiplier (1.0 = normal, higher = riskier), per the
    spec: near market close, lunch session, and expiry day should all
    increase risk. Reuses only the clock — no new data source. Returns
    {"multiplier": float, "reasons": [...]}."""
    minute_of_day = now_ist.hour * 60 + now_ist.minute
    reasons = []
    multiplier = 1.0
    if minute_of_day >= 15 * 60 + 10:   # final stretch before the 15:22 square-off
        multiplier = max(multiplier, 1.5)
        reasons.append("near market close (last 15 minutes)")
    if 12 * 60 <= minute_of_day < 13 * 60 + 15:   # 12:00-13:15 lunch lull
        multiplier = max(multiplier, 1.15)
        reasons.append("lunch session (typically thin volume)")
    if expiry_date and now_ist.strftime("%Y-%m-%d") == expiry_date:
        multiplier = max(multiplier, 1.4)
        reasons.append("expiry day — elevated gamma/pinning risk")
    return {"multiplier": round(multiplier, 2), "reasons": reasons}


def theta_risk(theta, premium, minutes_to_close):
    """Theta Risk — expected premium loss from time decay alone before
    the session ends, as a % of current premium. Reuses the SAME
    per-leg theta already computed (broker-supplied or Black-Scholes
    fallback) — no new greek calculation. `theta` is expected in
    PER-DAY terms (standard convention); scaled down to the remaining
    session minutes. Risk increases specifically late in the session,
    matching the spec's own framing, since the SAME theta eats a
    bigger fraction of a shrinking remaining-time budget."""
    if theta is None or not premium:
        return {"expected_decay_pct": None, "unavailable": True}
    daily_decay = abs(theta)
    # 2026-08-03: F&O close moved 15:30 -> 15:40, so the session is 385
    # minutes, not 375. Anything scaling risk by elapsed session fraction
    # was reading ~3% fast at the close.
    session_minutes = 385   # 9:15-15:40 IST (F&O)
    fraction_of_day = min(1.0, minutes_to_close / session_minutes) if session_minutes else 0
    expected_loss = daily_decay * fraction_of_day
    expected_decay_pct = round(min(100, expected_loss / premium * 100), 1)
    return {"expected_decay_pct": expected_decay_pct, "unavailable": False}


def correlation_risk(positions):
    """Correlation Risk — detects over-concentration in the SAME
    directional bet spread across "different" symbols that actually
    move together (NIFTY/BANKNIFTY/FINNIFTY/SENSEX are all index
    options on the same underlying market direction). Multiple CE (or
    multiple PE) positions open simultaneously across these symbols is
    effectively one large leveraged directional bet, not genuine
    diversification — flagged here, not blocked (correlation risk is
    contextual; a human or the caller decides whether to actually
    reject). Reuses only the already-open `positions` dict, no new
    data."""
    if not positions or len(positions) < 2:
        return {"concentrated": False, "same_direction_count": 0, "legs": []}
    legs = [p.get("leg", "").upper() for p in positions.values()]
    ce_count = legs.count("CE")
    pe_count = legs.count("PE")
    concentrated = ce_count >= 2 or pe_count >= 2
    return {"concentrated": concentrated,
           "same_direction_count": max(ce_count, pe_count),
           "legs": legs}


def aggregate_portfolio_greeks(positions, get_chain_fn, spreads=None):
    """Net Delta/Gamma/Theta/Vega across all open single-leg positions
    AND credit spreads, scaled by quantity. Reuses per-leg greeks
    analyzer.py ALREADY computes for every strike (broker-supplied
    where available, Black-Scholes fallback otherwise, per its own
    existing "greeks_source" field) — no new greek calculation
    anywhere in this function, purely a lookup-and-sum over data that
    already exists.

    `get_chain_fn(symbol)` should return the same live chain dict shape
    `bus.get(f"chain:{symbol}")` already provides (a dict with a
    "rows" list, each row having "strike"/"ce"/"pe" keys).

    Spreads are a genuine extension over single-leg positions (added
    after the initial increment, flagged then as a deferred gap): each
    spread has a SHORT leg (sold — negative greek sign, since being
    short a call/put has opposite Greeks exposure to being long it)
    and a LONG hedge leg (bought — positive sign, same convention as
    a regular position). Both legs' strikes/types come from the
    spread's own already-stored fields.

    Returns {"delta": float, "gamma": float, "theta": float,
    "vega": float, "positions_included": n, "positions_missing_data":
    n} — the two count fields make data completeness visible rather
    than silently averaging over however many positions happened to
    have usable data."""
    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    included = 0
    missing = 0
    for sym, pos in (positions or {}).items():
        chain = get_chain_fn(sym)
        if not chain:
            missing += 1
            continue
        row = next((r for r in chain.get("rows", [])
                   if r.get("strike") == pos.get("strike")), None)
        if not row:
            missing += 1
            continue
        leg_data = row.get((pos.get("leg") or "").lower()) or {}
        greeks = {k: leg_data.get(k) for k in ("delta", "gamma", "theta", "vega")}
        if any(v is None for v in greeks.values()):
            missing += 1
            continue
        qty = pos.get("qty", 0)
        for k in net:
            net[k] += greeks[k] * qty
        included += 1

    for sp in (spreads or {}).values():
        sym = sp.get("symbol")
        chain = get_chain_fn(sym) if sym else None
        if not chain:
            missing += 1
            continue
        qty = sp.get("qty", 0)
        legs = sp.get("legs") or []
        if not legs:
            missing += 1
            continue
        ok = True
        spread_net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        for leg_info in legs:
            # action="SELL" -> short (negative greek sign, opposite of
            # being long that option); action="BUY" -> the hedge leg,
            # same positive sign as a regular long position.
            sign = -1 if leg_info.get("action") == "SELL" else 1
            row = next((r for r in chain.get("rows", [])
                       if r.get("strike") == leg_info.get("strike")), None)
            if not row:
                ok = False
                break
            leg_data = row.get((leg_info.get("leg") or "").lower()) or {}
            greeks = {k: leg_data.get(k) for k in ("delta", "gamma", "theta", "vega")}
            if any(v is None for v in greeks.values()):
                ok = False
                break
            for k in spread_net:
                spread_net[k] += greeks[k] * qty * sign
        if not ok:
            missing += 1
            continue
        for k in net:
            net[k] += spread_net[k]
        included += 1

    return {**{k: round(v, 4) for k, v in net.items()},
           "positions_included": included, "positions_missing_data": missing}


def _portfolio_component(daily_pnl, daily_loss_limit, position_count, max_positions):
    """Portfolio sub-score (0-100, higher = riskier): the worse of how
    much of today's loss limit is already used, or how full the
    concurrent-position book is. Reuses the SAME two numbers RiskAgent.
    evaluate() already checks as pass/fail gates (daily_loss_limit,
    max_concurrent_positions) — this just expresses them as a graduated
    0-100 contribution instead of a binary pass/fail, for the composite
    score."""
    loss_used_pct = (max(0, -daily_pnl) / daily_loss_limit * 100
                     if daily_loss_limit else 0)
    position_used_pct = (position_count / max_positions * 100
                         if max_positions else 0)
    return min(100, max(loss_used_pct, position_used_pct))


def _liquidity_component(positions, get_chain_fn):
    """Liquidity sub-score (0-100, higher = riskier): average bid-ask
    spread as a % of mid-price across open positions' current quotes —
    reuses the `bid`/`ask` fields analyzer.py already carries on every
    leg (broker-supplied, defaulting to 0 when unavailable), no new
    quote data. A wide spread relative to price is expensive to exit
    and a genuine liquidity risk; a missing quote can't be scored and
    is excluded rather than guessed at."""
    if not positions:
        return 0, 0   # no open positions -> no liquidity risk, explicit 0 not "unavailable"
    spreads = []
    for sym, pos in positions.items():
        chain = get_chain_fn(sym)
        if not chain:
            continue
        row = next((r for r in chain.get("rows", [])
                   if r.get("strike") == pos.get("strike")), None)
        if not row:
            continue
        leg_data = row.get((pos.get("leg") or "").lower()) or {}
        bid, ask = leg_data.get("bid"), leg_data.get("ask")
        if not bid or not ask or ask <= bid:
            continue
        mid = (bid + ask) / 2
        spreads.append((ask - bid) / mid * 100 if mid else 0)
    if not spreads:
        return None, 0   # genuinely no usable bid/ask data -- unavailable, not zero
    avg_spread_pct = sum(spreads) / len(spreads)
    # A 2% spread is already wide for a liquid index option; scaling
    # so ~2% spread lands near 100 (documented calibration, same
    # honesty standard as every other threshold in this codebase).
    return min(100, round(avg_spread_pct * 50)), len(spreads)


def _greeks_component(portfolio_greeks, deployed_capital):
    """Greeks sub-score (0-100, higher = riskier): net delta exposure
    normalized against deployed capital — a large directional delta
    relative to capital at risk means the portfolio is effectively a
    single big directional bet dressed up as several separate option
    trades. Uses delta specifically (not all four greeks combined)
    since delta concentration is the dominant practical risk for a
    book of long option positions; gamma/theta/vega are still returned
    in the aggregation above for display, just not folded into this
    particular sub-score to avoid an arbitrary combination formula
    across four different units."""
    if portfolio_greeks.get("positions_included", 0) == 0:
        return 0
    if not deployed_capital:
        return 0
    delta_exposure_ratio = abs(portfolio_greeks["delta"]) / deployed_capital * 100
    return min(100, round(delta_exposure_ratio * 20))


def compute_ai_risk_score(*, daily_pnl, daily_loss_limit, position_count,
                          max_positions, positions, get_chain_fn,
                          deployed_capital, market_risk_meter,
                          institutional_score, technical_volatility_pct,
                          news_score, spreads=None):
    """The spec's own weighted AI Risk Score (0-100): Portfolio 25% /
    Liquidity 20% / Greeks 15% / Volatility 15% / Institutional 10% /
    Technical 10% / News 5%. Every sub-component reuses an existing
    calculation — `market_risk_meter` is analyzer.py's own existing
    risk_meter (distance-from-max-pain + PCR skew + IV, already
    computed every chain analysis cycle — not recomputed here, and
    per the earlier audit this narrower existing score is exactly
    what feeds the Volatility slice of the new composite rather than
    being duplicated). `institutional_score`/`technical_volatility_pct`
    reuse Feature #5/#7's own outputs directly. `news_score` reuses the
    existing `news_risk_opportunity()` value (its absolute magnitude —
    any significant news event raises risk regardless of which
    direction it points).

    Sub-components unavailable (no open positions for liquidity/
    greeks, no chain data yet, etc.) are excluded from the weighted
    average and the weights renormalized across whatever IS available
    — same graceful-degradation convention used throughout this
    project — rather than treating missing data as zero risk (which
    would be misleadingly reassuring) or blocking the whole score.

    Returns {"score": 0-100, "risk_level": "Very Low".."Extreme",
    "components": {...}, "unavailable": [...]}."""
    portfolio = _portfolio_component(daily_pnl, daily_loss_limit,
                                     position_count, max_positions)
    liquidity, _n = _liquidity_component(positions, get_chain_fn)
    greeks = _greeks_component(
        aggregate_portfolio_greeks(positions, get_chain_fn, spreads), deployed_capital)

    weights = {"portfolio": 25, "liquidity": 20, "greeks": 15, "volatility": 15,
              "institutional": 10, "technical": 10, "news": 5}
    # Bug found in testing: `abs(news_score or 0)` silently collapsed
    # a genuinely MISSING news_score (None — no news data available)
    # into 0 (explicitly "no risk"), the same as a real, computed 0.0
    # score would mean. Those are different things — one is a data
    # gap, the other is a confirmed calm reading — and conflating them
    # would let the score quietly treat "no data" as "definitely safe"
    # rather than excluding it. Fixed with an explicit None check.
    news_component = min(100, abs(news_score) * 100) if news_score is not None else None
    raw = {"portfolio": portfolio, "liquidity": liquidity, "greeks": greeks,
          "volatility": market_risk_meter,
          "institutional": institutional_score,
          "technical": technical_volatility_pct,
          "news": news_component}

    available = {k: v for k, v in raw.items() if v is not None}
    unavailable = [k for k in raw if k not in available]
    if not available:
        return {"score": 0, "risk_level": "Very Low", "components": {},
               "unavailable": unavailable}

    total_weight = sum(weights[k] for k in available)
    score = round(sum(available[k] * weights[k] for k in available) / total_weight)

    if score < 20:
        risk_level = "Very Low"
    elif score < 40:
        risk_level = "Low"
    elif score < 60:
        risk_level = "Medium"
    elif score < 80:
        risk_level = "High"
    else:
        risk_level = "Extreme"

    return {"score": score, "risk_level": risk_level,
           "components": {k: round(v, 1) for k, v in available.items()},
           "unavailable": unavailable}
