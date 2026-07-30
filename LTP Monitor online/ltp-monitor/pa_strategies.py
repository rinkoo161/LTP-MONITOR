"""pa_strategies.py — price-action intraday strategies on index candles.

Three strategies, each returning a directional setup or None:
  orb            : opening-range breakout with session-anchor filter
  vwap_pullback  : trend-following pullback to the session anchor
  ema_mtf        : 9/20 EMA cross with multi-timeframe confirmation

Note on "VWAP": index spot has no volume (NIFTY is a calculated value),
so the session anchor is the cumulative typical-price mean (TWAP proxy).
Same anchoring idea; honest about the data.

Every parameter has BOUNDS so the daily adaptive tuner can relax filters
(when the strategy isn't trading) or tighten them (when it's bleeding)
WITHOUT ever leaving safe ranges. Relaxation order is deliberate: soft
filters (anchor side, MTF confirm) drop before core ones.
"""

PA_NAMES = ("orb", "vwap_pullback", "ema_mtf", "sg_ema", "momentum_confluence",
            "ew_reversal")

PA_DEFAULTS = {
    "orb": {"or_minutes": 5, "buf_frac": 0.10, "min_or_range_pct": 0.08,
            "anchor_filter": 1, "max_trades_per_day": 3},
    # v58.48 — deck setups 4/5/6 as OPTIONAL confirmations. All OFF and
    # min_confirmations 0, so S3 behaves exactly as it did before until
    # deliberately enabled. S3 has live trade history; changing it
    # silently would destroy the ability to compare before and after.
    "vwap_pullback": {"band_pct": 0.10, "trend_ema": 20, "resume_ema": 9,
                      "require_tide": 0, "require_macd_zero_reversal": 0,
                      "require_hidden_divergence": 0, "require_bb_confirm": 0,
                      "min_confirmations": 0,
                      "max_trades_per_day": 3},
    "ema_mtf": {"fast": 5, "slow": 13, "mtf_confirm": 1,
                "max_trades_per_day": 2},
    # Strategy 7 (v51) — Structure-Gated EMA Cross. Deliberately shares
    # ema_mtf's cross+MTF machinery (the spec's overlap warning: S7 is
    # NOT a new signal, it is S4 plus two gates). If the Shadow Journal
    # later shows S4 and S7 firing on near-identical bars, collapse them
    # into one strategy with the gates as toggles — decide AFTER data.
    "sg_ema": {"fast": 5, "slow": 13, "mtf_confirm": 1,
               "require_structure": 1, "require_ai_bias": 1,
               "min_ai_bias": 20, "structural_stop_buffer_pct": 0.05,
               "rr_target": 2.0, "max_trades_per_day": 2},
    # 2026-07-27 — ported from a TradingView Pine Script strategy the
    # person had already validated live (per their own screenshots —
    # "Long +8"/"-8 Short"/"Exit: MACD turn down" labels matching this
    # logic exactly). Two independent entry paths: a 4-way confluence
    # reversal (RSI divergence + recent EMA cross + MACD/RSI/Stoch/EMA
    # agreement) and a standalone "weapon" pattern (equal highs/lows +
    # an EMA break). See evaluate()'s own docstring for the ONE
    # deliberate simplification versus the original Pine Script (the
    # MACD-histogram-turn early exit).
    "momentum_confluence": {
        "ema_fast": 5, "ema_slow": 13, "macd_fast": 12, "macd_slow": 26,
        "macd_signal": 9, "rsi_len": 14, "rsi_bear_thresh": 40,
        "rsi_bull_thresh": 60, "stoch_len": 14, "stoch_k_smooth": 3,
        "stoch_d_smooth": 3, "stoch_ob": 80, "stoch_os": 20,
        "pivot_left": 5, "pivot_right": 5, "min_confluence": 3,
        "hist_turn_confirm_bars": 2,
        "cross_lookback": 10, "eq_tol_pct": 0.15, "rr_target": 2.0,
        "max_trades_per_day": 3,
    },
    # Strategy 8 (v58.28) — EW-Reversal, ported from the Avadhut Sathe
    # "Get the Ultimate Edge" deck. Three reversal detectors under ONE
    # id (ending diagonal / H&S / failed H&S); see ew_reversal.py for
    # why they are one strategy and not three. Registered here purely
    # so it inherits the existing machinery — DEFAULT_PARAMS, the
    # bounds clamp in backtester.get_params(), the Strategies table and
    # the tuner — rather than growing a parallel set of all four.
    #
    # rr_target's LOWER bound is 1.95, not lower: the RiskAgent's
    # risk-reward gate rejects anything below that, so a tuner allowed
    # to step underneath it would silently produce a strategy whose
    # every signal is auto-rejected downstream.
    "ew_reversal": {
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "min_pattern_bars": 12, "shoulder_tol_pct": 1.5,
        "neckline_buffer_pct": 0.05, "stop_buffer_pct": 0.05,
        "require_macd_divergence": 1, "require_tide": 1,
        "require_tide_all_detectors": 1,
        "ending_diagonal_enabled": 1, "hs_enabled": 1,
        "failed_hs_enabled": 1,
        "rr_target": 2.0, "max_trades_per_day": 2,
    },
}

# (lo, hi, relax_direction)  relax moves toward the permissive end
PA_BOUNDS = {
    "orb": {"buf_frac": (0.02, 0.25, -1), "min_or_range_pct": (0.02, 0.30, -1),
            "anchor_filter": (0, 1, -1)},
    "vwap_pullback": {"band_pct": (0.05, 0.35, +1),
                      "require_tide": (0, 1, -1),
                      "require_macd_zero_reversal": (0, 1, -1),
                      "require_hidden_divergence": (0, 1, -1),
                      "require_bb_confirm": (0, 1, -1),
                      "min_confirmations": (0, 4, -1)},
    "ema_mtf": {"mtf_confirm": (0, 1, -1)},
    "sg_ema": {"mtf_confirm": (0, 1, -1), "require_structure": (0, 1, -1),
               "require_ai_bias": (0, 1, -1), "min_ai_bias": (0, 60, -1),
               "structural_stop_buffer_pct": (0.0, 0.30, +1),
               "rr_target": (1.95, 3.0, -1)},
    "momentum_confluence": {"min_confluence": (2, 4, -1),
                            "hist_turn_confirm_bars": (1, 4, +1),
                            "cross_lookback": (5, 20, +1),
                            "eq_tol_pct": (0.05, 0.40, +1),
                            "rr_target": (1.5, 3.0, -1)},
    "ew_reversal": {"min_pattern_bars": (6, 40, -1),
                    "shoulder_tol_pct": (0.5, 5.0, +1),
                    "neckline_buffer_pct": (0.0, 0.30, -1),
                    "stop_buffer_pct": (0.0, 0.30, +1),
                    "require_macd_divergence": (0, 1, -1),
                    "require_tide": (0, 1, -1),
                    "require_tide_all_detectors": (0, 1, -1),
                    "rr_target": (1.95, 3.5, -1)},
}

PA_META = {
    "orb": {"title": "Opening Range Breakout (5m + anchor)",
            "bias": "both directions"},
    "vwap_pullback": {"title": "Anchor Pullback (trend-following)",
                      "bias": "with the trend"},
    "ema_mtf": {"title": "5/13 EMA Cross (MTF confirmed)",
                "bias": "with the cross"},
    "sg_ema": {"title": "Strategy 7 — Structure-Gated EMA Cross (SG-EMA)",
               "bias": "with the cross, gated by market structure + AI bias"},
    "momentum_confluence": {"title": "Momentum Confluence (ported from TradingView Pine Script)",
                            "bias": "both directions, reversal-oriented"},
    "ew_reversal": {"title": "Strategy 8 \u2014 EW-Reversal (ending diagonal / H&S / failed H&S)",
                    "bias": "counter-trend on reversal patterns, "
                            "with-trend on failed H&S"},
}


def _ema(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _anchor(candles):
    """Cumulative typical-price mean for the session so far."""
    tot = 0.0
    out = []
    for i, c in enumerate(candles, 1):
        tot += (c["high"] + c["low"] + c["close"]) / 3
        out.append(tot / i)
    return out


def _pivot_at(series, idx, left, right):
    """Matches Pine Script's ta.pivotlow()/ta.pivothigh() semantics
    exactly: the value at `idx` is a confirmed pivot low/high only once
    `right` bars AFTER it have printed (so it can be compared against
    both sides), returning (is_low, is_high) for that index — or
    (False, False) if there aren't enough bars on either side yet.
    Needed for RSI divergence (pivot points ON THE RSI SERIES, not
    price) and the "weapon" equal-high/low pattern (pivots on price) —
    momentum_confluence is the first strategy in this module needing a
    fixed-lookback pivot on an arbitrary series, distinct from
    structure.py's zigzag_series (a deviation-based, price-only pivot
    used for Strategy 7's structure gate — a different definition of
    "pivot" entirely, not interchangeable with this one)."""
    if idx - left < 0 or idx + right >= len(series):
        return False, False
    window = series[idx - left:idx + right + 1]
    center = series[idx]
    if center is None or any(v is None for v in window):
        return False, False
    is_low = center == min(window)
    is_high = center == max(window)
    return is_low, is_high


def _rsi_divergence(closes, highs, lows, rsi_s, pivot_left, pivot_right, i):
    """Mirrors the Pine Script's sequential `var`-tracked divergence
    check exactly: scans forward tracking the LAST confirmed RSI pivot
    low/high (and the price at that same bar) in order, and returns
    (bullish_confirmed_at_i, bearish_confirmed_at_i) — True only on the
    EXACT bar where the SECOND pivot of a divergence pair is confirmed
    (Pine confirms a pivot `pivot_right` bars after it forms, so the
    divergence itself is only visible `pivot_right` bars after the
    swing point — this lag is inherent to the pattern, not a
    simplification introduced by this port)."""
    rsi_trunc = rsi_s[:i + 1]
    last_low_rsi = last_low_price = None
    last_high_rsi = last_high_price = None
    bullish_now = bearish_now = False
    for idx in range(len(rsi_trunc)):
        is_low, is_high = _pivot_at(rsi_trunc, idx, pivot_left, pivot_right)
        confirmed_at = idx + pivot_right
        if is_low:
            price_at_low = lows[idx]
            if last_low_rsi is not None and confirmed_at == i:
                bullish_now = (price_at_low < last_low_price and
                              rsi_trunc[idx] > last_low_rsi)
            last_low_rsi, last_low_price = rsi_trunc[idx], price_at_low
        if is_high:
            price_at_high = highs[idx]
            if last_high_rsi is not None and confirmed_at == i:
                bearish_now = (price_at_high > last_high_price and
                              rsi_trunc[idx] < last_high_rsi)
            last_high_rsi, last_high_price = rsi_trunc[idx], price_at_high
    return bullish_now, bearish_now


def _last_two_price_pivots(highs, lows, pivot_left, pivot_right, i):
    """The last TWO confirmed price pivot highs and lows as of bar i
    (in chronological order, oldest first) — for the "weapon" equal-
    high/low pattern, which (unlike RSI divergence above) doesn't need
    the second pivot confirmed on this EXACT bar, just the most recent
    two seen so far, matching Pine's `lastHi1`/`lastHi2` var pair."""
    highs_trunc, lows_trunc = highs[:i + 1], lows[:i + 1]
    hi1 = hi2 = lo1 = lo2 = None
    for idx in range(len(highs_trunc)):
        _, is_high = _pivot_at(highs_trunc, idx, pivot_left, pivot_right)
        is_low, _ = _pivot_at(lows_trunc, idx, pivot_left, pivot_right)
        if is_high:
            hi2 = hi1
            hi1 = highs_trunc[idx]
        if is_low:
            lo2 = lo1
            lo1 = lows_trunc[idx]
    return (hi2, hi1), (lo2, lo1)


def evaluate(name, c1, c5=None, c15=None, params=None, taken_today=0,
            precomputed=None):
    """c1/c5/c15: today's session candles (dicts with open/high/low/close),
    oldest first, last = current. Returns setup dict or None:
      {dir:+1|-1, entry_spot, stop_spot, t1_spot, t2_spot, why}

    `precomputed`, if given, is {"anchor": [...], "ema": {period: [...]}}
    — full-day series computed ONCE, indexed here instead of recomputed
    over the growing window every call. Backtest replay uses this (same
    math, O(1) lookup instead of O(n) recompute per minute — the
    difference between an O(n) and an O(n²) full-day replay). Live
    trading calls without it since a single day's cost there is small
    and doesn't compound across hundreds of archived days.
    """
    p = dict(PA_DEFAULTS.get(name, {}), **(params or {}))
    if not c1 or len(c1) < 10 or taken_today >= p.get("max_trades_per_day", 3):
        return None
    closes = [c["close"] for c in c1]
    spot = closes[-1]
    i = len(c1) - 1   # current index within the (possibly precomputed) day
    if precomputed:
        anchor = precomputed["anchor"][i]
    else:
        anchor = _anchor(c1)[-1]

    def _ema_at(period, idx):
        if precomputed and period in precomputed.get("ema", {}):
            return precomputed["ema"][period][idx]
        return _ema(closes[:idx + 1], period)[-1]

    def _anchor_at(idx):
        if precomputed:
            return precomputed["anchor"][idx]
        return _anchor(c1[:idx + 1])[-1]

    if name == "orb":
        n = int(p["or_minutes"])
        if len(c1) <= n + 1:
            return None
        orh = max(c["high"] for c in c1[:n])
        orl = min(c["low"] for c in c1[:n])
        rng = orh - orl
        if rng < spot * p["min_or_range_pct"] / 100:
            return None                      # dead open — no energy
        buf = rng * p["buf_frac"]
        prev = closes[-2]
        if spot > orh + buf and prev <= orh + buf:
            if p["anchor_filter"] and spot < anchor:
                return None
            return {"dir": +1, "entry_spot": spot, "stop_spot": orl,
                    "t1_spot": spot + rng, "t2_spot": spot + 2 * rng,
                    "why": f"broke OR high {orh:.0f} (range {rng:.0f})"}
        if spot < orl - buf and prev >= orl - buf:
            if p["anchor_filter"] and spot > anchor:
                return None
            return {"dir": -1, "entry_spot": spot, "stop_spot": orh,
                    "t1_spot": spot - rng, "t2_spot": spot - 2 * rng,
                    "why": f"broke OR low {orl:.0f} (range {rng:.0f})"}
        return None

    if name == "vwap_pullback":
        if len(c1) < 30:
            return None
        resume_n = int(p["resume_ema"])
        e_res_last = _ema_at(resume_n, i)
        e_res_prev = _ema_at(resume_n, i - 1)
        band = spot * p["band_pct"] / 100
        prev_dist = abs(closes[-2] - _anchor_at(i - 1))
        trending_up = spot > anchor and closes[-1] > e_res_last
        trending_dn = spot < anchor and closes[-1] < e_res_last
        touched = prev_dist <= band
        need = int(p.get("min_confirmations", 0))

        def _confirmed(d):
            """Deck setups 4/5/6 as a quality filter. At the default
            min_confirmations of 0 this returns immediately without
            evaluating anything, so S3 is byte-identical to v58.47."""
            if need <= 0:
                return True, {}
            met, det = pullback_confirmations(c1, c5, c15, d, p)
            return met >= need, det

        if touched and trending_up and closes[-2] < e_res_prev:
            _ok, _det = _confirmed(+1)
            if not _ok:
                return None
            return {"dir": +1, "entry_spot": spot, "confirmations": _det,
                    "stop_spot": anchor - band * 2,
                    "t1_spot": spot + (spot - anchor) + band,
                    "t2_spot": spot + 2 * ((spot - anchor) + band),
                    "why": f"pullback to anchor {anchor:.0f}, resumed up"}
        if touched and trending_dn and closes[-2] > e_res_prev:
            _ok, _det = _confirmed(-1)
            if not _ok:
                return None
            return {"dir": -1, "entry_spot": spot, "confirmations": _det,
                    "stop_spot": anchor + band * 2,
                    "t1_spot": spot - (anchor - spot) - band,
                    "t2_spot": spot - 2 * ((anchor - spot) + band),
                    "why": f"pullback to anchor {anchor:.0f}, resumed down"}
        return None

    if name == "ema_mtf":
        if len(c1) < 25:
            return None
        fast_n, slow_n = int(p["fast"]), int(p["slow"])
        f_last, f_prev = _ema_at(fast_n, i), _ema_at(fast_n, i - 1)
        s_last, s_prev = _ema_at(slow_n, i), _ema_at(slow_n, i - 1)
        crossed_up = f_last > s_last and f_prev <= s_prev
        crossed_dn = f_last < s_last and f_prev >= s_prev
        if not (crossed_up or crossed_dn):
            return None
        if p["mtf_confirm"]:
            for tf in (c5, c15):
                if not tf or len(tf) < int(p["slow"]) + 2:
                    return None
                tfc = [c["close"] for c in tf]
                tf_bull = _ema(tfc, int(p["fast"]))[-1] > _ema(tfc, int(p["slow"]))[-1]
                if crossed_up and not tf_bull:
                    return None
                if crossed_dn and tf_bull:
                    return None
        risk = max(spot * 0.0015, abs(f_last - s_last) * 3)
        d = +1 if crossed_up else -1
        return {"dir": d, "entry_spot": spot, "stop_spot": spot - d * risk,
                "t1_spot": spot + d * risk, "t2_spot": spot + d * 2 * risk,
                "why": f"{int(p['fast'])}/{int(p['slow'])} EMA cross "
                       + ("up" if d > 0 else "down")
                       + (" + MTF confirmed" if p["mtf_confirm"] else "")}
    if name == "momentum_confluence":
        import mtf_confluence_strategy as mcs
        n = int(p["pivot_left"]) + int(p["pivot_right"]) + 2
        if len(c1) < max(n, int(p["macd_slow"]) + int(p["macd_signal"]) + 2):
            return None
        highs = [c["high"] for c in c1]
        lows = [c["low"] for c in c1]
        ema_fast_s = _ema(closes, int(p["ema_fast"]))
        ema_slow_s = _ema(closes, int(p["ema_slow"]))
        macd_line, signal_line, hist = mcs.macd(
            closes, int(p["macd_fast"]), int(p["macd_slow"]), int(p["macd_signal"]))
        rsi_s = mcs.rsi(closes, int(p["rsi_len"]))
        k_s, d_s = mcs.stochastic(highs, lows, closes, int(p["stoch_len"]),
                                  int(p["stoch_k_smooth"]), int(p["stoch_d_smooth"]))

        f_now, s_now = ema_fast_s[i], ema_slow_s[i]
        f_prev, s_prev = ema_fast_s[i - 1], ema_slow_s[i - 1]
        cross_up = f_now > s_now and f_prev <= s_prev
        cross_dn = f_now < s_now and f_prev >= s_prev
        ema_bull_bias = f_now > s_now
        ema_bear_bias = f_now < s_now

        lookback = int(p["cross_lookback"])
        recent_bull_cross = any(
            ema_fast_s[j] is not None and ema_slow_s[j] is not None and
            ema_fast_s[j - 1] is not None and ema_slow_s[j - 1] is not None and
            ema_fast_s[j] > ema_slow_s[j] and ema_fast_s[j - 1] <= ema_slow_s[j - 1]
            for j in range(max(1, i - lookback), i + 1))
        recent_bear_cross = any(
            ema_fast_s[j] is not None and ema_slow_s[j] is not None and
            ema_fast_s[j - 1] is not None and ema_slow_s[j - 1] is not None and
            ema_fast_s[j] < ema_slow_s[j] and ema_fast_s[j - 1] >= ema_slow_s[j - 1]
            for j in range(max(1, i - lookback), i + 1))

        macd_ok = macd_line[i] is not None and signal_line[i] is not None and \
                 macd_line[i - 1] is not None and signal_line[i - 1] is not None and \
                 hist[i] is not None and hist[i - 1] is not None
        macd_bear = macd_ok and (
            (macd_line[i] < signal_line[i] and macd_line[i - 1] >= signal_line[i - 1])
            or hist[i] < hist[i - 1])
        macd_bull = macd_ok and (
            (macd_line[i] > signal_line[i] and macd_line[i - 1] <= signal_line[i - 1])
            or hist[i] > hist[i - 1])

        rsi_bear = rsi_s[i] is not None and rsi_s[i] < p["rsi_bear_thresh"]
        rsi_bull = rsi_s[i] is not None and rsi_s[i] > p["rsi_bull_thresh"]

        stoch_ok = k_s[i] is not None and d_s[i] is not None and \
                  k_s[i - 1] is not None and d_s[i - 1] is not None
        stoch_bear = stoch_ok and k_s[i] < d_s[i] and k_s[i - 1] >= d_s[i - 1] and \
                    k_s[i - 1] >= p["stoch_ob"]
        stoch_bull = stoch_ok and k_s[i] > d_s[i] and k_s[i - 1] <= d_s[i - 1] and \
                    k_s[i - 1] <= p["stoch_os"]

        bear_score = sum([macd_bear, rsi_bear, stoch_bear, ema_bear_bias])
        bull_score = sum([macd_bull, rsi_bull, stoch_bull, ema_bull_bias])
        min_conf = int(p["min_confluence"])
        bear_confluence = bear_score >= min_conf
        bull_confluence = bull_score >= min_conf

        pivot_left, pivot_right = int(p["pivot_left"]), int(p["pivot_right"])
        bullish_div, bearish_div = _rsi_divergence(
            closes, highs, lows, rsi_s, pivot_left, pivot_right, i)

        eq_tol = p["eq_tol_pct"] / 100
        last_hi, last_lo = _last_two_price_pivots(highs, lows, pivot_left, pivot_right, i)
        equal_highs = (last_hi[0] is not None and last_hi[1] is not None and
                      abs(last_hi[0] - last_hi[1]) <= last_hi[0] * eq_tol)
        equal_lows = (last_lo[0] is not None and last_lo[1] is not None and
                     abs(last_lo[0] - last_lo[1]) <= last_lo[0] * eq_tol)
        price_cross_up = closes[i] > f_now and closes[i - 1] <= ema_fast_s[i - 1]
        price_cross_dn = closes[i] < f_now and closes[i - 1] >= ema_fast_s[i - 1]
        weapon_bull = equal_lows and price_cross_up
        weapon_bear = equal_highs and price_cross_dn

        confluence_long = bullish_div and recent_bull_cross and bull_confluence
        confluence_short = bearish_div and recent_bear_cross and bear_confluence
        long_entry = confluence_long or weapon_bull
        short_entry = confluence_short or weapon_bear
        if not (long_entry or short_entry):
            return None
        # Prefer the confluence signal's label if BOTH fired on the same
        # bar (rare, but the Pine Script's own `or` entry condition
        # doesn't distinguish which fired either — matches its behavior).
        d = +1 if long_entry else -1
        why_parts = []
        if d > 0:
            why_parts.append("confluence reversal" if confluence_long else "weapon pattern")
        else:
            why_parts.append("confluence reversal" if confluence_short else "weapon pattern")
        # 2026-07-27 — the ONE deliberate simplification versus the
        # original Pine Script: the Pine strategy's real exit is a
        # fixed stop at the entry candle's own low/high (mapped
        # EXACTLY below — a genuine, faithful match, not an
        # approximation) PLUS an early exit the moment the MACD
        # histogram's slope reverses — a condition evaluated on every
        # SUBSEQUENT candle, not a fixed price level fixed at entry
        # time. Every other PA strategy in this module expresses its
        # exit as fixed stop_spot/t1_spot/t2_spot levels computed once
        # at signal time (the framework ExecutionAgent's monitoring
        # loop already checks against) — there is no existing
        # mechanism for "keep evaluating an index-level condition on
        # every future candle for an open position." Faithfully
        # porting the histogram-turn exit would need that new
        # mechanism built as a dedicated follow-up; until then this
        # uses a fixed risk-reward target (rr_target, tunable) as an
        # explicit, documented stand-in — not a silent gap.
        risk = abs(spot - (lows[i] if d > 0 else highs[i]))
        risk = max(risk, spot * 0.0008)   # guard against a degenerate same-bar stop
        stop = lows[i] if d > 0 else highs[i]
        rr = p["rr_target"]
        return {"dir": d, "entry_spot": spot, "stop_spot": stop,
                "t1_spot": spot + d * risk * rr, "t2_spot": spot + d * risk * rr * 1.5,
                "why": why_parts[0] + f" ({'long' if d > 0 else 'short'})"}
    return None


def pullback_confirmations(c1, c5, c15, direction, p):
    """Deck setups 4, 5 and 6 as CONFIRMATIONS on an existing pullback.

    v58.48 — the source deck's remaining High Probability Setups:

        4  Trading for Wave 3   — enter at the end of Wave 2
        5  Trading for Wave 5   — enter at the end of Wave 4
        6  Trading the main trend at the end of Wave C
        7  Trading the correction after Wave 5 (COUNTER-trend)

    Setups 4, 5 and 6 are the same trade in three costumes: "a
    correction is ending, resume with the trend". That is precisely
    what Anchor Pullback already does. They do not need three new
    strategies — what they add is a way to judge WHICH pullback is
    worth taking, which is the one thing S3 currently has no opinion
    about (it takes every touch of the anchor).

    Setup 7 is deliberately NOT here. It is a counter-trend fade after
    a five-wave advance completes, so it belongs with the reversal
    detectors in Strategy 8. Forcing it in would have made S3 mean two
    opposite things at once.

    Every confirmation is INDEPENDENT and OFF by default. S3 has live
    trade history, and the lesson from v58.39 was that silently
    changing a strategy's behaviour destroys the ability to compare
    before and after. With `min_confirmations` at 0 this function is
    never even called.

    Indicator primitives are reused from ta_elliott rather than
    reimplemented, so the Tide, Bollinger phase and divergence
    definitions cannot drift between Strategy 3 and Strategy 9.

    Returns (met_count, detail_dict).
    """
    import ta_elliott as _te
    detail = {}
    met = 0
    tp = {"bb_period": 20, "bb_stdev": 2.0,
          "bb_slope_eps": p.get("bb_slope_eps", 0.00015),
          "bb_min_width_pct": 0.15,
          "tide_fast": 5, "tide_slow": 13, "tide_use_15m": 0,
          "gmma_compression_pct": 25.0, "gmma_lookback": 60,
          "gmma_min_separation_pct": 0.05, "gmma_timeframe": "1m",
          "zigzag_deviation_pct": p.get("zigzag_deviation_pct", 0.5)}

    # Setup 4 — "MACD Histogram of the TIDE should favor the trade"
    if p.get("require_tide"):
        tide = _te.tide_of(c15, tp, c5=c5)
        if tide is None:
            detail["tide"] = "skipped (not computable yet)"
            met += 1          # missing data must not block — standing rule
        elif tide == direction:
            detail["tide"] = "favours the trade"
            met += 1
        else:
            detail["tide"] = "AGAINST the trade"

    # Setup 5 — "End of Wave 4 with MACD doing Zero Line reversal"
    if p.get("require_macd_zero_reversal"):
        closes = [c["close"] for c in c1]
        line, _s, _h = _te._macd(closes, 12, 26, 9)
        rev = 0
        for k in range(max(1, len(line) - 5), len(line)):
            if line[k - 1] <= 0 < line[k]:
                rev = +1
            elif line[k - 1] >= 0 > line[k]:
                rev = -1
        ok = rev == direction
        detail["macd_zero_reversal"] = ("crossed zero with the trade" if ok
                                        else "no reversal in this direction")
        met += 1 if ok else 0

    # Setups 4/5 — reverse (hidden) divergence: the correction is ending
    if p.get("require_hidden_divergence"):
        try:
            import structure as _st
            piv = _st.zigzag_series(c1, tp["zigzag_deviation_pct"])
            closes = [c["close"] for c in c1]
            _l, _s, hist = _te._macd(closes, 12, 26, 9)
            tmap = {c["time"]: n for n, c in enumerate(c1)}
            dv = _te.divergences(c1, piv, hist, tmap)
            ok = dv["hidden_bull"] if direction > 0 else dv["hidden_bear"]
            detail["hidden_divergence"] = "present" if ok else "absent"
            met += 1 if ok else 0
        except Exception as e:
            detail["hidden_divergence"] = f"skipped ({type(e).__name__})"
            met += 1

    # Setup 4 — "Bollinger Band should confirm previous correction"
    if p.get("require_bb_confirm"):
        bb = _te.bollinger_state(c5 or c1, tp)
        st = bb.get("state")
        if st is None:
            detail["bollinger"] = "skipped (not computable yet)"
            met += 1
        elif st in ("CORRECTIVE_STALL", "CORRECTIVE_FLAT"):
            detail["bollinger"] = f"{st} — a correction was in progress"
            met += 1
        else:
            detail["bollinger"] = f"{st} — no corrective phase to resume from"
    return met, detail


def macd_hist_turn_exit(c1, direction, params=None):
    """The Pine original's early exit: leave when the MACD histogram's
    slope reverses against the position.

    v58.47, closing the ONE documented simplification in this module's
    port. The note in evaluate() was accurate about why it was
    deferred: every other PA strategy expresses its exit as fixed
    stop/target PRICES computed once at signal time, and there was no
    mechanism for "keep evaluating an index-level condition on every
    future candle for an open position". That mechanism now exists
    (agents.dynamic_exit_reason), and this is the first condition
    plugged into it.

    Mirror of the entry test in evaluate(): a long entered partly
    because the histogram was RISING exits when it starts falling, and
    vice versa. `hist_turn_confirm_bars` requires the reversal to
    persist rather than firing on a single noisy bar — the entry logic
    reads one bar because it is confirming a setup that already
    exists, whereas an exit that trips on one bar would surrender
    every position to ordinary chop.

    Returns a reason string or None. Never raises: an exit path that
    can throw is worse than one that occasionally misses.
    """
    p = dict(PA_DEFAULTS["momentum_confluence"], **(params or {}))
    n = int(p.get("hist_turn_confirm_bars", 2))
    # Imported HERE, matching evaluate()'s own local import. First
    # version of this function referenced a module-level `mcs` that
    # does not exist — a NameError that the broad `except` below then
    # swallowed, so the exit silently never fired and the function
    # looked like it worked. The except stays (an exit path that can
    # throw is worse than one that occasionally misses) but the import
    # is now correct, and a test asserts the condition actually FIRES
    # rather than merely not crashing.
    import mtf_confluence_strategy as mcs
    try:
        closes = [c["close"] for c in c1]
        need = int(p["macd_slow"]) + int(p["macd_signal"]) + n + 2
        if len(closes) < need:
            return None
        _, _, hist = mcs.macd(closes, int(p["macd_fast"]), int(p["macd_slow"]),
                              int(p["macd_signal"]))
        tail = [h for h in hist[-(n + 1):] if h is not None]
        if len(tail) < n + 1:
            return None
        # Every one of the last n steps must go against the position.
        if direction > 0:
            turned = all(tail[i] < tail[i - 1] for i in range(1, len(tail)))
        else:
            turned = all(tail[i] > tail[i - 1] for i in range(1, len(tail)))
        if not turned:
            return None
        return (f"MACD histogram turned against the position "
                f"({n} bar(s): {tail[0]:+.2f} -> {tail[-1]:+.2f})")
    except Exception:
        return None


def structure_ok(direction, pivots):
    """Strategy 7's ZigZag structure gate, exactly per the v51 spec.
    direction: "long"/"short". Returns True/False, or None when there is
    no confirmed pivot yet — the caller must treat None as SKIP THE GATE
    (§4.5 graceful degradation: missing data must not silently reject
    every signal — the bug pattern already fixed twice in this project)."""
    confirmed = [p for p in (pivots or []) if p.get("structure")]
    if not confirmed:
        return None
    last = confirmed[-1]["structure"]
    return last in (("HH", "HL") if direction == "long" else ("LH", "LL"))


def evaluate_sg_ema(c1, c5, c15, params=None, taken_today=0,
                    pivots=None, ai_bias=None):
    """Strategy 7 (v51): ema_mtf's cross + MTF confirm, PLUS the ZigZag
    structure gate and the AI-bias gate, PLUS a STRUCTURAL stop at the
    last confirmed pivot instead of ema_mtf's EMA-separation stop.

    `pivots`: structure.zigzag_series() output for today's 1m candles —
    the IDENTICAL series the chart draws (parity requirement).
    `ai_bias`: bias:{sym} bus payload from RegimeAgent's Feature #2
    engine, or None when not computed yet.

    Returns (setup_or_None, gates) where gates is the per-gate breakdown
    the Shadow Journal and the eligibility card both need:
      {"cross": ..., "mtf": ..., "structure": ..., "ai_bias": ...}
    each True (passed) / False (blocked) / "skipped (<why>)".
    """
    p = dict(PA_DEFAULTS["sg_ema"], **(params or {}))
    # Initial labels must distinguish "gate is OFF" from "gate is on but
    # never reached because no cross fired" — the eligibility card shows
    # these strings all day whenever there is no cross, and "gate off"
    # would be plainly wrong while the gate is enabled.
    gates = {"cross": False,
             "mtf": ("not evaluated (no cross)" if p.get("mtf_confirm")
                     else "skipped (mtf_confirm off)"),
             "structure": ("not evaluated (no cross)"
                           if p.get("require_structure")
                           else "skipped (gate off)"),
             "ai_bias": ("not evaluated (no cross)"
                         if p.get("require_ai_bias")
                         else "skipped (gate off)")}
    # Reuse ema_mtf wholesale for cross + MTF (spec: "reuse Strategy 4's
    # existing check, do not reimplement") — run it with the SAME p so
    # fast/slow/mtf_confirm apply identically.
    base = evaluate("ema_mtf", c1, c5, c15,
                    params={k: p[k] for k in ("fast", "slow", "mtf_confirm",
                                              "max_trades_per_day")},
                    taken_today=taken_today)
    if not base:
        return None, gates
    gates["cross"] = True
    if p["mtf_confirm"]:
        gates["mtf"] = True     # ema_mtf returned -> MTF already passed
    d = base["dir"]
    direction = "long" if d > 0 else "short"

    if p.get("require_structure"):
        st = structure_ok(direction, pivots)
        if st is None:
            gates["structure"] = "skipped (no confirmed pivots yet)"
        elif not st:
            gates["structure"] = False
            return None, gates
        else:
            gates["structure"] = True

    if p.get("require_ai_bias"):
        if not ai_bias or ai_bias.get("score") is None:
            gates["ai_bias"] = "skipped (bias not computed yet)"
        else:
            score = ai_bias.get("score", 0)
            label = str(ai_bias.get("label", ai_bias.get("bias", ""))).lower()
            agree = (("bull" in label and d > 0) or
                     ("bear" in label and d < 0))
            strong = abs(score) >= p.get("min_ai_bias", 20)
            if agree and strong:
                gates["ai_bias"] = True
            elif "neutral" in label or not label:
                gates["ai_bias"] = "skipped (bias neutral)"
            else:
                gates["ai_bias"] = False
                return None, gates

    # STRUCTURAL stop: last confirmed pivot on the SAME side, buffered.
    spot = base["entry_spot"]
    stop_spot = base["stop_spot"]          # fallback: ema_mtf's own stop
    confirmed = [pv for pv in (pivots or []) if pv.get("structure")]
    side_pivots = [pv for pv in confirmed
                   if (pv["type"] == "low" if d > 0 else pv["type"] == "high")]
    if side_pivots:
        pivot_px = side_pivots[-1]["price"]
        buf = spot * p.get("structural_stop_buffer_pct", 0.05) / 100
        cand = pivot_px - buf if d > 0 else pivot_px + buf
        # only a pivot on the CORRECT side of price is a valid stop
        if (spot - cand) * d > 0:
            stop_spot = round(cand, 2)
    risk = abs(spot - stop_spot)
    rr = p.get("rr_target", 2.0)
    return {"dir": d, "entry_spot": spot, "stop_spot": stop_spot,
            "t1_spot": round(spot + d * risk * rr, 2),
            "t2_spot": round(spot + d * risk * rr * 1.33, 2),
            "structural_stop": stop_spot,
            "why": base["why"] + " + structure gate + AI-bias gate "
                   f"(stop @ {'pivot' if side_pivots else 'EMA-sep fallback'})"}, gates


def tune(name, params, direction):
    """One bounded relax (+1) or tighten (-1) step. Returns (new, changes)."""
    p = dict(params)
    changes = []
    for key, (lo, hi, relax_dir) in PA_BOUNDS.get(name, {}).items():
        step_dir = relax_dir * direction
        cur = p.get(key, PA_DEFAULTS[name][key])
        # v58.28 — ew_reversal's two binary gates added here. Without
        # this they fall to the generic numeric branch below, which for
        # (0, 1) bounds produces 0.75 / 0.5 / 0.25 steps: truthy
        # fractions that leave a gate nominally "on" while displaying a
        # meaningless value. Deliberately NOT extended to sg_ema's own
        # binary keys in the same release — that would change Strategy
        # 7's tuning behaviour, which is out of scope here.
        # Every 0/1 gate must FLIP, not step. A (0,1) bound stepped by
        # 0.25 yields 0.75 / 0.5 / 0.25 — truthy fractions that leave a
        # gate nominally "on" while displaying a meaningless value.
        # v58.48 adds S3's four confirmation gates to the list.
        if key in ("anchor_filter", "mtf_confirm",
                   "require_macd_divergence", "require_tide",
                   "require_tide_all_detectors",
                   "require_macd_zero_reversal", "require_hidden_divergence",
                   "require_bb_confirm"):
            new = (0 if step_dir < 0 else 1)
        else:
            step = (hi - lo) * 0.25 * step_dir
            new = round(min(hi, max(lo, cur + step)), 3)
        if new != cur:
            p[key] = new
            changes.append(f"{key} {cur}->{new}")
    return p, changes
