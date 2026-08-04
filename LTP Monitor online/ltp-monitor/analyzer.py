"""
LTP-Calculator style option-chain analytics.

Implements the concepts popularized by volume/OI based option-chain reading:
  * Buyer vs Seller dominance per strike (volume vs OI-change behaviour)
  * Support / resistance from PE / CE open-interest concentration
  * Max Pain, PCR (OI & volume), IV skew
  * Per-strike "risk level" for option sellers (safe -> danger zones)
  * A market-state machine (trending / rangebound / reversal-watch)
  * Rule-based trade suggestions with entry / SL / target zones

NOTHING here is financial advice -- it is a decision-support dashboard.
"""

import json
import os
import urllib.request
import urllib.error


import time as _time
import threading as _threading

# ---------------------------------------------------------------- AI budget
# One process-wide gate that every Claude call passes through. It caches
# results per (kind, symbol), rate-limits per symbol, skips calls when the
# market picture hasn't materially changed, and enforces a hard daily cap.
# This is what keeps token spend low.
_ai_lock = _threading.Lock()
_ai_cache = {}          # key -> (ts, result, fingerprint)
_ai_calls_today = 0
_ai_day = None
_ai_last_error = None


def ai_budget_status():
    with _ai_lock:
        return {"calls_today": _ai_calls_today, "day": _ai_day,
                "last_error": _ai_last_error}


def _fingerprint(analysis: dict) -> str:
    """A cheap signature of the market picture. If this hasn't changed, an
    expensive AI re-analysis would just return the same thing — so we skip
    it and serve cache. Rounded so tiny wiggles don't force new calls."""
    spot = analysis.get("spot") or 0
    return "|".join(str(x) for x in [
        analysis.get("symbol"), analysis.get("bias"),
        round(spot / 10) * 10,                       # spot bucket
        analysis.get("atm"), analysis.get("max_pain"),
        analysis.get("pcr_oi"),
        round((analysis.get("avg_iv") or 0)),
        (analysis.get("momentum") or {}).get("trend"),
        round(analysis.get("risk_meter", 0) / 10) * 10,
    ])


def _ai_gate(kind: str, symbol: str, fingerprint: str):
    """Return (cached_result, reason_to_skip) or (None, None) to proceed.
    Caller must hold nothing; this manages its own lock."""
    import config as _cfg
    cfg = _cfg.load()
    global _ai_calls_today, _ai_day
    with _ai_lock:
        today = _time.strftime("%Y-%m-%d")
        if today != _ai_day:
            _ai_day, _ai_calls_today = today, 0
        if not cfg.get("ai_enabled", True):
            return None, "ai_disabled"
        key = f"{kind}:{symbol}"
        hit = _ai_cache.get(key)
        ttl = cfg.get("ai_min_interval", 180)
        if hit:
            ts, result, fp = hit
            fresh = _time.time() - ts < ttl
            unchanged = cfg.get("ai_signal_on_change_only", True) and fp == fingerprint
            if fresh or unchanged:
                return result, None            # serve cache
        if _ai_calls_today >= cfg.get("ai_daily_call_cap", 400):
            return None, "daily_cap"
        return None, "proceed"


def _ai_store(kind: str, symbol: str, fingerprint: str, result):
    global _ai_calls_today
    with _ai_lock:
        _ai_cache[f"{kind}:{symbol}"] = (_time.time(), result, fingerprint)
        _ai_calls_today += 1


def _claude_json(prompt: str, api_key: str, max_tokens: int = 700):
    """Call the configured LLM (local Ollama by default). Returns
    (text, error_message). api_key kept for signature compat; the engine
    choice now lives in config and is handled by llm.generate_json."""
    global _ai_last_error
    import llm
    text, engine, err = llm.generate_json(prompt, max_tokens)
    if err:
        _ai_last_error = ("Anthropic key invalid/expired — update it in "
                          "Settings" if "invalid" in err or "401" in err
                          else err)
        return None, _ai_last_error
    _ai_last_error = None
    return text, None


# ---------------------------------------------------------------- core maths



def ranked_levels(win, spot):
    """R1-R3 / S1-S3 from combined OI + OI-change + volume concentration.
    Per the OI support/resistance rule: CE resistance is built from the
    call side at/above spot (OTM+ATM calls), PE support from the put side
    at/below spot (OTM+ATM puts) — an ITM strike's OI doesn't act as a
    forward wall. Strength is relative to the strongest wall in that set;
    colors follow blue (max) / yellow (>=75%) / pink (55-75%)."""
    def score(leg):
        return leg["oi"] * 0.5 + max(leg["oi_chg"], 0) * 0.3 + leg["volume"] * 0.2

    def rank(side, cands):
        rows = sorted(cands, key=lambda r: score(r[side]), reverse=True)[:3]
        mx = score(rows[0][side]) if rows else 1
        out = []
        for i, r in enumerate(rows):
            pct = round(score(r[side]) / mx * 100) if mx else 0
            color = "blue" if i == 0 else "yellow" if pct >= 75 else \
                    "pink" if pct >= 55 else "grey"
            out.append({"level": r["strike"], "strength": pct,
                        "color": color,
                        "oi": r[side]["oi"], "oi_chg": r[side]["oi_chg"]})
        return out

    ce_cands = [r for r in win if r["strike"] >= spot] or win
    pe_cands = [r for r in win if r["strike"] <= spot] or win
    return {"R": rank("ce", ce_cands), "S": rank("pe", pe_cands)}


def chain_colors(win):
    """Whole-chain (not just top-3) OI/volume color coding for the
    Color-Coded Dashboard panel: blue = strongest wall in the visible
    chain, yellow >=75% of it, pink 55-75%, else uncolored."""
    def score(leg):
        return leg["oi"] * 0.5 + max(leg["oi_chg"], 0) * 0.3 + leg["volume"] * 0.2

    def colorize(side):
        scored = [(r["strike"], score(r[side])) for r in win]
        mx = max((v for _, v in scored), default=1) or 1
        out = {}
        for k, v in scored:
            pct = round(v / mx * 100)
            color = ("blue" if pct >= 99 else "yellow" if pct >= 75
                     else "pink" if pct >= 55 else "none")
            out[k] = {"pct": pct, "color": color}
        return out

    return {"ce": colorize("ce"), "pe": colorize("pe")}


def premium_color(chg, ltp):
    """Directional colour + intensity for the LTP-change column."""
    if not ltp:
        return {"dir": "flat", "intensity": "light"}
    pct = abs(chg) / ltp * 100
    intensity = "strong" if pct >= 8 else "medium" if pct >= 3 else "light"
    direction = "up" if chg > 0 else "down" if chg < 0 else "flat"
    return {"dir": direction, "intensity": intensity, "pct": round(pct, 1)}


def sentiment_summary(win):
    """Aggregate long/short buildup & unwinding across the visible chain,
    per the Market Sentiment Interpretation rules (price vs OI direction)."""
    from collections import Counter
    ce_counts, pe_counts = Counter(), Counter()
    for r in win:
        ce_counts[classify_leg(r["ce"])[0]] += 1
        pe_counts[classify_leg(r["pe"])[0]] += 1
    dom_ce = ce_counts.most_common(1)[0][0] if ce_counts else "long-unwinding"
    dom_pe = pe_counts.most_common(1)[0][0] if pe_counts else "long-unwinding"
    phrase = {
        "long-buildup": "fresh buying (long build-up)",
        "short-buildup": "aggressive writing / selling (short build-up)",
        "short-covering": "writers exiting — buyers regaining control (short covering)",
        "long-unwinding": "long holders booking out (long unwinding)",
    }
    hint = "Neutral — no dominant one-sided writing."
    if dom_pe == "short-buildup" and dom_ce != "short-buildup":
        hint = "PE writers defending support aggressively — bullish tilt."
    elif dom_ce == "short-buildup" and dom_pe != "short-buildup":
        hint = "CE writers capping upside aggressively — bearish tilt."
    elif dom_ce == "short-covering" and dom_pe == "short-buildup":
        hint = "Call short-covering + Put short-buildup together — strong bullish (Buy Call) signal."
    elif dom_pe == "short-covering" and dom_ce == "short-buildup":
        hint = "Put short-covering + Call short-buildup together — strong bearish (Buy Put) signal."
    return {
        "ce": {"counts": dict(ce_counts), "dominant": dom_ce},
        "pe": {"counts": dict(pe_counts), "dominant": dom_pe},
        "summary": f"CE side: {phrase[dom_ce]}. PE side: {phrase[dom_pe]}.",
        "hint": hint,
    }


def max_pain(rows):
    """Strike where total option writers' loss is minimum."""
    strikes = [r["strike"] for r in rows]
    best, best_val = None, float("inf")
    for k in strikes:
        pain = 0.0
        for r in rows:
            s = r["strike"]
            pain += (r["ce"].get("oi") or 0) * max(0.0, k - s)   # CE writers lose above strike
            pain += (r["pe"].get("oi") or 0) * max(0.0, s - k)   # PE writers lose below strike
        if pain < best_val:
            best, best_val = k, pain
    return best


def classify_leg(leg):
    """
    Volume vs OI interpretation (LTP-calculator style):
      price up  + OI up   -> long build-up      (buyers in control)
      price down+ OI up   -> short build-up     (sellers/writers in control)
      price up  + OI down -> short covering
      price down+ OI down -> long unwinding
    High volume with small net OI change = intraday churn (weak hands).
    """
    price_up = leg["chg"] > 0
    oi_up = leg["oi_chg"] > 0
    if price_up and oi_up:
        state = "long-buildup"
    elif (not price_up) and oi_up:
        state = "short-buildup"
    elif price_up and not oi_up:
        state = "short-covering"
    else:
        state = "long-unwinding"
    churn = leg["volume"] > 0 and abs(leg["oi_chg"]) < 0.15 * leg["volume"]
    return state, churn


def institutional_activity(state, side):
    """Feature #4 (Option Chain Intelligence Engine) — Institutional
    Activity Engine. Maps classify_leg()'s existing 4-quadrant price/OI
    classification onto the spec's 8 named categories, reusing the
    exact same logic rather than recomputing it:
      Long Build-up / Long Unwinding — generic, same name either side
        (directional buying/selling isn't writing-specific).
      Short Build-up / Short Covering — these ARE inherently leg-
        specific (a call writer and a put writer are different market
        participants even though the price/OI math is identical), so
        reported under the spec's per-leg names: Fresh Call Writing /
        Fresh Put Writing (short build-up) and Call Unwinding / Put
        Unwinding (short covering).
    `side` is "ce" or "pe" (case-insensitive)."""
    side = side.lower()
    if state == "long-buildup":
        return "Long Build-up"
    if state == "long-unwinding":
        return "Long Unwinding"
    if state == "short-buildup":
        return "Fresh Call Writing" if side == "ce" else "Fresh Put Writing"
    if state == "short-covering":
        return "Call Unwinding" if side == "ce" else "Put Unwinding"
    return "Neutral"


def _pct_change(cur, prev):
    """None (not 0 or a fake number) whenever either side is missing —
    same honesty standard as Market Breadth/Volume Profile elsewhere:
    an unavailable comparison should read as unavailable, not silently
    default to 0% (which would look like 'no change' rather than 'no
    data')."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def _abs_change(cur, prev, ndigits):
    if cur is None or prev is None:
        return None
    return round(cur - prev, ndigits)


def leg_changes(leg_data, prev_snapshot, session_open_snapshot):
    """Feature #4's Real-Time Calculations section: premium/OI/volume/
    IV % change and delta/gamma/theta/vega absolute change, against
    BOTH the previous persisted snapshot and today's session-open
    snapshot (either or both may not exist yet — no snapshot history
    on a fresh install, or before today's first snapshot has landed —
    in which case that half is None, not faked)."""
    out = {"vs_prev": None, "vs_open": None}
    for key, snap in (("vs_prev", prev_snapshot), ("vs_open", session_open_snapshot)):
        if not snap:
            continue
        out[key] = {
            "premium_chg_pct": _pct_change(leg_data.get("ltp"), snap.get("ltp")),
            "oi_chg_pct": _pct_change(leg_data.get("oi"), snap.get("oi")),
            "volume_chg_pct": _pct_change(leg_data.get("volume"), snap.get("volume")),
            "iv_chg_pct": _pct_change(leg_data.get("iv"), snap.get("iv")),
            "delta_chg": _abs_change(leg_data.get("delta"), snap.get("delta"), 4),
            "gamma_chg": _abs_change(leg_data.get("gamma"), snap.get("gamma"), 5),
            "theta_chg": _abs_change(leg_data.get("theta"), snap.get("theta"), 4),
            "vega_chg": _abs_change(leg_data.get("vega"), snap.get("vega"), 4),
        }
    return out


def premium_intelligence(state, side):
    """Feature #4 Premium Intelligence — plain-English explanation of a
    leg's premium+OI combination. Reuses classify_leg()'s existing
    quadrant (no new logic), just narrates it, per the spec's own
    mapping:
      Premium up + OI up   -> Fresh Buying
      Premium down + OI up -> Writing
      Premium up + OI down -> Short Covering
      Premium down + OI down -> Long Unwinding
    """
    side_word = "Call" if side.lower() == "ce" else "Put"
    templates = {
        "long-buildup": f"Fresh buying in {side_word}s — premium and OI both "
                        f"rising, buyers in control.",
        "short-buildup": f"{side_word} writing — premium falling while OI "
                         f"rises, writers in control.",
        "short-covering": f"Short covering in {side_word}s — premium rising "
                          f"while OI falls, writers exiting.",
        "long-unwinding": f"Long unwinding in {side_word}s — premium and OI "
                          f"both falling, buyers booking out.",
    }
    return templates.get(state, f"No dominant {side_word.lower()} activity detected.")


def pcr_engine(rows, win, atm, snapshot_ctx=None):
    """Feature #4 PCR Engine — Overall, ATM, strike-wise, and intraday-
    trend PCR. Overall PCR here matches analyze()'s existing pcr_oi
    exactly (same full-chain `rows`, same formula) — this function
    doesn't replace that field, it's the richer structured version for
    the Option Chain Engine's own consolidated output.

    Honest scope note: the trend comparison is computed over the
    ANALYZED FOCUS WINDOW (~10 strikes either side of ATM — what
    chain_snapshots actually persists), not the full chain, since that
    window is what's available to compare against historically. Labeled
    "window_pcr" explicitly rather than silently conflated with
    "overall" — same disclosed-approximation discipline already used
    elsewhere in this codebase (e.g. VWAP-as-TWAP-proxy)."""
    def pcr_of(strikes):
        ce_oi = sum((s["ce"].get("oi") or 0) for s in strikes)
        pe_oi = sum((s["pe"].get("oi") or 0) for s in strikes)
        return round(pe_oi / ce_oi, 2) if ce_oi else None

    def bias_label(pcr):
        if pcr is None:
            return "unavailable"
        if pcr > 1.3:
            return "bullish (heavy put OI relative to calls)"
        if pcr < 0.7:
            return "bearish (heavy call OI relative to puts)"
        return "neutral"

    def window_pcr_from_snapshot(snap_map):
        if not snap_map:
            return None
        ce_oi = sum(v.get("oi") or 0 for (strike, leg), v in snap_map.items() if leg == "ce")
        pe_oi = sum(v.get("oi") or 0 for (strike, leg), v in snap_map.items() if leg == "pe")
        return round(pe_oi / ce_oi, 2) if ce_oi else None

    overall = pcr_of(rows)
    atm_row = next((r for r in win if r["strike"] == atm), None)
    atm_pcr = pcr_of([atm_row]) if atm_row else None
    strike_wise = [{"strike": r["strike"], "pcr": pcr_of([r])} for r in win]
    window_pcr_now = pcr_of(win)

    def trend_vs(label, snap_map):
        past_pcr = window_pcr_from_snapshot(snap_map)
        if past_pcr is None or window_pcr_now is None:
            return None
        direction = ("rising" if window_pcr_now > past_pcr else
                    "falling" if window_pcr_now < past_pcr else "flat")
        return {f"{label}_pcr": past_pcr, "current_pcr": window_pcr_now,
               "change": round(window_pcr_now - past_pcr, 2),
               "direction": direction}

    trend = {"vs_prev": None, "vs_open": None}
    if snapshot_ctx:
        trend["vs_prev"] = trend_vs("prev", snapshot_ctx.get("prev"))
        trend["vs_open"] = trend_vs("open", snapshot_ctx.get("session_open"))

    return {
        "overall": overall, "overall_bias": bias_label(overall),
        "atm": atm_pcr, "atm_bias": bias_label(atm_pcr),
        "strike_wise": strike_wise,
        "window_pcr": window_pcr_now,
        "trend": trend,
    }


def iv_greeks_engine(leg_data, changes):
    """Feature #4 IV & Greeks Engine — detects IV Expansion, IV Crush,
    Gamma Build-up, Theta Decay (acceleration), and Vega Risk (high
    vega exposure relative to premium). Reuses the SAME `changes` dict
    leg_changes() already computed for this leg (no new snapshot
    fetch) — prefers the vs_prev comparison (most recent snapshot),
    falls back to vs_open if vs_prev isn't available yet, and every
    flag stays False/no-explanation when there's no history to compare
    against at all (same graceful-degradation pattern as increments
    1-2). Vega Risk is the one exception that needs no history — it's
    a snapshot-in-time read of current vega relative to current
    premium, not a change-over-time detection.

    Thresholds (8% IV move, 10% gamma move, 15% vega/premium ratio) are
    a documented first-pass heuristic to tune against real outcomes —
    same honesty standard already applied to this codebase's other
    heuristics (e.g. the news impact-window classifier), not a
    validated/backtested model."""
    cmp = (changes or {}).get("vs_prev") or (changes or {}).get("vs_open")
    flags = {"iv_expansion": False, "iv_crush": False,
            "gamma_buildup": False, "theta_decay_accelerating": False,
            "vega_risk": False, "explanations": []}

    if cmp:
        iv_chg = cmp.get("iv_chg_pct")
        if iv_chg is not None and iv_chg >= 8:
            flags["iv_expansion"] = True
            flags["explanations"].append(
                f"IV expanding (+{iv_chg}%) — premiums inflating faster "
                f"than delta alone would predict; benefits option "
                f"buyers, raises risk for writers.")
        elif iv_chg is not None and iv_chg <= -8:
            flags["iv_crush"] = True
            flags["explanations"].append(
                f"IV crush ({iv_chg}%) — premiums deflating even without "
                f"an adverse spot move; a real risk for option buyers "
                f"holding through this.")

        gamma_chg, gamma_now = cmp.get("gamma_chg"), leg_data.get("gamma")
        if gamma_chg is not None and gamma_now and gamma_now > 0 and \
                gamma_chg / gamma_now > 0.10:
            flags["gamma_buildup"] = True
            flags["explanations"].append(
                "Gamma building up — delta is becoming more sensitive "
                "to further spot moves (typical as a strike moves "
                "toward ATM or expiry approaches).")

        theta_chg = cmp.get("theta_chg")
        if theta_chg is not None and theta_chg < 0:
            flags["theta_decay_accelerating"] = True
            flags["explanations"].append(
                "Theta decay accelerating — time value eroding faster "
                "than before; works against option buyers, for writers.")

    vega_now, ltp_now = leg_data.get("vega"), leg_data.get("ltp")
    if vega_now and ltp_now:
        if abs(vega_now) / ltp_now > 0.15:
            flags["vega_risk"] = True
            flags["explanations"].append(
                "High vega exposure relative to premium — unusually "
                "sensitive to IV swings; a volatility crush or spike "
                "can move this premium sharply even with spot unchanged.")

    return flags


def _top_wall_from_snapshot(snap_map, spot, side):
    """Recomputes ranked_levels()'s exact top-wall SCORE formula
    (oi*0.5 + max(oi_chg,0)*0.3 + volume*0.2) against a persisted
    snapshot map, restricted the same way ranked_levels restricts its
    candidates (CE >= spot, PE <= spot) — used only for Support/
    Resistance Shift detection below, so "did the #1 wall move" is
    measured with the SAME definition of "wall" the live wall-detection
    code already uses, not a different simplified proxy."""
    def score(v):
        return ((v.get("oi") or 0) * 0.5 + max(v.get("oi_chg") or 0, 0) * 0.3
                + (v.get("volume") or 0) * 0.2)
    cands = [(strike, score(v)) for (strike, leg), v in snap_map.items()
             if leg == side and ((side == "ce" and strike >= spot) or
                                 (side == "pe" and strike <= spot))]
    return max(cands, key=lambda x: x[1])[0] if cands else None


def smart_money_engine(strikes_out, signal_lines, spot, snapshot_ctx=None):
    """Feature #4 Smart Money Engine — chain-wide detection across the
    per-strike institutional_activity/changes ALREADY computed by
    increments 1-3 (nothing recalculated). Detects:
      Strong Call/Put Writing — Fresh Call/Put Writing (increment 1)
        paired with a large OI increase (>15% vs previous snapshot).
      Support/Resistance Shift — the #1 wall (using ranked_levels'
        OWN scoring formula, recomputed against the previous snapshot)
        has moved to a different strike than last cycle.
      OI Migration — top-3 strikes gaining OI vs top-3 losing it this
        cycle (net repositioning across the chain).
      Volume Breakout — any leg with a sharp volume jump (>50% vs
        previous snapshot).
      Aggressive Buyers/Writers — Long Build-up / fresh-writing
        activity paired with a sharp volume jump (conviction, not
        drift) — same >30% volume-jump bar for both.
    Every list/field stays empty/None (not faked) with no snapshot
    history to compare against — same graceful-degradation pattern as
    increments 1-3. Thresholds (15%/30%/50%) are a documented first-
    pass heuristic, same honesty standard as iv_greeks_engine's."""
    events = {"strong_call_writing": [], "strong_put_writing": [],
             "support_shift": None, "resistance_shift": None,
             "oi_migration": {"into": [], "out_of": []},
             "volume_breakout": [], "aggressive_buyers": [],
             "aggressive_writers": []}

    oi_deltas = []
    for s in strikes_out:
        for leg in ("ce", "pe"):
            d = s[leg]
            cmp = (d.get("changes") or {}).get("vs_prev")
            if not cmp:
                continue
            oi_chg_pct = cmp.get("oi_chg_pct")
            vol_chg_pct = cmp.get("volume_chg_pct")
            activity = d.get("institutional_activity")
            if oi_chg_pct is not None:
                oi_deltas.append((s["strike"], leg.upper(), oi_chg_pct))
            if activity == "Fresh Call Writing" and oi_chg_pct and oi_chg_pct > 15:
                events["strong_call_writing"].append(
                    {"strike": s["strike"], "oi_chg_pct": oi_chg_pct})
            if activity == "Fresh Put Writing" and oi_chg_pct and oi_chg_pct > 15:
                events["strong_put_writing"].append(
                    {"strike": s["strike"], "oi_chg_pct": oi_chg_pct})
            if vol_chg_pct and vol_chg_pct > 50:
                events["volume_breakout"].append(
                    {"strike": s["strike"], "leg": leg.upper(),
                    "volume_chg_pct": vol_chg_pct})
            if activity == "Long Build-up" and vol_chg_pct and vol_chg_pct > 30:
                events["aggressive_buyers"].append(
                    {"strike": s["strike"], "leg": leg.upper()})
            if activity in ("Fresh Call Writing", "Fresh Put Writing") and \
                    vol_chg_pct and vol_chg_pct > 30:
                events["aggressive_writers"].append(
                    {"strike": s["strike"], "leg": leg.upper()})

    oi_deltas.sort(key=lambda x: x[2], reverse=True)
    events["oi_migration"]["into"] = [
        {"strike": s, "leg": l, "oi_chg_pct": c}
        for s, l, c in oi_deltas[:3] if c > 0]
    events["oi_migration"]["out_of"] = [
        {"strike": s, "leg": l, "oi_chg_pct": c}
        for s, l, c in oi_deltas[-3:] if c < 0]

    if snapshot_ctx and snapshot_ctx.get("prev"):
        prev_map = snapshot_ctx["prev"]
        prev_r1 = _top_wall_from_snapshot(prev_map, spot, "ce")
        prev_s1 = _top_wall_from_snapshot(prev_map, spot, "pe")
        cur_r1 = signal_lines["R"][0]["level"] if signal_lines.get("R") else None
        cur_s1 = signal_lines["S"][0]["level"] if signal_lines.get("S") else None
        if prev_r1 is not None and cur_r1 is not None and prev_r1 != cur_r1:
            events["resistance_shift"] = {"from": prev_r1, "to": cur_r1}
        if prev_s1 is not None and cur_s1 is not None and prev_s1 != cur_s1:
            events["support_shift"] = {"from": prev_s1, "to": cur_s1}

    return events


def strike_strength_engine(strikes_out, atm, spot):
    """Feature #4 Strike Strength Engine — the spec's FULLER multi-
    factor score (Total OI, OI% change, Volume, Volume% change,
    Premium% change, IV, Distance from ATM), ADDITIVE alongside (not
    replacing) `chain_colors()`'s existing wall-detection score that
    spread strategies already depend on live in production — that
    formula is completely untouched. This is a separate, richer
    institutional-activity ranking, per the spec's own "use ranking in
    AI decision making" ask, exposed as a new per-leg field.

    Normalizes each factor 0-1 against the analyzed window's own max
    (adapts to whatever OI/volume scale a given symbol/day actually
    has, rather than a hardcoded absolute threshold), then weights:
    Total OI 25%, OI%-change 15%, Volume 20%, Volume%-change 15%,
    Premium%-change 10%, IV 10%, Distance-from-ATM (closer=stronger)
    5%. Documented first-pass weighting, same honesty standard as the
    IV/Greeks and Smart Money engines' thresholds — not backtested.
    Colors follow the same blue(highest)/yellow(>=75%)/pink(55-75%)
    convention chain_colors() already established."""
    rows = []
    for s in strikes_out:
        for leg in ("ce", "pe"):
            d = s[leg]
            cmp = (d.get("changes") or {}).get("vs_prev") or {}
            dist_pct = abs(s["strike"] - atm) / spot * 100 if spot else 0
            rows.append((s["strike"], leg, d.get("oi") or 0,
                        abs(cmp.get("oi_chg_pct") or 0), d.get("volume") or 0,
                        abs(cmp.get("volume_chg_pct") or 0),
                        abs(cmp.get("premium_chg_pct") or 0),
                        d.get("iv") or 0, dist_pct))
    if not rows:
        return {}

    def mx(idx):
        v = max(r[idx] for r in rows)
        return v if v else 1

    mx_oi, mx_oichg, mx_vol = mx(2), mx(3), mx(4)
    mx_volchg, mx_prem, mx_iv, mx_dist = mx(5), mx(6), mx(7), mx(8)

    scored = {}
    for strike, leg, oi, oi_chg_pct, vol, vol_chg_pct, prem_chg_pct, iv, dist_pct in rows:
        norm_dist = 1 - (dist_pct / mx_dist) if mx_dist else 1
        scored[(strike, leg)] = (
            (oi / mx_oi) * 0.25 + (oi_chg_pct / mx_oichg) * 0.15 +
            (vol / mx_vol) * 0.20 + (vol_chg_pct / mx_volchg) * 0.15 +
            (prem_chg_pct / mx_prem) * 0.10 + (iv / mx_iv) * 0.10 +
            norm_dist * 0.05)

    mx_score = max(scored.values()) or 1
    out = {}
    for (strike, leg), sc in scored.items():
        pct = round(sc / mx_score * 100)
        color = ("blue" if pct >= 99 else "yellow" if pct >= 75
                else "pink" if pct >= 55 else "none")
        out.setdefault(strike, {})[leg] = {"score": round(sc, 4),
                                           "pct": pct, "color": color}
    return out


def generate_narrative(strikes_out, smart_money, atm):
    """Feature #4 AI Narrative — plain-English bullet sentences built
    from data ALREADY computed (institutional_activity, smart_money
    events), rule-based rather than an LLM call so it's always
    available with zero added latency/cost — matches the spec's own
    "every AI recommendation must include an explanation" requirement
    at the sentence level, in the exact style of the spec's own
    examples ("Fresh Put Writing observed at 25250 indicating strong
    institutional support."). Capped to the most notable lines rather
    than one per strike, so it stays readable."""
    lines = []
    for s in strikes_out:
        for leg, side_role in (("ce", "resistance"), ("pe", "support")):
            act = s[leg].get("institutional_activity")
            if act in ("Fresh Call Writing", "Fresh Put Writing"):
                lines.append(f"{act} observed at {s['strike']} indicating "
                            f"strong institutional {side_role}.")
    for e in smart_money.get("strong_call_writing", [])[:1]:
        lines.append(f"Call writers active at {e['strike']} creating "
                     f"major resistance.")
    for e in smart_money.get("strong_put_writing", [])[:1]:
        lines.append(f"Put writers active at {e['strike']} creating "
                     f"major support.")
    atm_row = next((s for s in strikes_out if s["strike"] == atm), None)
    if atm_row:
        for leg, label in (("ce", "CE"), ("pe", "PE")):
            act = atm_row[leg].get("institutional_activity")
            if act == "Long Build-up":
                lines.append(f"ATM {label} showing Long Build-up with "
                             f"rising premium and volume.")
            elif act:
                lines.append(f"ATM {label} showing {act}.")
    if smart_money.get("resistance_shift"):
        rs = smart_money["resistance_shift"]
        lines.append(f"Resistance has shifted from {rs['from']} to "
                     f"{rs['to']} — OI migrating higher.")
    if smart_money.get("support_shift"):
        ss = smart_money["support_shift"]
        lines.append(f"Support has shifted from {ss['from']} to "
                     f"{ss['to']} — OI migrating lower.")
    return lines[:8]


def option_chain_ai_output(bias, support, resistance, atm, confidence,
                           avg_iv, pcr_full, suggestions, strikes_out):
    """Feature #4's own consolidated AI Output structure, assembled
    entirely from data ALREADY computed elsewhere in analyze() (no new
    calculation): Current Bias, Strongest Support/Resistance, Highest
    OI/Volume/Premium-Change strikes, an Institutional Activity
    summary (counts per category chain-wide), PCR, IV Status, Best ATM
    Strike, Suggested CE/PE, Confidence%."""
    def top_by(value_fn):
        best = None
        for s in strikes_out:
            for leg in ("ce", "pe"):
                v = value_fn(s[leg])
                if v is None:
                    continue
                if best is None or v > best[0]:
                    best = (v, s["strike"], leg.upper())
        return ({"strike": best[1], "leg": best[2], "value": best[0]}
               if best else None)

    def premium_chg_abs(d):
        cmp = (d.get("changes") or {}).get("vs_prev")
        return abs(cmp["premium_chg_pct"]) if cmp and cmp.get("premium_chg_pct") is not None else None

    from collections import Counter
    activity_counts = Counter()
    for s in strikes_out:
        for leg in ("ce", "pe"):
            act = s[leg].get("institutional_activity")
            if act:
                activity_counts[act] += 1

    iv_status = ("elevated" if avg_iv > 25 else
                "subdued" if avg_iv < 12 else "normal")
    # `suggestions` is _suggest()'s own list-of-action-dicts shape
    # (each with "action"/"instrument" like "23700 CE", not the
    # separate ai_signal()/_rule_signal() "signal": "BUY_CE" shape) —
    # picking the first BUY entry per leg from that list, matching
    # what's actually returned rather than a different function's
    # shape.
    suggested_ce = next((x for x in suggestions
                        if x.get("action") == "BUY" and "CE" in x.get("instrument", "")), None)
    suggested_pe = next((x for x in suggestions
                        if x.get("action") == "BUY" and "PE" in x.get("instrument", "")), None)

    return {
        "current_bias": bias,
        "strongest_support": support[:3],
        "strongest_resistance": resistance[:3],
        "highest_oi_strike": top_by(lambda d: d.get("oi")),
        "highest_volume_strike": top_by(lambda d: d.get("volume")),
        "highest_premium_change_strike": top_by(premium_chg_abs),
        "institutional_activity_summary": dict(activity_counts),
        "pcr": {"overall": pcr_full.get("overall"), "atm": pcr_full.get("atm"),
               "bias": pcr_full.get("overall_bias")},
        "iv_status": iv_status,
        "best_atm_strike": atm,
        "suggested_ce": suggested_ce,
        "suggested_pe": suggested_pe,
        "confidence_pct": confidence,
    }



# --------------------------------------------------------------- stops
# ONE definition of option stop geometry (2026-08-02).
#
# There were FOUR, and they disagreed about everything that matters:
#
#   analyzer  (x3 sites)  entry * 0.70                 flat 30%, no vol input
#   agents ~6285 / ~6990  entry * (1 - risk_pct)       clamp [5%, 30%]
#   agents ~6575          atr_pct * atr_stop_multiplier, fallback 0.15
#   agents ~6796 / ~7211  1.5*ATR*0.5                  clamp [10%, 60%]
#
# Two different clamps, two different fallbacks, one path ignoring
# volatility entirely. The result is visible in the journal as stop
# widths clustered on discrete values — 43%, 30%, 14%, 5% — which no
# volatility-derived rule produces. A trade's risk therefore depended on
# which code path happened to generate it, not on the market, and that
# made per-symbol risk incomparable: NIFTY signals came mostly from the
# widest rule, SENSEX from the flat 30% one.
#
# This is the same failure the OI quadrant classifier, the market-session
# check and the news regexes each had: two near-identical implementations
# that silently drift. Collapsed to one, with the others as callers.
STOP_BOUNDS = (0.05, 0.60)      # fraction of premium; the UNION of the old
                                # clamps, so no existing path is truncated
                                # by the consolidation itself
# Used ONLY when no volatility reading exists. 0.30, not the 0.15 the
# agents paths used: the ATR branch produces ~29% for a typical NIFTY
# case, and 30% is what dominated the journal, so a fallback of 0.15
# would be systematically TIGHTER than the rule it stands in for — a
# silent risk change smuggled in under a consolidation.
STOP_FALLBACK_PCT = 0.30


def option_stop_geometry(entry, cfg=None, atr_pct=None, spot=None,
                         spot_risk_pts=None, delta=0.5, rr=2.0, rr2=2.67):
    """(stoploss, target1, target2, meta) for a long option.

    Preference order, most informative first:

      1. `spot_risk_pts` — a STRUCTURAL stop already expressed in index
         points (e.g. the entry candle's low). Translated to premium via
         `delta`. This is the best input because it comes from price
         structure rather than an average.
      2. `atr_pct` — index ATR as a percentage of spot, scaled by
         `atr_stop_multiplier`. Volatility-proportional, so it is
         comparable across symbols.
      3. `STOP_FALLBACK_PCT` — a flat fraction, used ONLY when neither
         exists, and reported as such in `meta["basis"]` so a fallback is
         never mistaken for a measurement.

    Returns the fraction actually used in `meta["risk_pct"]` and which
    branch produced it in `meta["basis"]`, because "why is this stop
    43%?" was previously unanswerable from the record.
    """
    import config as _cfg
    cfg = cfg if cfg is not None else _cfg.load()
    entry = float(entry or 0)
    if entry <= 0:
        return None, None, None, {"basis": "no entry price"}
    lo, hi = STOP_BOUNDS
    if spot_risk_pts:
        risk_pct = abs(float(spot_risk_pts)) * float(delta) / entry
        basis = "structural"
    elif atr_pct and spot:
        # DIMENSIONS. `atr_pct` is index ATR as a percentage of SPOT. The
        # old rule did `atr_pct * multiplier / 100` and used the result
        # directly as a fraction of PREMIUM — comparing a spot quantity
        # to a premium one. For NIFTY that yields 1.75%, below the 5%
        # floor, so the ATR branch ALWAYS produced the lower clamp: every
        # "volatility-based" stop was really the clamp, which is why the
        # journal shows a 5% cluster in every symbol.
        #
        # Correct translation: an index move of `atr_pct%` of spot moves
        # the premium by that many points times delta.
        idx_pts = float(atr_pct) / 100.0 * float(spot) * \
            cfg.get("option_stop_atr_mult", 0.5)
        risk_pct = (idx_pts * float(delta)) / entry
        basis = "atr"
    elif atr_pct:
        # ATR known but no spot to translate it through — refuse to guess
        # rather than reuse the dimensionally wrong shortcut.
        risk_pct = STOP_FALLBACK_PCT
        basis = "fallback (ATR present but no spot to translate it)"
    else:
        risk_pct = STOP_FALLBACK_PCT
        basis = "fallback (no ATR reading)"
    clamped = max(lo, min(hi, risk_pct))
    meta = {"basis": basis, "risk_pct": round(clamped, 4),
            "raw_risk_pct": round(risk_pct, 4),
            "clamped": abs(clamped - risk_pct) > 1e-9,
            "bounds": STOP_BOUNDS}
    return (round(entry * (1 - clamped), 2),
            round(entry * (1 + clamped * rr), 2),
            round(entry * (1 + clamped * rr * (rr2 / rr if rr else 1)), 2),
            meta)

def analyze(chain: dict, momentum: dict | None = None,
            indicators: dict | None = None, snapshot_ctx: dict | None = None) -> dict:
    rows = chain["rows"]
    spot = chain["spot"] or 0
    if not rows or not spot:
        return {"error": "empty chain"}

    # Normalize once: some brokers (observed with Kotak) omit OI/volume
    # for illiquid strikes or return them as None/missing rather than 0.
    # Every downstream calculation in this module assumes numeric
    # oi/oi_chg/volume — sanitize here so a gap in broker data can never
    # crash analysis, regardless of which function touches it later.
    import bs_greeks as _bs
    from datetime import date as _date
    days_to_expiry = None
    exp_str = chain.get("expiry")
    if exp_str:
        try:
            y, m, d_ = (int(x) for x in exp_str.split("-"))
            days_to_expiry = (_date(y, m, d_) - _date.today()).days + 1
        except (ValueError, TypeError):
            pass
    for r in rows:
        for leg in ("ce", "pe"):
            d = r.get(leg) or {}
            for field, default in (("oi", 0), ("oi_chg", 0), ("volume", 0),
                                   ("ltp", 0), ("chg", 0), ("iv", 0),
                                   ("bid", 0), ("ask", 0)):
                d[field] = d.get(field) or default
            # Fallback greeks: only when the broker didn't provide real
            # ones (delta still None means no greeks came through at
            # all — Kotak/Zerodha always leave it None; Dhan sets a
            # real value whenever it has data, so this never overrides
            # a genuine broker-supplied greek). Uses the same LTP/spot/
            # strike every broker already gives us — no extra API calls.
            if d.get("delta") is None and days_to_expiry:
                bs_result = _bs.compute_for_leg(
                    spot, r["strike"], d["ltp"], days_to_expiry,
                    is_call=(leg == "ce"))
                if bs_result:
                    d["iv"] = d["iv"] or bs_result["iv"]
                    d["delta"] = bs_result["delta"]
                    d["gamma"] = bs_result["gamma"]
                    d["theta"] = bs_result["theta"]
                    d["vega"] = bs_result["vega"]
                    d["greeks_source"] = "bs_fallback"
            r[leg] = d

    # focus window: ~10 strikes either side of ATM
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"]
    idx = [i for i, r in enumerate(rows) if r["strike"] == atm][0]
    lo, hi = max(0, idx - 10), min(len(rows), idx + 11)
    win = rows[lo:hi]

    tot_ce_oi = sum(r["ce"].get("oi") or 0 for r in rows) or 1
    tot_pe_oi = sum(r["pe"].get("oi") or 0 for r in rows) or 1
    tot_ce_vol = sum(r["ce"].get("volume") or 0 for r in rows) or 1
    tot_pe_vol = sum(r["pe"].get("volume") or 0 for r in rows) or 1

    pcr_oi = tot_pe_oi / tot_ce_oi
    pcr_vol = tot_pe_vol / tot_ce_vol
    mp = max_pain(win)

    # Feature #4 PCR Engine — richer structured PCR (ATM/strike-wise/
    # trend), additive alongside the existing pcr_oi/pcr_volume scalars
    # above (unchanged, still the same numbers existing consumers read).
    pcr_full = pcr_engine(rows, win, atm, snapshot_ctx)

    # ranked signal lines (R1-R3 / S1-S3) from OI + ΔOI + volume
    signal_lines = ranked_levels(win, spot)
    resistance = [x["level"] for x in signal_lines["R"]]
    support = [x["level"] for x in signal_lines["S"]]

    # whole-chain color coding + sentiment interpretation (from the notes)
    colors = chain_colors(win)
    sentiment = sentiment_summary(win)

    # per-strike risk levels + dominance
    strikes_out = []
    for r in win:
        ce_state, ce_churn = classify_leg(r["ce"])
        pe_state, pe_churn = classify_leg(r["pe"])
        ce_score = _seller_risk(r["ce"], r["strike"], spot, side="CE")
        pe_score = _seller_risk(r["pe"], r["strike"], spot, side="PE")
        # Feature #4 (Option Chain Intelligence Engine) — additive
        # fields only, nothing existing changed: institutional_activity
        # relabels the same classify_leg() quadrant per the spec's 8
        # named categories; changes is None/None (both halves) unless
        # a caller passed snapshot_ctx (built from persisted chain
        # snapshots) — a caller not wired for snapshots gets exactly
        # the same output shape as before, just with these two extra
        # keys present and empty.
        ce_prev = pe_prev = ce_open = pe_open = None
        if snapshot_ctx:
            prev_map = snapshot_ctx.get("prev") or {}
            open_map = snapshot_ctx.get("session_open") or {}
            ce_prev = prev_map.get((r["strike"], "ce"))
            pe_prev = prev_map.get((r["strike"], "pe"))
            ce_open = open_map.get((r["strike"], "ce"))
            pe_open = open_map.get((r["strike"], "pe"))
        ce_changes = leg_changes(r["ce"], ce_prev, ce_open)
        pe_changes = leg_changes(r["pe"], pe_prev, pe_open)
        strikes_out.append({
            "strike": r["strike"],
            "ce": {**r["ce"], "state": ce_state, "churn": ce_churn,
                   "risk": ce_score, "risk_label": _risk_label(ce_score),
                   "wall": colors["ce"].get(r["strike"], {"pct": 0, "color": "none"}),
                   "premium": premium_color(r["ce"]["chg"], r["ce"]["ltp"]),
                   "institutional_activity": institutional_activity(ce_state, "ce"),
                   "premium_intelligence": premium_intelligence(ce_state, "ce"),
                   "changes": ce_changes,
                   "iv_greeks": iv_greeks_engine(r["ce"], ce_changes)},
            "pe": {**r["pe"], "state": pe_state, "churn": pe_churn,
                   "risk": pe_score, "risk_label": _risk_label(pe_score),
                   "wall": colors["pe"].get(r["strike"], {"pct": 0, "color": "none"}),
                   "premium": premium_color(r["pe"]["chg"], r["pe"]["ltp"]),
                   "institutional_activity": institutional_activity(pe_state, "pe"),
                   "premium_intelligence": premium_intelligence(pe_state, "pe"),
                   "changes": pe_changes,
                   "iv_greeks": iv_greeks_engine(r["pe"], pe_changes)},
        })

    # market state machine (positional OI view + live price momentum)
    bias, state, confidence = _market_state(
        spot, mp, pcr_oi, pcr_vol, support, resistance, win, momentum,
        indicators
    )

    # overall risk meter 0-100 (how hostile conditions are for fresh positions)
    dist_mp = abs(spot - mp) / spot * 100
    ivs = [r["ce"]["iv"] for r in win if r["ce"]["iv"]] + \
          [r["pe"]["iv"] for r in win if r["pe"]["iv"]]
    avg_iv = sum(ivs) / len(ivs) if ivs else 0
    risk_meter = min(100, round(
        dist_mp * 12 +                       # stretched from max pain
        (abs(pcr_oi - 1.0) * 25) +           # one-sided positioning
        (avg_iv * 1.2)                       # elevated IV
    ))

    suggestions = _suggest(bias, state, spot, atm, support, resistance, mp,
                           strikes_out, pcr_oi)

    # Feature #4 Smart Money Engine — chain-wide, computed after
    # strikes_out/signal_lines exist since it scans across them.
    smart_money = smart_money_engine(strikes_out, signal_lines, spot, snapshot_ctx)

    # Feature #4 Strike Strength Engine (fuller multi-factor score) —
    # merged back into strikes_out as a new additive per-leg field,
    # alongside (not replacing) the existing "wall" field.
    strength_map = strike_strength_engine(strikes_out, atm, spot)
    for s in strikes_out:
        leg_scores = strength_map.get(s["strike"], {})
        s["ce"]["strike_strength"] = leg_scores.get("ce")
        s["pe"]["strike_strength"] = leg_scores.get("pe")

    # Feature #4 AI Narrative + consolidated AI Output — both purely
    # derived from data already computed above, no new calculation.
    narrative = generate_narrative(strikes_out, smart_money, atm)
    ai_output = option_chain_ai_output(bias, support, resistance, atm,
                                       confidence, avg_iv, pcr_full,
                                       suggestions, strikes_out)

    return {
        "symbol": chain["symbol"],
        "spot": spot,
        "expiry": chain["expiry"],
        "exchange_timestamp": chain.get("timestamp"),
        "source": chain.get("source"),
        "atm": atm,
        "max_pain": mp,
        "pcr_oi": round(pcr_oi, 2),
        "pcr_volume": round(pcr_vol, 2),
        "pcr_engine": pcr_full,
        "avg_iv": round(avg_iv, 1),
        "support": support,
        "resistance": resistance,
        "signal_lines": signal_lines,
        "sentiment": sentiment,
        "indicators": indicators,
        "bias": bias,
        "momentum": momentum,
        "market_state": state,
        "confidence": confidence,
        "risk_meter": risk_meter,
        "strikes": strikes_out,
        "suggestions": suggestions,
        "smart_money": smart_money,
        "narrative": narrative,
        "ai_output": ai_output,
        "disclaimer": ("Educational decision-support only. Options carry high "
                       "risk; verify with your broker's live feed before "
                       "trading."),
    }


def _seller_risk(leg, strike, spot, side):
    """0 (safe for writer) -> 100 (danger zone). Mirrors the green/yellow/red
    zoning shown in LTP-calculator style tools."""
    dist = (strike - spot) / spot if side == "CE" else (spot - strike) / spot
    dist_score = max(0.0, 1.0 - max(dist, 0) * 40)        # near/ITM = risky
    vol_oi = leg["volume"] / leg["oi"] if leg["oi"] else 2.0
    churn_score = min(1.0, vol_oi / 3.0)                  # heavy churn = unstable
    unwind = 1.0 if leg["oi_chg"] < 0 and leg["chg"] > 0 else 0.0
    return round(min(100, (0.55 * dist_score + 0.30 * churn_score
                           + 0.15 * unwind) * 100))


def _risk_label(score):
    if score >= 70:
        return "danger"
    if score >= 40:
        return "caution"
    return "safe"


def _market_state(spot, mp, pcr_oi, pcr_vol, support, resistance, win,
                  momentum=None, indicators=None):
    bull = bear = 0
    if indicators:
        if indicators.get("macd_hist") is not None:
            if indicators["macd_hist"] > 0: bull += 1
            else: bear += 1
        k = indicators.get("stoch_k")
        if k is not None:
            if k < 20: bull += 1          # oversold zone (notes: buy zone)
            elif k > 80: bear += 1
    # live price action gets real weight: OI is positional, price is now
    if momentum:
        if momentum["trend"] == "rising":
            bull += 2
            if (momentum.get("pct_15m") or 0) > 0.3:
                bull += 1
        elif momentum["trend"] == "falling":
            bear += 2
            if (momentum.get("pct_15m") or 0) < -0.3:
                bear += 1
    if pcr_oi > 1.15: bull += 2
    elif pcr_oi > 1.0: bull += 1
    if pcr_oi < 0.85: bear += 2
    elif pcr_oi < 1.0: bear += 1
    if spot > mp: bull += 1
    else: bear += 1
    if pcr_vol > 1.1: bull += 1
    if pcr_vol < 0.9: bear += 1

    # writer behaviour near ATM
    near = [r for r in win if abs(r["strike"] - spot) / spot < 0.01]
    for r in near:
        if classify_leg(r["pe"])[0] == "short-buildup": bull += 1   # PE writing
        if classify_leg(r["ce"])[0] == "short-buildup": bear += 1   # CE writing

    net = bull - bear
    if net >= 3: bias = "BULLISH"
    elif net <= -3: bias = "BEARISH"
    elif net >= 1: bias = "MILD BULLISH"
    elif net <= -1: bias = "MILD BEARISH"
    else: bias = "NEUTRAL"

    rng = (min(resistance) - max(support)) / spot if support and resistance else 0
    if momentum and momentum["trend"] != "flat" and (
            ("BULL" in bias) != (momentum["trend"] == "rising")):
        state = (f"DIVERGENCE — OI positioning says {bias.lower()} but price "
                 f"is {momentum['trend']} ({momentum.get('pct_15m')}% /15m); "
                 "wait for alignment")
    elif support and resistance and max(support) < spot < min(resistance) and rng < 0.02:
        state = "RANGEBOUND (compression between OI walls)"
    elif abs(spot - mp) / spot > 0.012:
        state = "STRETCHED — reversal-watch toward max pain"
    else:
        state = "TRENDING with " + bias.lower() + " undertone"

    confidence = min(95, 40 + abs(net) * 10)
    return bias, state, confidence


def _suggest(bias, state, spot, atm, support, resistance, mp, strikes, pcr):
    out = []
    sup = max(support) if support else atm
    res = min(resistance) if resistance else atm

    def strike_row(k):
        for s in strikes:
            if s["strike"] == k:
                return s
        return None

    if bias in ("BULLISH", "MILD BULLISH"):
        row = strike_row(atm)
        ce_ltp = row["ce"]["ltp"] if row else 0
        out.append({
            "action": "BUY", "instrument": f"{atm} CE",
            "type": "directional",
            "entry_hint": f"near LTP ₹{ce_ltp:.1f} on a hold above support {sup}",
            "stoploss": f"spot close below {sup}",
            "target": f"resistance zone {res}",
            "rationale": f"{bias}: PCR {pcr}, PE writers defending {sup}, "
                         f"spot above max pain {mp}.",
        })
        out.append({
            "action": "SELL", "instrument": f"{sup} PE",
            "type": "premium-selling",
            "entry_hint": "only with defined risk (spread or hedge)",
            "stoploss": f"spot breaks {sup} decisively",
            "target": "premium decay to ~40-50%",
            "rationale": f"Highest PE OI wall at {sup} acting as support.",
        })
    elif bias in ("BEARISH", "MILD BEARISH"):
        row = strike_row(atm)
        pe_ltp = row["pe"]["ltp"] if row else 0
        out.append({
            "action": "BUY", "instrument": f"{atm} PE",
            "type": "directional",
            "entry_hint": f"near LTP ₹{pe_ltp:.1f} on rejection from {res}",
            "stoploss": f"spot close above {res}",
            "target": f"support zone {sup}",
            "rationale": f"{bias}: PCR {pcr}, CE writers capping {res}, "
                         f"spot vs max pain {mp}.",
        })
        out.append({
            "action": "SELL", "instrument": f"{res} CE",
            "type": "premium-selling",
            "entry_hint": "only with defined risk (spread or hedge)",
            "stoploss": f"spot breaks {res} decisively",
            "target": "premium decay to ~40-50%",
            "rationale": f"Highest CE OI wall at {res} acting as resistance.",
        })
    else:
        out.append({
            "action": "SELL", "instrument": f"{res} CE + {sup} PE (short strangle)",
            "type": "rangebound",
            "entry_hint": "both legs, hedged with far OTM wings (iron condor)",
            "stoploss": f"spot exits {sup}–{res} range",
            "target": "theta decay while range holds",
            "rationale": f"Neutral state: {state}. OI walls at {sup}/{res}, "
                         f"max pain {mp} near spot.",
        })
    return out


# ---------------------------------------------------------------- Claude AI

def ai_visual(analysis: dict, context: dict | None = None) -> dict:
    """Beginner-friendly structured insights for the infographic panel:
    verdict, plain-English summary, three scenarios with probabilities,
    and a key-level ladder. AI-generated when a key is set; otherwise
    derived from the rule engine."""
    import config as _cfg
    cfg = _cfg.load()
    base = _rule_visual(analysis)
    if cfg.get("ai_engine", "local") == "off":
        base["source"] = "rule-engine (AI off)"
        return base

    sym = analysis.get("symbol", "?")
    fp = _fingerprint(analysis)
    cached, reason = _ai_gate("visual", sym, fp)
    if cached is not None:
        return cached
    if reason and reason != "proceed":
        base["source"] = "rule-engine"
        return base

    compact = {k: analysis[k] for k in (
        "symbol", "spot", "atm", "max_pain", "pcr_oi", "bias",
        "market_state", "risk_meter", "momentum", "support", "resistance")}
    prompt = (
        "Explain this Indian index option chain to a BEGINNER. If momentum "
        "conflicts with OI bias, say so. JSON only:\n"
        '{"verdict":"BULLISH|BEARISH|NEUTRAL",'
        '"plain_summary":[<=3 short jargon-free sentences],'
        '"scenarios":[{"name":"Bullish","probability":<n>,"trigger":"<spot>",'
        '"then":"<outcome>","beginner_action":"<line>"},'
        '{"name":"Bearish",...},{"name":"Rangebound",...}],'
        '"watch_out":"<one line>"}\n'
        "Probabilities ~100. Use only given levels.\n" + json.dumps(compact)
    )
    text, err = _claude_json(prompt, None, 500)
    if err:
        base["source"] = f"rule-engine (AI unavailable: {err})"
        return base
    try:
        out = json.loads(text)
        out["source"] = "local/online AI"
        _ai_store("visual", sym, fp, out)
        return out
    except Exception as e:
        base["source"] = f"rule-engine (AI error: {e})"
        return base


def _rule_visual(analysis: dict) -> dict:
    bias = analysis["bias"]
    spot = analysis["spot"]
    sup = max([s for s in analysis["support"] if s < spot] or analysis["support"] or [spot])
    res = min([r for r in analysis["resistance"] if r > spot] or analysis["resistance"] or [spot])
    bull = 55 if "BULL" in bias else 25 if "BEAR" in bias else 33
    bear = 25 if "BULL" in bias else 55 if "BEAR" in bias else 33
    rng = 100 - bull - bear
    return {
        "verdict": "BULLISH" if "BULL" in bias else "BEARISH" if "BEAR" in bias else "NEUTRAL",
        "plain_summary": [
            f"The market is at {spot:,.0f}. Big option sellers have built a "
            f"floor near {sup:,.0f} and a ceiling near {res:,.0f}.",
            f"More traders are betting it stays above {sup:,.0f} than below "
            f"(PCR {analysis['pcr_oi']}).",
            "Prices tend to gravitate toward the 'max pain' level of "
            f"{analysis['max_pain']:,.0f} as expiry nears.",
        ],
        "scenarios": [
            {"name": "Bullish", "probability": bull,
             "trigger": f"holds above {sup:,.0f}",
             "then": f"drift toward {res:,.0f}",
             "beginner_action": "call (CE) buyers benefit; keep a stoploss"},
            {"name": "Bearish", "probability": bear,
             "trigger": f"breaks below {sup:,.0f}",
             "then": f"slide toward the next support",
             "beginner_action": "put (PE) buyers benefit; avoid catching the fall"},
            {"name": "Rangebound", "probability": rng,
             "trigger": f"stays between {sup:,.0f}–{res:,.0f}",
             "then": "option premiums slowly lose value (theta decay)",
             "beginner_action": "buyers lose to time decay; patience or stay out"},
        ],
        "key_levels": (
            [{"level": r, "label": "resistance (CE wall)", "kind": "resistance"}
             for r in sorted(set(analysis["resistance"]), reverse=True)] +
            [{"level": analysis["max_pain"], "label": "max pain", "kind": "pivot"}] +
            [{"level": s, "label": "support (PE wall)", "kind": "support"}
             for s in sorted(set(analysis["support"]), reverse=True)]
        ),
        "watch_out": ("High risk meter — avoid fresh positions"
                      if analysis["risk_meter"] > 70 else
                      "A close beyond the OI walls can trigger fast moves"),
    }


def ai_signal(analysis: dict, api_key: str | None = None,
              context: dict | None = None, log=None) -> dict:
    """Structured trade decision from Claude: exact entry/SL/target price
    points as JSON, rendered as a signal card in the UI and consumed by
    the autopilot. Falls back to the rule engine when no key is set.

    `log` is an optional sink for the invariant-repair layer below. It
    exists because that layer was completely unobservable: its message
    went to `enforce_signal_invariants`'s no-op default (this caller
    passed only three arguments), and the `invariant_repairs` field it
    writes instead was read by nothing — not app.py, not the dashboard.
    So from v58.44 until 2026-08-03 the repairs ran on real trades and
    left no trace anywhere, which is why "has the generator stopped
    ignoring its own prompt" could not be answered."""
    import config as _cfg
    api_key = None
    fallback = _rule_signal(analysis)
    if _cfg.load().get("ai_engine", "local") == "off":
        fallback["source"] = "rule-engine (AI off)"
        return fallback

    sym = analysis.get("symbol", "?")
    fp = _fingerprint(analysis)
    cached, reason = _ai_gate("signal", sym, fp)
    if cached is not None:
        return cached
    if reason and reason != "proceed":
        note = {"ai_disabled": "AI off in Settings",
                "daily_cap": "daily AI cap reached"}.get(reason, reason)
        fallback["source"] = f"rule-engine ({note})"
        return fallback

    # compact prompt: only ATM +/- 3 strikes and the essential aggregates.
    atm = analysis["atm"]
    near = [s for s in analysis["strikes"] if abs(s["strike"] - atm)
            <= 3 * (analysis["strikes"][1]["strike"] - analysis["strikes"][0]["strike"])
            ] if len(analysis["strikes"]) > 1 else analysis["strikes"]
    compact = {k: analysis[k] for k in (
        "symbol", "spot", "atm", "max_pain", "pcr_oi", "bias",
        "market_state", "risk_meter", "momentum")}
    compact["near_strikes"] = [
        {"k": s["strike"], "ce_ltp": s["ce"]["ltp"], "pe_ltp": s["pe"]["ltp"],
         "ce_st": s["ce"]["state"], "pe_st": s["pe"]["state"]}
        for s in near]
    if context:
        compact["ctx"] = {"news": (context.get("news") or {}).get("sentiment"),
                          "risk_event": (context.get("news") or {}).get("risk_event"),
                          "mood": context.get("social_mood"),
                          "macro": context.get("macro")}

    prompt = (
        "Indian index options. Output ONE trade decision as JSON only:\n"
        '{"signal":"BUY_CE|BUY_PE|WAIT","strike":<n>,"entry":<premium>,'
        '"stoploss":<premium>,"target1":<premium>,"target2":<premium>,'
        '"spot_invalidation":<spot>,"confidence":<0-100>,'
        '"reasons":[<=3 short],"risk_note":<short>}\n'
        "Rules: momentum is live price; if it conflicts with OI bias, follow "
        "momentum or WAIT. ATM/ITM strikes only, never OTM. Min 1:2 RR "
        "(target1-entry >= 2*(entry-stoploss)). WAIT if stale/closed/"
        "risk_meter>70/no edge. \"signal\" must be EXACTLY one of the "
        "three words BUY_CE, BUY_PE, or WAIT \u2014 never combine them, "
        "never output the word \"or\" or a \"|\" character.\n" + json.dumps(compact)
    )
    text, err = _claude_json(prompt, None, 400)
    if err:
        fallback["source"] = f"rule-engine (AI unavailable: {err})"
        return fallback
    try:
        sig = json.loads(text)
        # 2026-07-27 — real bug found from a live activity log: the
        # prompt above uses "BUY_CE|BUY_PE|WAIT" as a SCHEMA HINT
        # (pick one of these three) — a smaller local model can
        # literally echo that placeholder back as its answer instead
        # of choosing one. Nothing validated `sig["signal"]` against
        # the actual allowed values before this fix, so a malformed
        # "BUY_CE|BUY_PE" string sailed straight through into every
        # downstream check. The regime gate (`signal in allowed_
        # signals`) can never match a value that isn't literally
        # "BUY_CE" or "BUY_PE" — so this wasn't really "regime blocks
        # a 90%-confidence trade," it was "a malformed value was never
        # a valid trade in the first place," logged 65 times in one
        # session and easy to misread as a regime-gate problem. Same
        # "fail loud, not silent" principle already applied elsewhere
        # in this codebase: an invalid AI response now falls back to
        # the rule engine, exactly like a genuine AI error would.
        if sig.get("signal") not in ("BUY_CE", "BUY_PE", "WAIT"):
            fallback["source"] = (f"rule-engine (AI returned an invalid "
                                  f"signal value: {sig.get('signal')!r})")
            return fallback
        sig["source"] = "AI"
        _attach_security_id(sig, analysis)
        # 2026-07-29 — the SAME class of bug as the signal-value check
        # above, one field over. The prompt states a min 1:2 RR and
        # "ATM/ITM strikes only, never OTM"; nothing validated the
        # numbers. 26 of 63 daily rejections were the risk gate
        # catching a generator that had ignored its own instructions.
        try:
            import config as _cfg
            sig, _rep = enforce_signal_invariants(
                sig, analysis, _cfg.load(), log=log or (lambda m: None))
        except Exception as _e:
            # Never let the repair layer break signal generation — a
            # slightly wrong signal still faces the risk gate, but no
            # signal at all is a silent outage.
            sig.setdefault("invariant_repairs", []).append(f"check failed: {_e}")
        _ai_store("signal", sym, fp, sig)
        return sig
    except Exception as e:
        fallback["source"] = f"rule-engine (AI error: {e})"
        return fallback


def _rule_signal(analysis: dict) -> dict:
    """Deterministic signal derived from the rule engine."""
    bias = analysis["bias"]
    atm = analysis["atm"]
    row = next((s for s in analysis["strikes"] if s["strike"] == atm), None)
    stale = not any(s["ce"]["volume"] or s["pe"]["volume"]
                    for s in analysis["strikes"])
    if stale or analysis["risk_meter"] > 70 or bias == "NEUTRAL" or not row:
        sig = {"signal": "WAIT", "strike": atm, "entry": 0, "stoploss": 0,
               "target1": 0, "target2": 0,
               "spot_invalidation": analysis["max_pain"],
               "confidence": 40, "timeframe": "intraday",
               "reasons": ["No clear edge or stale/zero volume data",
                           f"Risk meter {analysis['risk_meter']}/100",
                           f"Bias: {bias}"],
               "risk_note": "Stand aside until live flow confirms."}
        return sig
    mom = analysis.get("momentum")
    leg = "ce" if "BULL" in bias else "pe"
    if mom and mom["trend"] != "flat":
        mom_leg = "ce" if mom["trend"] == "rising" else "pe"
        if mom_leg != leg:
            return {"signal": "WAIT", "strike": atm, "entry": 0,
                    "stoploss": 0, "target1": 0, "target2": 0,
                    "spot_invalidation": analysis["max_pain"],
                    "confidence": 45, "timeframe": "intraday",
                    "reasons": [
                        f"Divergence: OI bias {bias} but price is "
                        f"{mom['trend']} ({mom.get('pct_15m')}% /15m)",
                        "Waiting for OI and price action to align"],
                    "risk_note": "Trading against live momentum is the "
                                 "fastest way to lose premium."}
    ltp = row[leg]["ltp"]
    spot = analysis["spot"]
    sup = max([s for s in analysis["support"] if s < spot] or [spot * 0.996])
    res = min([s for s in analysis["resistance"] if s > spot] or [spot * 1.004])
    sig = {
        "signal": "BUY_CE" if leg == "ce" else "BUY_PE",
        "strike": atm,                      # ATM only — never OTM
        "entry": round(ltp, 1),
        # 2026-08-02 — was a flat 30% of premium with no volatility input.
        # Now the shared geometry, so this path cannot drift from the
        # others. `atr_pct` comes from the regime engine when available;
        # without it the shared fallback applies and SAYS SO.
        **dict(zip(("stoploss", "target1", "target2"),
                   option_stop_geometry(ltp, atr_pct=analysis.get("atr_pct"),
                                        spot=spot)[:3])),
        "spot_invalidation": sup if leg == "ce" else res,
        "confidence": analysis["confidence"],
        "timeframe": "intraday",
        "reasons": [f"Bias {bias}, PCR {analysis['pcr_oi']}",
                    f"OI walls: support {sup} / resistance {res}",
                    f"Spot vs max pain: {analysis['spot']:.0f} vs {analysis['max_pain']}"],
        "risk_note": "Rule-based: 30% SL with min 1:2 risk-reward "
                     "(T1 at +60%). ATM strike only, never OTM.",
    }
    _attach_security_id(sig, analysis)
    return sig


def enforce_signal_invariants(sig, analysis, cfg=None, log=lambda m: None):
    """Enforce the invariants the LLM prompt STATES but nothing checked.

    2026-07-29 — root cause of two separate rejection categories that
    together accounted for 26 of 63 daily rejections:

        18/day  "risk-reward 0.8 (need >=2.0)"
         8/day  "strike 24100 not OTM (ATM 24200)"

    The prompt tells the model "Min 1:2 RR (target1-entry >=
    2*(entry-stoploss))" and "ATM/ITM strikes only, never OTM" — and
    then nothing validated the reply. A live example: entry 190.35, SL
    160.55, T1 213.8 gives rr 0.79, and strike 24100 on a PE with ATM
    24200 is OTM. The model violated BOTH stated rules and the signal
    was built anyway, travelled the whole pipeline, and was thrown away
    at the risk gate.

    The rule-engine FALLBACK never had this problem: it hard-codes
    ltp*0.70 / ltp*1.60 (rr exactly 2.0) and strike = ATM. So the gate
    was right and the generator was wrong — which is why the fix is
    here and not a looser gate.

    Repairs rather than rejects, because a directionally-correct signal
    with bad arithmetic is still worth taking at corrected levels. Every
    repair is logged so how often the model disobeys is visible rather
    than silent.
    """
    cfg = cfg or {}
    repairs = []
    if not sig or sig.get("signal") not in ("BUY_CE", "BUY_PE"):
        return sig, repairs
    atm = analysis.get("atm")
    entry = sig.get("entry") or 0
    sl = sig.get("stoploss") or 0

    # --- strike policy (the prompt says ATM/ITM only) ---
    policy = cfg.get("option_strike_policy", "atm_or_itm")
    strike = sig.get("strike")
    if atm and strike is not None and policy != "any":
        if policy == "atm_or_itm":
            bad = (sig["signal"] == "BUY_CE" and strike > atm) or \
                  (sig["signal"] == "BUY_PE" and strike < atm)
        else:
            bad = (sig["signal"] == "BUY_CE" and strike < atm) or \
                  (sig["signal"] == "BUY_PE" and strike > atm)
        if bad:
            repairs.append(f"strike {strike} -> {atm} (policy {policy})")
            sig["strike"] = atm
            _attach_security_id(sig, analysis)
            # The premium belongs to the OLD strike; re-read it or the
            # entry price describes a contract we are not buying.
            row = next((r for r in analysis.get("strikes", [])
                        if r["strike"] == atm), None)
            leg = "ce" if sig["signal"] == "BUY_CE" else "pe"
            new_ltp = row and row[leg].get("ltp")
            if new_ltp:
                repairs.append(f"entry {entry} -> {new_ltp} (re-read at ATM)")
                entry = sig["entry"] = round(new_ltp, 1)
                # Re-derive the stop for the NEW strike through the shared
                # geometry rather than reapplying a flat 30%: the premium
                # just changed, so the risk fraction has to be recomputed
                # on the same basis every other path uses.
                sl = sig["stoploss"] = option_stop_geometry(
                    new_ltp, atr_pct=analysis.get("atr_pct"),
                    spot=analysis.get("spot"))[0]

    # --- stop WIDTH (2026-08-04, B8) ---
    # Found on the FIRST live day the repair layer had somewhere to log:
    #
    #     NIFTY BUY_CE 24600   entry 132.8   stop 9.3   target 379.8
    #
    # A 93% stop — the option must lose 93% of its value before stopping
    # out — and it scored EXACTLY 2.00 on the RR floor below. That is the
    # hole: rr = (target-entry)/(entry-stop), so a generator can satisfy
    # "min 1:2" by WIDENING THE STOP instead of moving the target, and
    # nothing here had an opinion about the denominator. The check above
    # only asks `0 < sl < entry`, which is validity, not width.
    #
    # The rupee caps do not close this. They bound the AMOUNT, not the
    # FRACTION: at entry 132.8 a 93% stop risks ₹8,028 and is blocked,
    # but at entry 30 the identical stop risks ₹1,814 and passes both the
    # ₹2,000 per-trade cap and the ₹7,000 daily limit — and cheap OTM
    # options are exactly what this system buys.
    #
    # Clamped through STOP_BOUNDS, the same bounds every other stop path
    # already uses, rather than a second set of numbers here. Done BEFORE
    # the RR floor on purpose: the RR repair divides by (entry - sl), so
    # it must see the corrected stop or it would size a target off a stop
    # that is about to change.
    if entry > 0 and 0 < sl < entry:
        _lo, _hi = STOP_BOUNDS
        _frac = (entry - sl) / entry
        if not (_lo <= _frac <= _hi):
            # Round in the SAFE direction, not to-nearest. round(132.8 *
            # 0.40, 1) is 53.1, which is 60.015% of premium — a hair OVER
            # the 60% bound the clamp exists to enforce. A risk bound
            # breached by rounding is not a bound. Too-wide clamps DOWN so
            # the stop rounds UP (tighter); too-tight clamps UP so it
            # rounds DOWN (wider). Either way the fraction lands inside.
            import math as _math
            _raw = entry * (1 - min(max(_frac, _lo), _hi))
            _new_sl = (_math.ceil(_raw * 10) / 10 if _frac > _hi
                       else _math.floor(_raw * 10) / 10)
            repairs.append(f"stoploss {sl} -> {_new_sl} "
                           f"(stop {100*_frac:.0f}% of premium, outside "
                           f"{100*_lo:.0f}-{100*_hi:.0f}%)")
            sl = sig["stoploss"] = _new_sl

    # --- risk-reward floor (the prompt says min 1:2) ---
    min_rr = cfg.get("signal_min_rr", 2.0)
    if entry > 0 and 0 < sl < entry:
        rr = (sig.get("target1", 0) - entry) / (entry - sl)
        if rr < min_rr:
            new_t1 = round(entry + min_rr * (entry - sl), 1)
            repairs.append(f"target1 {sig.get('target1')} -> {new_t1} "
                           f"(rr {rr:.2f} -> {min_rr:.2f})")
            sig["target1"] = new_t1
            if sig.get("target2", 0) <= new_t1:
                sig["target2"] = round(entry + min_rr * 1.33 * (entry - sl), 1)
    elif entry > 0:
        # Degenerate stop (>= entry, or missing). Fall back to the
        # rule-engine's own fractions rather than inventing something.
        repairs.append(f"stoploss {sl} invalid vs entry {entry} — "
                       "reset to rule-engine 30%/60%")
        _sl, _t1, _t2, _m = option_stop_geometry(entry)
        sig["stoploss"] = _sl
        sig["target1"] = round(entry * 1.60, 1)
        sig["target2"] = round(entry * 2.05, 1)

    if repairs:
        sig["invariant_repairs"] = repairs
        log(f"LLM signal repaired ({len(repairs)}): " + " ; ".join(repairs))
    return sig, repairs


def _attach_security_id(sig: dict, analysis: dict):
    if sig.get("signal") not in ("BUY_CE", "BUY_PE"):
        return
    leg = "ce" if sig["signal"] == "BUY_CE" else "pe"
    row = next((s for s in analysis["strikes"]
                if s["strike"] == sig.get("strike")), None)
    if row:
        sig["security_id"] = row[leg].get("security_id")
        sig["option_ltp"] = row[leg]["ltp"]


def ai_deep_dive(analysis: dict, context: dict | None = None) -> dict:
    """Structured deep-dive for the 'AI Deep Analysis' panel: writer
    behaviour, risk zones, scenarios and a critique — rendered as cards
    instead of a wall of narrative text. Falls back to a rule-derived
    structured summary if no key / on AI error."""
    import config as _cfg
    base = _rule_deep_dive(analysis)
    if _cfg.load().get("ai_engine", "local") == "off":
        base["source"] = "rule-engine (AI off)"
        return base

    sym = analysis.get("symbol", "?")
    fp = _fingerprint(analysis)
    cached, reason = _ai_gate("deepdive", sym, fp)
    if cached is not None:
        return cached
    if reason and reason != "proceed":
        base["source"] = "rule-engine"
        return base

    compact = {k: analysis[k] for k in (
        "symbol", "spot", "atm", "max_pain", "pcr_oi", "bias",
        "market_state", "risk_meter", "momentum", "sentiment",
        "support", "resistance")}
    # 2026-07-28 — per direct request, this analysis should weigh
    # market sentiment, institutional impact, technical analysis, and
    # trading behaviour, not just option-chain flow. `context` was
    # accepted as a parameter before this but never actually used
    # anywhere in this function's body (a real, silent gap — the
    # caller's news/social/macro context was computed and passed for
    # nothing). Folded in properly now, alongside the two genuinely
    # new inputs (technical, institutional) and trading_behavior
    # (aggregated from existing closed-trade records, not a new
    # tracking mechanism) that api_ai() now also provides.
    if context:
        compact["market_sentiment_ctx"] = {
            "news": (context.get("news") or {}).get("sentiment"),
            "social_mood": context.get("social_mood"),
            "macro_stance": context.get("macro")}
        if context.get("technical"):
            compact["technical_analysis"] = context["technical"]
        if context.get("institutional"):
            compact["institutional_activity"] = context["institutional"]
        if context.get("trading_behavior"):
            compact["recent_trading_behavior"] = context["trading_behavior"]
    prompt = (
        "Options-flow analyst. Weigh ALL of: option-chain writer flow, "
        "market sentiment, institutional activity, technical analysis, "
        "and recent trading behaviour for this symbol (whatever of "
        "these are present in the data below) — not option-chain flow "
        "alone. JSON only:\n"
        '{"writer_behavior":[<=4 short bullets],'
        '"risk_zones":[{"level":<n>,"type":"support|resistance","note":"<short>"}],'
        '"scenarios":[{"name":"Bullish","probability":<n>,"trigger":"<spot>",'
        '"then":"<outcome>"},{"name":"Bearish",...},{"name":"Rangebound",...}],'
        '"critique":[<=3 bullets on the rule signal: RR valid? momentum aligned?],'
        '"watch_out":"<one line>"}\n' + json.dumps(compact)
    )
    text, err = _claude_json(prompt, None, 600)
    if err:
        base["source"] = f"rule-engine (AI unavailable: {err})"
        return base
    try:
        out = json.loads(text)
        out["source"] = "local AI"
        _ai_store("deepdive", sym, fp, out)
        return out
    except Exception as e:
        base["source"] = f"rule-engine (AI error: {e})"
        return base


def _rule_deep_dive(analysis: dict) -> dict:
    bias = analysis["bias"]
    sent = analysis.get("sentiment", {})
    danger = [s for s in analysis["strikes"]
              if s["ce"]["risk_label"] == "danger" or s["pe"]["risk_label"] == "danger"]
    risk_zones = []
    for r in analysis.get("resistance", [])[:2]:
        risk_zones.append({"level": r, "type": "resistance",
                           "note": "CE OI wall — likely cap on upside"})
    for s in analysis.get("support", [])[:2]:
        risk_zones.append({"level": s, "type": "support",
                           "note": "PE OI wall — likely floor on downside"})
    bull = 55 if "BULL" in bias else 25 if "BEAR" in bias else 33
    bear = 25 if "BULL" in bias else 55 if "BEAR" in bias else 33
    return {
        "writer_behavior": [
            sent.get("summary", "No dominant writer behaviour detected."),
            sent.get("hint", ""),
            f"{len(danger)} strikes in the visible window are in the 'danger' "
            "risk zone for option writers.",
        ],
        "risk_zones": risk_zones,
        "scenarios": [
            {"name": "Bullish", "probability": bull,
             "trigger": f"holds above {max(analysis['support'], default=analysis['atm']):,.0f}",
             "then": "drift toward resistance"},
            {"name": "Bearish", "probability": bear,
             "trigger": f"breaks below {max(analysis['support'], default=analysis['atm']):,.0f}",
             "then": "slide toward next support"},
            {"name": "Rangebound", "probability": max(0, 100 - bull - bear),
             "trigger": "stays between the OI walls",
             "then": "premiums decay with time (theta)"},
        ],
        "critique": [
            f"Rule engine confidence: {analysis['confidence']}%.",
            "Momentum " + ("aligned with OI bias." if not (analysis.get("momentum")
                and (("BULL" in bias) != (analysis["momentum"]["trend"] == "rising")))
                else "DIVERGES from OI bias — treat rule signal with caution."),
        ],
        "watch_out": ("High risk meter — avoid fresh positions"
                      if analysis["risk_meter"] > 70 else
                      "A close beyond the OI walls can trigger fast moves"),
    }


def deep_ai_analysis(analysis: dict, api_key: str | None = None) -> str:
    """Optional: send the computed summary to Claude for narrative deep-dive.
    Requires ANTHROPIC_API_KEY in environment. Falls back gracefully."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        import config as _cfg
        api_key = _cfg.load().get("anthropic_api_key")
    if not api_key:
        return ("(Add your Anthropic API key in Settings to enable Claude "
                "deep analysis. Showing rule-engine output only.)")

    compact = {k: analysis[k] for k in (
        "symbol", "spot", "expiry", "atm", "max_pain", "pcr_oi", "pcr_volume",
        "avg_iv", "support", "resistance", "bias", "market_state",
        "risk_meter", "suggestions")}
    compact["top_strikes"] = [
        {"strike": s["strike"],
         "ce": {k: s["ce"][k] for k in ("ltp", "oi", "oi_chg", "volume", "state", "risk_label")},
         "pe": {k: s["pe"][k] for k in ("ltp", "oi", "oi_chg", "volume", "state", "risk_label")}}
        for s in analysis["strikes"][::2]
    ]

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 900,
        "messages": [{
            "role": "user",
            "content": (
                "You are an options-flow analyst. Given this Indian index "
                "option-chain summary (volume/OI based), write a concise "
                "deep-dive: 1) what writers are doing, 2) key risk zones, "
                "3) scenarios (bull/bear/range) with invalidation levels, "
                "4) critique of the rule-engine suggestions. Be specific, "
                "no fluff, end with a one-line risk warning.\n\n"
                + json.dumps(compact)
            ),
        }],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as e:
        return f"(Claude deep analysis unavailable: {e})"
