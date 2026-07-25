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


def analyze(chain: dict, momentum: dict | None = None,
            indicators: dict | None = None) -> dict:
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
        strikes_out.append({
            "strike": r["strike"],
            "ce": {**r["ce"], "state": ce_state, "churn": ce_churn,
                   "risk": ce_score, "risk_label": _risk_label(ce_score),
                   "wall": colors["ce"].get(r["strike"], {"pct": 0, "color": "none"}),
                   "premium": premium_color(r["ce"]["chg"], r["ce"]["ltp"])},
            "pe": {**r["pe"], "state": pe_state, "churn": pe_churn,
                   "risk": pe_score, "risk_label": _risk_label(pe_score),
                   "wall": colors["pe"].get(r["strike"], {"pct": 0, "color": "none"}),
                   "premium": premium_color(r["pe"]["chg"], r["pe"]["ltp"])},
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
              context: dict | None = None) -> dict:
    """Structured trade decision from Claude: exact entry/SL/target price
    points as JSON, rendered as a signal card in the UI and consumed by
    the autopilot. Falls back to the rule engine when no key is set."""
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
        "risk_meter>70/no edge.\n" + json.dumps(compact)
    )
    text, err = _claude_json(prompt, None, 400)
    if err:
        fallback["source"] = f"rule-engine (AI unavailable: {err})"
        return fallback
    try:
        sig = json.loads(text)
        sig["source"] = "AI"
        _attach_security_id(sig, analysis)
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
        "stoploss": round(ltp * 0.70, 1),   # risk = 30% of premium
        "target1": round(ltp * 1.60, 1),    # reward = 60% -> RR 1:2 minimum
        "target2": round(ltp * 2.05, 1),
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
    prompt = (
        "Options-flow analyst. JSON only:\n"
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
