"""ai_decision_engine.py — Feature #8's "AI Decision Engine" stage,
per the pipeline: Market Data -> Option Chain Intelligence ->
Institutional Activity -> Technical Confirmation -> AI Decision Engine
-> AI Probability Engine -> AI Trade Engine -> AI Execution Engine ->
Paper/Live Order -> AI Learning Engine.

AUDIT FINDING this module fixes: Institutional Activity (Feature #5,
institutional_engine.py) and Technical Confirmation (Feature #7,
technical_engine.py) are BOTH fully built, tested, and displayed on
the dashboard — but neither was ever actually wired into the trade
approval pipeline. `institutional:{symbol}` is read nowhere outside
its own module (besides one AI-commentary cross-check); `technical:
{symbol}` is read NOWHERE outside its own module at all. Every signal
RiskAgent evaluates is scored purely on StrategyAgent's own OI-bias
confidence formula — the two confirmation engines sit there computed
and displayed, contributing nothing to whether a trade actually goes
through. This is precisely the gap the pipeline diagram's own
"AI Decision Engine" stage is meant to close.

Design directly follows Feature #7's own explicit philosophy (already
written into that spec, just never connected): "Technical indicators
only increase or decrease confidence in existing market bias...
never generate Buy/Sell signals using a single indicator." This module
does exactly that — for BOTH confirmation engines, not just
technical — adjusting a signal's confidence up/down based on
agreement/disagreement, never generating or blocking on its own,
except for one explicit gate matching the same pattern RiskAgent's
regime gate already uses (an unambiguous strong-disagreement halt).
"""


def _direction_of(signal_type):
    """BUY_CE -> bullish, BUY_PE -> bearish. Returns None for anything
    else (spreads, unrecognized types) — those get no adjustment."""
    if signal_type == "BUY_CE":
        return "bullish"
    if signal_type == "BUY_PE":
        return "bearish"
    return None


def _agreement(direction, bias_label):
    """+1 if bias_label agrees with direction, -1 if it conflicts, 0
    for Neutral/unrecognized. Works for both Institutional's 3-level
    (Bullish/Bearish/Neutral) and Technical's 5-level (Strong Bullish/
    Bullish/Neutral/Bearish/Strong Bearish) bias labels — checks
    substring, not exact match, so both scales work identically here."""
    if direction is None or not bias_label:
        return 0
    bullish = "Bullish" in bias_label
    bearish = "Bearish" in bias_label
    if direction == "bullish":
        return 1 if bullish else -1 if bearish else 0
    return 1 if bearish else -1 if bullish else 0


def evaluate_signal(sig, institutional, technical):
    """The AI Decision Engine's own entry point — takes a proposed
    signal (from whichever strategy generated it) plus the two
    already-computed confirmation engines' current output for that
    symbol, and returns an adjusted confidence plus a decision note
    explaining why. Never changes sig["signal"] itself (still purely a
    confirmation layer, not a signal generator) and never silently
    blocks — the one explicit hard-block condition (both engines
    STRONGLY disagreeing) is returned as its own flag for RiskAgent's
    gate to check explicitly, matching the existing regime-gate
    pattern, not hidden inside a confidence number alone.

    Returns {"adjusted_confidence": int, "institutional_agreement":
    -1/0/1, "technical_agreement": -1/0/1, "notes": [...],
    "hard_block": bool, "hard_block_reason": str|None}."""
    base_confidence = sig.get("confidence", 0)
    direction = _direction_of(sig.get("signal"))
    notes = []

    inst_bias = (institutional or {}).get("institutional_bias")
    inst_score = (institutional or {}).get("institutional_score", 0)
    inst_agreement = _agreement(direction, inst_bias)

    tech_bias = (technical or {}).get("technical_bias")
    tech_confidence = (technical or {}).get("confidence_pct", 0)
    tech_agreement = _agreement(direction, tech_bias)

    adjustment = 0
    if inst_agreement == 1:
        bump = round(inst_score / 100 * 10)
        adjustment += bump
        notes.append(f"Institutional Activity agrees ({inst_bias}, "
                    f"score {inst_score}) — confidence +{bump}")
    elif inst_agreement == -1:
        penalty = round(inst_score / 100 * 15)
        adjustment -= penalty
        notes.append(f"Institutional Activity conflicts ({inst_bias}, "
                    f"score {inst_score}) — confidence -{penalty}")

    if tech_agreement == 1:
        bump = round(tech_confidence / 100 * 10)
        adjustment += bump
        notes.append(f"Technical Confirmation agrees ({tech_bias}, "
                    f"{tech_confidence}% confidence) — confidence +{bump}")
    elif tech_agreement == -1:
        penalty = round(tech_confidence / 100 * 15)
        adjustment -= penalty
        notes.append(f"Technical Confirmation conflicts ({tech_bias}, "
                    f"{tech_confidence}% confidence) — confidence -{penalty}")

    if not notes:
        notes.append("Institutional/Technical data unavailable or neutral — "
                     "no adjustment, base confidence used as-is")

    adjusted = max(0, min(100, base_confidence + adjustment))

    # Explicit hard-block, matching RiskAgent's own regime-gate pattern
    # (an unambiguous "don't trade this" condition surfaced as its own
    # flag, not buried inside a confidence number a caller might not
    # check closely enough) — only when BOTH engines strongly and
    # confidently disagree with the proposed direction, not a single
    # engine's mild disagreement.
    hard_block = (inst_agreement == -1 and tech_agreement == -1 and
                 inst_score >= 50 and tech_confidence >= 50)
    hard_block_reason = None
    if hard_block:
        hard_block_reason = (f"Both Institutional Activity ({inst_bias}, "
                            f"score {inst_score}) AND Technical Confirmation "
                            f"({tech_bias}, {tech_confidence}%) strongly "
                            f"conflict with {sig.get('signal')} — blocked")

    return {"adjusted_confidence": adjusted,
           "institutional_agreement": inst_agreement,
           "technical_agreement": tech_agreement,
           "notes": notes, "hard_block": hard_block,
           "hard_block_reason": hard_block_reason}
