"""ai_probability_engine.py — Feature #8's "AI Probability Engine"
stage, sitting between AI Decision Engine and AI Trade Engine in the
pipeline: ... -> AI Decision Engine -> **AI Probability Engine** ->
AI Trade Engine -> ...

Deliberately DIFFERENT in kind from ai_decision_engine.py, not a
second copy of the same idea:

  - AI Decision Engine adjusts a signal's confidence using a HEURISTIC
    (does Institutional/Technical agree with this signal's direction
    right now) — a real-time read, no memory of past outcomes.
  - AI Probability Engine estimates an actual win probability from
    this system's OWN historical trade record — "signals that looked
    like this one, in the past, won how often" — genuinely empirical,
    not another heuristic layered on top.

Requires `agents.py`'s `place()` to have captured decision-context
snapshot fields on each position at entry time (entry_confidence,
entry_institutional_agreement, entry_technical_agreement, entry_regime
— added alongside this module) — those flow through unchanged into
the closed-trade record, giving this engine something to bucket
historical outcomes by. Trades from BEFORE this was added won't have
these fields; handled as a genuine data gap (excluded from bucketed
matching, not backfilled with guesses), not hidden.
"""

# v59.66 — minimum matched trades before a bucket may emit a probability
# at all. Below this the answer is "unavailable", not a smoothed number.
MIN_SAMPLE = 5


def _bucket_confidence(confidence):
    """Buckets confidence into 10-point bands (e.g. 70 -> "70-79") —
    coarse enough that early on, with few trades, buckets actually
    have more than one or two members to average over."""
    if confidence is None:
        return None
    lo = int(confidence // 10) * 10
    return f"{lo}-{lo + 9}"


def _matches(trade, confidence_bucket, inst_agreement, tech_agreement, regime):
    """A historical trade "matches" the new signal's bucket if its own
    entry-time confidence bucket, institutional agreement, and regime
    all line up — technical agreement is treated as a SOFTER match
    (matches if equal OR if either is missing) since Feature #7 is the
    newest engine and has the least historical coverage; requiring an
    exact match on it would starve the bucket down to near-nothing for
    a long time. Regime match is exact when both are known."""
    if trade.get("entry_confidence") is None:
        return False
    if _bucket_confidence(trade["entry_confidence"]) != confidence_bucket:
        return False
    if trade.get("entry_institutional_agreement") != inst_agreement:
        return False
    t_tech = trade.get("entry_technical_agreement")
    if t_tech is not None and tech_agreement is not None and t_tech != tech_agreement:
        return False
    if regime and trade.get("entry_regime") and trade["entry_regime"] != regime:
        return False
    return True


def estimate_probability(sig, decision, regime, closed_trades):
    """The AI Probability Engine's own entry point. `decision` is the
    AI Decision Engine's own output (institutional_agreement/
    technical_agreement — reused directly, not recomputed). Returns
    {"probability_pct": 0-100, "sample_size": n, "basis": str,
    "confidence_in_estimate": "low"/"medium"/"high",
    "unavailable": bool}.

    Uses Laplace (add-one) smoothing on the win rate — with a tiny
    sample, a naive win_count/total_count estimate is dangerously
    overconfident (3 wins out of 3 trades does NOT mean 100% forever).
    Smoothing pulls small samples toward 50% until real evidence
    accumulates, and `confidence_in_estimate`/`sample_size` are always
    returned alongside the number so a caller can see WHY a 55%
    estimate from 4 trades should be trusted far less than a 55%
    estimate from 200."""
    confidence_bucket = _bucket_confidence(sig.get("confidence"))
    if confidence_bucket is None or not closed_trades:
        return {"probability_pct": None, "sample_size": 0,
               "basis": "no historical data available yet", "confidence_in_estimate": "none",
               "unavailable": True}

    inst_agreement = (decision or {}).get("institutional_agreement")
    tech_agreement = (decision or {}).get("technical_agreement")
    regime_label = (regime or {}).get("regime")

    matched = [t for t in closed_trades
              if _matches(t, confidence_bucket, inst_agreement, tech_agreement, regime_label)]
    n = len(matched)
    if n == 0:
        return {"probability_pct": None, "sample_size": 0,
               "basis": (f"no past trades match this profile yet (confidence "
                        f"{confidence_bucket}%, institutional agreement "
                        f"{inst_agreement}, regime {regime_label or 'unknown'})"),
               "confidence_in_estimate": "none", "unavailable": True}

    # v59.66 (third-eye Tier 1) — below MIN_SAMPLE the bucket is reported
    # as unavailable, not as a number. Laplace smoothing tempers a tiny
    # sample but still let n=1 emit "33%" into unified_probability; a
    # probability from that few observations is noise wearing a percent
    # sign. unified_probability already drops unavailable components.
    if n < MIN_SAMPLE:
        return {"probability_pct": None, "sample_size": n,
               "basis": (f"only {n} matching past trade(s) — below the "
                         f"{MIN_SAMPLE}-trade minimum for a probability "
                         f"estimate"),
               "confidence_in_estimate": "insufficient", "unavailable": True}
    wins = sum(1 for t in matched if t.get("pnl", 0) > 0)
    # Laplace smoothing: (wins + 1) / (n + 2) pulls toward 50% for
    # small n, converges to the true win_count/total_count as n grows.
    smoothed = (wins + 1) / (n + 2)
    probability_pct = round(smoothed * 100)

    if n < 5:
        conf_label = "low"
    elif n < 20:
        conf_label = "medium"
    else:
        conf_label = "high"

    return {"probability_pct": probability_pct, "sample_size": n,
           "basis": (f"{wins}/{n} similar past trades won (confidence "
                    f"{confidence_bucket}%, institutional agreement "
                    f"{inst_agreement}, regime {regime_label or 'any'})"),
           "confidence_in_estimate": conf_label, "unavailable": False}


def unified_probability(sig, decision, probability, institutional, technical):
    """v58 — the "Unified AI Probability" stage from the original
    pipeline framing (Option Chain -> Institutional -> Technical ->
    Decision Engine -> **AI Probability Engine** -> ...), the one piece
    of that framing genuinely not yet built: a SINGLE combined number,
    not four separate scores a human has to mentally weigh themselves.

    Deliberately distinct from every existing piece it consumes, not a
    re-derivation of any of them:
      - `estimate_probability()` above is EMPIRICAL (historical trade
        record only) — this function is a live, present-moment
        composite that also uses estimate_probability()'s own output
        as one INPUT among several, not a replacement for it.
      - `ai_decision_engine.evaluate_signal()` already checks whether
        Institutional/Technical CURRENTLY AGREE with the proposed
        direction and nudges confidence accordingly — that adjusted
        number is reused directly as one component here, not
        recomputed. What THIS function adds on top is the two
        engines' own MAGNITUDE (institutional_score / technical_score
        — how strong the reading is, not just whether it agrees),
        which the Decision Engine's agreement check never uses.

    Same weighted-composite-with-graceful-degradation pattern already
    established twice in this codebase (risk_engine.compute_ai_risk_
    score, risk_engine.trade_quality_score) — a documented first-pass
    weighting, not backtested, with any unavailable component EXCLUDED
    and the remaining weights renormalized rather than defaulting a
    missing input to a misleadingly neutral score.

    Weights (of the total, before renormalization): option-chain base
    confidence 25%, Decision-Engine-adjusted confidence 30%,
    institutional magnitude (direction-scaled) 20%, technical magnitude
    (direction-scaled) 15%, empirical historical probability 10% (itself
    scaled down further when its own `confidence_in_estimate` is low —
    a 95% estimate from 3 trades shouldn't swing this composite as hard
    as a 95% estimate from 200).

    Returns {"unified_probability_pct": 0-100 or None,
    "components": {...}, "weights": {...} (post-renormalization),
    "basis": str, "unavailable": bool}.
    """
    direction = sig.get("signal")
    if direction not in ("BUY_CE", "BUY_PE"):
        return {"unified_probability_pct": None, "components": {}, "weights": {},
               "basis": "no actionable direction to score", "unavailable": True}
    is_bullish = direction == "BUY_CE"

    components, weights = {}, {}

    base_conf = sig.get("_pre_decision_confidence")
    if base_conf is not None:
        components["option_chain"] = base_conf
        weights["option_chain"] = 0.25

    if decision and decision.get("adjusted_confidence") is not None:
        components["decision_engine"] = decision["adjusted_confidence"]
        weights["decision_engine"] = 0.30

    if institutional and not institutional.get("score_unavailable", True):
        inst_score = institutional.get("institutional_score")
        inst_bias = (institutional.get("institutional_bias") or "").lower()
        if inst_score is not None and inst_bias:
            agrees = ("bull" in inst_bias) == is_bullish
            # Disagreement inverts the magnitude: a STRONG institutional
            # reading pointing the wrong way is worse than a weak one,
            # not a neutral wash — same "conviction against you is bad"
            # framing risk_engine.py's dynamic_risk_check() already uses.
            components["institutional"] = inst_score if agrees else (100 - inst_score)
            weights["institutional"] = 0.20

    if technical and technical.get("technical_score") is not None:
        tech_score = technical["technical_score"]
        tech_bias = (technical.get("technical_bias") or "").lower()
        if tech_bias:
            agrees = ("bull" in tech_bias) == is_bullish
            components["technical"] = tech_score if agrees else (100 - tech_score)
            weights["technical"] = 0.15

    if probability and not probability.get("unavailable", True):
        prob_pct = probability.get("probability_pct")
        conf_label = probability.get("confidence_in_estimate")
        if prob_pct is not None:
            components["historical_probability"] = prob_pct
            weights["historical_probability"] = {
                "low": 0.03, "medium": 0.07, "high": 0.10}.get(conf_label, 0.05)

    if not components:
        return {"unified_probability_pct": None, "components": {}, "weights": {},
               "basis": "no inputs available yet", "unavailable": True}

    total_w = sum(weights.values())
    norm_weights = {k: round(v / total_w, 3) for k, v in weights.items()}
    score = sum(components[k] * norm_weights[k] for k in components)

    return {"unified_probability_pct": round(score),
           "components": components, "weights": norm_weights,
           "basis": f"combined from {len(components)}/5 available inputs",
           "unavailable": False}
