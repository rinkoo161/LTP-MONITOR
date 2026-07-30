"""technical_engine.py — Feature #7 (Technical Analysis Engine).

Per the spec's own explicit framing: this is a CONFIRMATION engine,
not a standalone signal generator — it never produces its own Buy/
Sell call. Every function here only strengthens or weakens confidence
in the bias Option Chain (Feature #4) and Institutional Activity
(Feature #5) engines have already determined.

Reuses, does NOT recompute: ema()/macd()/rsi()/stochastic()/
bollinger_percent_b()/atr() from mtf_confluence_strategy.py, and
supertrend()/ichimoku_bias() from market_bias.py — every one of these
indicator calculations already exists and is already used live
elsewhere in this codebase (the MTF Confluence strategy, Feature #2's
bias engine). This module is purely an INTERPRETATION layer on top of
them (cross detection, alignment, zone classification, scoring), per
the spec's own "avoid duplicate indicator calculations" instruction.

Built incrementally, one indicator engine at a time, per the spec's
own explicit pacing instruction — stopping for review after each
engine rather than batching them (a stricter cadence than Features
#4/#5 used, matching this spec's more explicit wording: "Complete one
indicator engine at a time. Wait for review before proceeding.").
"""


import mtf_confluence_strategy as mcs

EMA_PERIODS = (9, 20, 50, 200)


import market_bias as mb


def ichimoku_engine(candles, tenkan_n=9, kijun_n=26, senkou_b_n=52, shift=26):
    """Ichimoku Cloud Engine (increment 8) — the spec's own most
    detailed section, and explicitly told to "carry one of the highest
    weights in technical confirmation". `market_bias.ichimoku_bias()`
    computes a SIMPLIFIED, non-shifted read (documented there as "not
    the full plotted system with forward/backward time shifts") —
    reused for Feature #2's own bias scoring, which only needed a
    directional read. This increment needs the REAL system: Senkou
    Span A/B are calculated from data as-of a point in time, but
    PLOTTED `shift` (26) bars forward on a real chart — so "today's
    cloud" on a live chart was actually calculated `shift` bars ago,
    and "the future cloud" (what will appear `shift` bars from now) is
    exactly today's freshly-computed Senkou A/B. That distinction is
    the genuinely new piece here; Tenkan/Kijun/Senkou math itself uses
    the same high-low-midpoint formula `market_bias.ichimoku_bias()`
    already established, just applied at multiple points in time
    instead of one.

    Needs senkou_b_n + shift + 2 candles minimum (peak history
    requirement of any indicator in this engine so far)."""
    empty = {"tenkan": None, "kijun": None, "senkou_a_current": None,
            "senkou_b_current": None, "senkou_a_future": None,
            "senkou_b_future": None, "chikou_confirmation": None,
            "price_position": None, "cloud_thickness_pct": None,
            "future_cloud_direction": None, "tenkan_kijun_cross": None,
            "bullish_kumo_breakout": False, "bearish_kumo_breakdown": False,
            "cloud_twist": None, "bias": "Neutral", "confidence": 0,
            "unavailable": True}
    min_needed = senkou_b_n + shift + 2
    if not candles or len(candles) < min_needed:
        return empty

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    last = len(candles) - 1

    def hl_mid(n, end_idx):
        window_h = highs[end_idx - n + 1:end_idx + 1]
        window_l = lows[end_idx - n + 1:end_idx + 1]
        return (max(window_h) + min(window_l)) / 2

    close = closes[last]
    tenkan = hl_mid(tenkan_n, last)
    kijun = hl_mid(kijun_n, last)

    # "Current cloud" — what's actually plotted at NOW on a real chart
    # — was calculated `shift` bars ago.
    idx_then = last - shift
    tenkan_then = hl_mid(tenkan_n, idx_then)
    kijun_then = hl_mid(kijun_n, idx_then)
    senkou_a_current = (tenkan_then + kijun_then) / 2
    senkou_b_current = hl_mid(senkou_b_n, idx_then)
    cloud_top_current = max(senkou_a_current, senkou_b_current)
    cloud_bottom_current = min(senkou_a_current, senkou_b_current)

    # "Future cloud" — today's fresh Senkou A/B, which will be the
    # cloud plotted `shift` bars from now.
    senkou_a_future = (tenkan + kijun) / 2
    senkou_b_future = hl_mid(senkou_b_n, last)
    future_cloud_direction = ("bullish" if senkou_a_future > senkou_b_future
                              else "bearish" if senkou_a_future < senkou_b_future
                              else "neutral")

    # Chikou span: current close vs price from `shift` bars ago.
    price_then = closes[last - shift]
    chikou_confirmation = ("bullish" if close > price_then else
                           "bearish" if close < price_then else "neutral")

    price_position = ("above" if close > cloud_top_current else
                      "below" if close < cloud_bottom_current else "inside")
    cloud_thickness_pct = (round(abs(cloud_top_current - cloud_bottom_current) /
                                close * 100, 3) if close else None)

    # One bar back, for cross/breakout/twist detection.
    prev_idx = last - 1
    tenkan_prev = hl_mid(tenkan_n, prev_idx)
    kijun_prev = hl_mid(kijun_n, prev_idx)
    close_prev = closes[prev_idx]

    tenkan_kijun_cross = None
    if tenkan_prev <= kijun_prev and tenkan > kijun:
        tenkan_kijun_cross = "bullish"
    elif tenkan_prev >= kijun_prev and tenkan < kijun:
        tenkan_kijun_cross = "bearish"

    bullish_kumo_breakout = close_prev <= cloud_top_current < close
    bearish_kumo_breakdown = close_prev >= cloud_bottom_current > close

    # Cloud twist: the FUTURE cloud's own A/B relationship flipping
    # between one bar ago and now (a leading signal — the cloud itself
    # is about to change color ahead of price).
    senkou_a_future_prev = (tenkan_prev + kijun_prev) / 2
    senkou_b_future_prev = hl_mid(senkou_b_n, prev_idx)
    cloud_twist = None
    if senkou_a_future_prev <= senkou_b_future_prev and senkou_a_future > senkou_b_future:
        cloud_twist = "bullish"
    elif senkou_a_future_prev >= senkou_b_future_prev and senkou_a_future < senkou_b_future:
        cloud_twist = "bearish"

    signals = []
    signals.append(1 if price_position == "above" else -1 if price_position == "below" else 0)
    signals.append(1 if tenkan > kijun else -1 if tenkan < kijun else 0)
    signals.append(1 if chikou_confirmation == "bullish" else -1 if chikou_confirmation == "bearish" else 0)
    signals.append(1 if future_cloud_direction == "bullish" else -1 if future_cloud_direction == "bearish" else 0)
    score = sum(signals)

    if price_position == "above" and tenkan > kijun and chikou_confirmation == "bullish":
        bias = "Strong Bullish"
    elif price_position == "below" and tenkan < kijun and chikou_confirmation == "bearish":
        bias = "Strong Bearish"
    elif score > 0:
        bias = "Bullish"
    elif score < 0:
        bias = "Bearish"
    else:
        bias = "Neutral"
    confidence = round(abs(score) / len(signals) * 100)

    return {"tenkan": round(tenkan, 2), "kijun": round(kijun, 2),
           "senkou_a_current": round(senkou_a_current, 2),
           "senkou_b_current": round(senkou_b_current, 2),
           "senkou_a_future": round(senkou_a_future, 2),
           "senkou_b_future": round(senkou_b_future, 2),
           "chikou_confirmation": chikou_confirmation,
           "price_position": price_position,
           "cloud_thickness_pct": cloud_thickness_pct,
           "future_cloud_direction": future_cloud_direction,
           "tenkan_kijun_cross": tenkan_kijun_cross,
           "bullish_kumo_breakout": bullish_kumo_breakout,
           "bearish_kumo_breakdown": bearish_kumo_breakdown,
           "cloud_twist": cloud_twist, "bias": bias,
           "confidence": confidence, "unavailable": False}


def supertrend_engine(candles, period=10, multiplier=3.0, change_lookback=5):
    """Supertrend Engine (increment 7) — Trend Direction, Trend
    Change, Buy/Sell State, Support/Resistance Level. Reuses `market_
    bias.supertrend()` directly — that function already computes BOTH
    the direction series (+1/-1) AND the actual trend line value per
    candle, but Feature #2's own bias engine only ever used the
    direction, discarding the trend line. This increment is exactly
    that missing piece: exposing the trend line itself as a dynamic
    support (when bullish, price sits above the line) or resistance
    (when bearish, price sits below it) level — no new Supertrend
    calculation, just surfacing what already existed."""
    empty = {"direction": None, "trend_change": False, "state": None,
            "level": None, "level_type": None, "bias": "Neutral",
            "unavailable": True}
    if not candles or len(candles) <= period:
        return empty

    trend, direction = mb.supertrend(candles, period, multiplier)
    if direction[-1] is None:
        return empty
    current_dir = direction[-1]
    current_level = trend[-1]

    # Trend change: did direction flip anywhere within the recent
    # lookback window (not just the last single candle, so a change
    # that happened a couple of bars ago is still surfaced as "recent"
    # rather than only the instant it happens).
    recent_dirs = [d for d in direction[-change_lookback:] if d is not None]
    trend_change = len(recent_dirs) >= 2 and recent_dirs[0] != recent_dirs[-1]

    state = "Buy State" if current_dir == 1 else "Sell State"
    level_type = "support" if current_dir == 1 else "resistance"
    bias = "Bullish" if current_dir == 1 else "Bearish"

    return {"direction": "bullish" if current_dir == 1 else "bearish",
           "trend_change": trend_change, "state": state,
           "level": round(current_level, 2) if current_level is not None else None,
           "level_type": level_type, "bias": bias, "unavailable": False}


def atr_engine(candles, period=14, lookback=10,
              expansion_ratio=1.15, contraction_ratio=0.85):
    """ATR Engine (increment 6) — ATR, Expected Intraday Range,
    Volatility Expansion/Contraction, and a suggested stop-loss
    distance. Reuses `mtf_confluence_strategy.atr()` directly (called
    twice, on the full candle list and on a shortened slice, to get a
    "then vs now" comparison — `atr()` itself only ever returns a
    single latest value, not a series, so this is the same technique
    already used elsewhere in this module for before/after comparisons
    rather than a new ATR calculation).

    The suggested stop distance reuses the EXACT SAME `atr_stop_
    multiplier` config value `sizing.py`'s live ATR-based stop-loss
    mode already uses (default 2.5) — this engine surfaces the number
    for confirmation purposes, it does not implement its own separate
    stop-loss logic or a different multiplier.

    ATR itself is a volatility measure, not a directional one — this
    engine's `bias` is always Neutral; expansion/contraction inform
    CONVICTION (how much a move can be trusted / how wide a stop needs
    to be), not which way the market is leaning, matching the spec's
    own framing ("use ATR for stop-loss recommendations", not for a
    Buy/Sell call)."""
    empty = {"atr": None, "atr_pct": None, "expected_range": None,
            "volatility_expansion": False, "volatility_contraction": False,
            "suggested_stop_distance": None, "bias": "Neutral",
            "unavailable": True}
    if not candles or len(candles) < period + 2:
        return empty

    current_atr = mcs.atr(candles, period)
    if current_atr is None:
        return empty
    spot = candles[-1].get("close")
    atr_pct = round(current_atr / spot * 100, 3) if spot else None

    past_atr = None
    if len(candles) >= period + 2 + lookback:
        past_atr = mcs.atr(candles[:-lookback], period)

    volatility_expansion = volatility_contraction = False
    if past_atr:
        if current_atr > past_atr * expansion_ratio:
            volatility_expansion = True
        elif current_atr < past_atr * contraction_ratio:
            volatility_contraction = True

    import config as _cfg
    multiplier = _cfg.load().get("atr_stop_multiplier", 2.5)
    suggested_stop_distance = round(current_atr * multiplier, 2)

    return {"atr": round(current_atr, 2), "atr_pct": atr_pct,
           "expected_range": round(current_atr, 2),
           "volatility_expansion": volatility_expansion,
           "volatility_contraction": volatility_contraction,
           "suggested_stop_distance": suggested_stop_distance,
           "bias": "Neutral", "unavailable": False}


def adx_engine(candles, period=14, strong_threshold=25, weak_threshold=15):
    """ADX Engine (increment 5) — ADX, +DI, -DI, Strong Trend/Weak
    Trend/Sideways Market. Reuses `mtf_confluence_strategy.adx_di()`
    directly — this is the EXACT SAME calculation RegimeAgent's own
    regime classification already uses (extracted from there, not a
    second copy), so this engine's ADX reading is always consistent
    with what the regime engine sees. `strong_threshold`/25 matches
    RegimeAgent's own trending-regime cutoff, for consistency between
    the two engines rather than two different opinions on what counts
    as "trending"."""
    empty = {"adx": None, "plus_di": None, "minus_di": None,
            "state": None, "bias": "Neutral", "unavailable": True}
    if not candles or len(candles) < period + 2:
        return empty

    adx, pdi, mdi = mcs.adx_di(candles, period)
    if adx == 0 and pdi == 0 and mdi == 0:
        return empty

    if adx >= strong_threshold:
        state = "Strong Trend"
    elif adx >= weak_threshold:
        state = "Weak Trend"
    else:
        state = "Sideways Market"

    if state == "Sideways Market":
        bias = "Neutral"
    elif pdi > mdi:
        bias = "Bullish"
    elif mdi > pdi:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"adx": round(adx, 2), "plus_di": round(pdi, 2),
           "minus_di": round(mdi, 2), "state": state, "bias": bias,
           "unavailable": False}


def rsi_engine(candles, overbought=70, oversold=30, divergence_lookback=30):
    """RSI Engine (increment 4) — Current RSI, RSI Trend, RSI Momentum,
    Overbought/Oversold, Bullish/Bearish Divergence. Reuses `mtf_
    confluence_strategy.rsi()` directly for the RSI values themselves
    — the genuinely NEW piece here is divergence detection (comparing
    price extremes against RSI extremes over a lookback window), which
    doesn't exist anywhere else in this codebase yet.

    Per the spec's own explicit instruction ("do not generate trades
    solely on RSI"), this engine's own `bias` output is still just ONE
    confirmation input for a higher-level aggregator to weigh alongside
    everything else — divergence is treated as the stronger signal,
    plain overbought/oversold as a weaker directional lean, matching
    how RSI is conventionally read (extremes alone are a caution zone,
    not a trade call; divergence is the sharper signal)."""
    empty = {"rsi": None, "trend": None, "momentum": None,
            "overbought": False, "oversold": False,
            "bullish_divergence": False, "bearish_divergence": False,
            "bias": "Neutral", "unavailable": True}
    if not candles:
        return empty
    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < 20:
        return empty

    rsi_series = mcs.rsi(closes)
    if rsi_series[-1] is None:
        return empty
    current = rsi_series[-1]
    prev = rsi_series[-2] if len(rsi_series) > 1 else None

    trend = None
    if len(rsi_series) >= 6 and rsi_series[-6] is not None:
        past = rsi_series[-6]
        trend = ("rising" if current > past + 1 else
                 "falling" if current < past - 1 else "flat")
    momentum = round(current - prev, 2) if prev is not None else None
    overbought = current >= overbought
    oversold = current <= oversold

    # Divergence: split the lookback window into two halves and compare
    # the price/RSI pairing at each half's extreme — a lower price low
    # paired with a HIGHER RSI low is bullish divergence (momentum
    # improving even as price falls); the mirror for highs is bearish.
    bullish_divergence = bearish_divergence = False
    n = min(divergence_lookback, len(closes), len(rsi_series))
    if n >= 10:
        window_closes = closes[-n:]
        window_rsi = rsi_series[-n:]
        half = n // 2
        def _extreme(cs, rs, pick):
            pairs = [(c, r) for c, r in zip(cs, rs) if r is not None]
            return pick(pairs, key=lambda x: x[0]) if pairs else None
        low1 = _extreme(window_closes[:half], window_rsi[:half], min)
        low2 = _extreme(window_closes[half:], window_rsi[half:], min)
        high1 = _extreme(window_closes[:half], window_rsi[:half], max)
        high2 = _extreme(window_closes[half:], window_rsi[half:], max)
        if low1 and low2 and low2[0] < low1[0] and low2[1] > low1[1]:
            bullish_divergence = True
        if high1 and high2 and high2[0] > high1[0] and high2[1] < high1[1]:
            bearish_divergence = True

    if bullish_divergence:
        bias = "Bullish"
    elif bearish_divergence:
        bias = "Bearish"
    elif oversold:
        bias = "Bullish"
    elif overbought:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"rsi": round(current, 2), "trend": trend, "momentum": momentum,
           "overbought": overbought, "oversold": oversold,
           "bullish_divergence": bullish_divergence,
           "bearish_divergence": bearish_divergence,
           "bias": bias, "unavailable": False}


def macd_engine(candles, lookback_for_strength=50):
    """MACD Engine (increment 3) — Bullish/Bearish Cross, Above/Below
    Zero, Histogram Expansion/Weakening, trend strength. Reuses
    mtf_confluence_strategy.macd() directly (MACD/signal/histogram
    math not recomputed here) — this function only interprets the
    resulting series."""
    empty = {"macd": None, "signal": None, "histogram": None,
            "bullish_cross": False, "bearish_cross": False,
            "above_zero": None, "histogram_expansion": False,
            "histogram_weakening": False, "trend_strength": None,
            "bias": "Neutral", "unavailable": True}
    if not candles:
        return empty
    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < 35:   # slow(26) + signal(9) warmup, roughly
        return empty

    macd_line, signal_line, hist = mcs.macd(closes)
    if macd_line[-1] is None or signal_line[-1] is None or hist[-1] is None:
        return empty

    prev_macd = macd_line[-2] if len(macd_line) > 1 else None
    prev_signal = signal_line[-2] if len(signal_line) > 1 else None
    prev_hist = hist[-2] if len(hist) > 1 else None

    bullish_cross = (prev_macd is not None and prev_signal is not None and
                     prev_macd <= prev_signal and macd_line[-1] > signal_line[-1])
    bearish_cross = (prev_macd is not None and prev_signal is not None and
                     prev_macd >= prev_signal and macd_line[-1] < signal_line[-1])
    above_zero = macd_line[-1] > 0

    same_side = (prev_hist is not None and prev_hist != 0 and
                (hist[-1] > 0) == (prev_hist > 0))
    histogram_expansion = bool(same_side and abs(hist[-1]) > abs(prev_hist))
    histogram_weakening = bool(same_side and abs(hist[-1]) < abs(prev_hist))

    # Trend strength: current |histogram| normalized against its own
    # recent range (histogram scale depends on the underlying's
    # absolute price level — NIFTY vs BANKNIFTY are wildly different
    # scales — so a relative-to-recent-max measure is used instead of
    # an absolute threshold).
    recent_hist = [h for h in hist[-lookback_for_strength:] if h is not None]
    max_abs_hist = max((abs(h) for h in recent_hist), default=0) or 1
    trend_strength = round(min(100, abs(hist[-1]) / max_abs_hist * 100))

    if hist[-1] > 0 and above_zero:
        bias = "Bullish"
    elif hist[-1] < 0 and not above_zero:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"macd": round(macd_line[-1], 4), "signal": round(signal_line[-1], 4),
           "histogram": round(hist[-1], 4), "bullish_cross": bullish_cross,
           "bearish_cross": bearish_cross, "above_zero": above_zero,
           "histogram_expansion": histogram_expansion,
           "histogram_weakening": histogram_weakening,
           "trend_strength": trend_strength, "bias": bias, "unavailable": False}


def ema_engine(candles):
    """EMA Engine (increment 2) — EMA Alignment (9/20/50/200), Golden
    Cross, Death Cross, Price above/below EMA9, Trend Strength. Returns
    Strong Bullish/Bullish/Neutral/Bearish/Strong Bearish.

    Reuses mtf_confluence_strategy.ema() directly — the EMA math
    itself isn't recomputed here, this function only interprets the
    resulting values (alignment, crosses, spread-based strength)."""
    empty = {"alignment": None, "golden_cross": False, "death_cross": False,
            "price_vs_ema9": None, "trend_strength": None, "bias": "Neutral",
            "values": {}, "unavailable": True}
    if not candles:
        return empty
    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < max(EMA_PERIODS) + 2:
        return empty

    emas = {p: mcs.ema(closes, p) for p in EMA_PERIODS}
    latest = {p: emas[p][-1] for p in EMA_PERIODS}
    prev = {p: emas[p][-2] for p in EMA_PERIODS}
    if any(v is None for v in latest.values()):
        return empty

    bullish_alignment = latest[9] > latest[20] > latest[50] > latest[200]
    bearish_alignment = latest[9] < latest[20] < latest[50] < latest[200]
    alignment = ("bullish" if bullish_alignment else
                "bearish" if bearish_alignment else "mixed")

    golden_cross = (prev[50] is not None and prev[200] is not None and
                    prev[50] <= prev[200] and latest[50] > latest[200])
    death_cross = (prev[50] is not None and prev[200] is not None and
                   prev[50] >= prev[200] and latest[50] < latest[200])

    price_vs_ema9 = ("above" if closes[-1] > latest[9] else
                     "below" if closes[-1] < latest[9] else "at")

    # Trend strength: how far apart the fastest/slowest EMAs are,
    # relative to price — a wide spread means a well-established,
    # strongly separated trend; a narrow one means the EMAs are
    # bunched together (weak or just-forming trend). Scaling factor
    # (x20) is a documented first-pass calibration, same honesty
    # standard as every other heuristic threshold in this codebase —
    # not backtested against real outcomes yet.
    spread_pct = abs(latest[9] - latest[200]) / latest[200] * 100 if latest[200] else 0
    trend_strength = round(min(100, spread_pct * 20))

    if trend_strength < 5:
        # EMAs are essentially bunched together (near-zero spread) —
        # a technically-true alignment by a razor-thin margin is noise,
        # not a trend. Found via testing: a choppy/flat series still
        # satisfied the strict "bullish/bearish alignment" inequality
        # by a tiny epsilon, which would have wrongly reported a
        # confident directional bias on essentially flat data.
        bias = "Neutral"
    elif bullish_alignment and price_vs_ema9 == "above":
        bias = "Strong Bullish" if trend_strength >= 50 else "Bullish"
    elif bearish_alignment and price_vs_ema9 == "below":
        bias = "Strong Bearish" if trend_strength >= 50 else "Bearish"
    elif bullish_alignment:
        bias = "Bullish"
    elif bearish_alignment:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"alignment": alignment, "golden_cross": golden_cross,
           "death_cross": death_cross, "price_vs_ema9": price_vs_ema9,
           "trend_strength": trend_strength, "bias": bias,
           "values": {f"ema{p}": round(latest[p], 2) for p in EMA_PERIODS},
           "unavailable": False}


def bollinger_engine(candles, period=20, mult=2.0, squeeze_ratio=0.5, expansion_ratio=1.5):
    """Bollinger Band Engine (increment 9) — Upper/Middle/Lower bands,
    Band Width, Squeeze/Expansion, Breakout, Mean Reversion. Reuses
    the IDENTICAL rolling-mean/stdev formula `mtf_confluence_strategy.
    bollinger_percent_b()` already uses (that function only exposes
    %B, not the absolute band values this engine needs) — same
    statistics, not a second independent computation of %B itself."""
    empty = {"upper": None, "middle": None, "lower": None,
            "band_width_pct": None, "squeeze": False, "expansion": False,
            "breakout": None, "mean_reversion": False, "bias": "Neutral",
            "unavailable": True}
    if not candles or len(candles) < period + 10:
        return empty
    closes = [c["close"] for c in candles]

    def bands_at(end_idx):
        window = closes[end_idx - period + 1:end_idx + 1]
        basis = sum(window) / period
        var = sum((c - basis) ** 2 for c in window) / period
        dev = mult * (var ** 0.5)
        return basis + dev, basis, basis - dev

    last = len(closes) - 1
    upper, middle, lower = bands_at(last)
    close = closes[last]
    band_width_pct = round((upper - lower) / middle * 100, 3) if middle else None

    recent_widths = []
    for i in range(max(period - 1, last - 20), last + 1):
        u, m, l = bands_at(i)
        if m:
            recent_widths.append((u - l) / m * 100)
    avg_width = sum(recent_widths) / len(recent_widths) if recent_widths else band_width_pct
    squeeze = bool(avg_width and band_width_pct is not None and
                  band_width_pct < avg_width * squeeze_ratio)
    expansion = bool(avg_width and band_width_pct is not None and
                     band_width_pct > avg_width * expansion_ratio)

    breakout = "upper" if close > upper else "lower" if close < lower else None
    # Mean reversion: price recently touched/exceeded a band and has
    # since moved back toward the middle band.
    prev_close = closes[last - 1] if last >= 1 else None
    _, prev_middle, _ = bands_at(last - 1) if last >= 1 else (None, None, None)
    mean_reversion = bool(
        prev_close is not None and prev_middle is not None and
        ((prev_close > upper and close < prev_close and close > middle) or
         (prev_close < lower and close > prev_close and close < middle)))

    if breakout == "upper":
        bias = "Bullish"
    elif breakout == "lower":
        bias = "Bearish"
    elif mean_reversion and prev_close > prev_middle:
        bias = "Bearish"   # reverting down from an upper extreme
    elif mean_reversion:
        bias = "Bullish"   # reverting up from a lower extreme
    else:
        bias = "Neutral"

    return {"upper": round(upper, 2), "middle": round(middle, 2),
           "lower": round(lower, 2), "band_width_pct": band_width_pct,
           "squeeze": squeeze, "expansion": expansion, "breakout": breakout,
           "mean_reversion": mean_reversion, "bias": bias,
           "unavailable": False}


def stoch_rsi_engine(candles, rsi_period=14, stoch_period=14, k_smooth=3, d_period=3,
                     overbought=80, oversold=20):
    """Stochastic RSI Engine (increment 10) — %K, %D, crossovers,
    momentum, overbought/oversold. Applies the stochastic %K/%D
    formula to the RSI SERIES (not price) — genuinely different from
    `mtf_confluence_strategy.stochastic()`, which operates on price
    high/low/close. Reuses `mtf_confluence_strategy.rsi()` for the
    underlying RSI values; only the stochastic-of-RSI normalization
    step is new. Per the spec's own instruction, used only as
    secondary confirmation — never a standalone signal."""
    empty = {"k": None, "d": None, "cross": None, "overbought": False,
            "oversold": False, "bias": "Neutral", "unavailable": True}
    if not candles or len(candles) < rsi_period + stoch_period + d_period + 2:
        return empty
    closes = [c["close"] for c in candles]
    rsi_series = mcs.rsi(closes, rsi_period)
    valid = [(i, v) for i, v in enumerate(rsi_series) if v is not None]
    if len(valid) < stoch_period + d_period + 2:
        return empty

    k_raw = []
    idxs = [i for i, _ in valid]
    vals = [v for _, v in valid]
    for i in range(stoch_period - 1, len(vals)):
        window = vals[i - stoch_period + 1:i + 1]
        hh, ll = max(window), min(window)
        k_raw.append(50.0 if hh == ll else (vals[i] - ll) / (hh - ll) * 100)
    k_smoothed = mcs._sma_skip_none(k_raw, k_smooth)
    d_smoothed = mcs._sma_skip_none(k_smoothed, d_period)
    if not k_smoothed or k_smoothed[-1] is None or not d_smoothed or d_smoothed[-1] is None:
        return empty

    k, d = k_smoothed[-1], d_smoothed[-1]
    prev_k = k_smoothed[-2] if len(k_smoothed) > 1 else None
    prev_d = d_smoothed[-2] if len(d_smoothed) > 1 else None
    cross = None
    if prev_k is not None and prev_d is not None:
        if prev_k <= prev_d and k > d:
            cross = "bullish"
        elif prev_k >= prev_d and k < d:
            cross = "bearish"
    overbought = k >= overbought
    oversold = k <= oversold

    if cross == "bullish" and oversold:
        bias = "Bullish"
    elif cross == "bearish" and overbought:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"k": round(k, 2), "d": round(d, 2), "cross": cross,
           "overbought": overbought, "oversold": oversold, "bias": bias,
           "unavailable": False}


def momentum_engine(candles, roc_period=10, accel_lookback=5):
    """Momentum Engine (increment 11) — Rate of Change, Momentum,
    Price Acceleration, Momentum Divergence. Genuinely new — no
    existing momentum/ROC calculation elsewhere in this codebase
    (compute_momentum() in agents.py is a simpler %change-over-time
    read for the regime engine, a different purpose)."""
    empty = {"roc_pct": None, "momentum": None, "acceleration": None,
            "divergence": None, "bias": "Neutral", "unavailable": True}
    if not candles or len(candles) < roc_period + accel_lookback + 2:
        return empty
    closes = [c["close"] for c in candles]
    last = len(closes) - 1

    ref = closes[last - roc_period]
    roc_pct = round((closes[last] - ref) / ref * 100, 3) if ref else None
    momentum = round(closes[last] - ref, 2)

    ref_prev = closes[last - accel_lookback - roc_period]
    roc_prev_pct = ((closes[last - accel_lookback] - ref_prev) / ref_prev * 100
                    if ref_prev else None)
    acceleration = (round(roc_pct - roc_prev_pct, 3)
                    if roc_pct is not None and roc_prev_pct is not None else None)

    # Divergence: price makes a new extreme but momentum (ROC) doesn't
    # confirm it — same two-half-window technique as the RSI engine's
    # divergence detection, applied to ROC instead of RSI.
    divergence = None
    n = min(40, len(closes))
    if n >= 20:
        window = closes[-n:]
        half = n // 2
        first_max_i = max(range(half), key=lambda i: window[i])
        # Bug fixed: range(half, n) already yields indices in [half, n),
        # so adding `half` again here previously double-offset it,
        # producing an out-of-range index into `window` (caught
        # immediately by running the test, not shipped).
        second_max_i = max(range(half, n), key=lambda i: window[i])
        if window[second_max_i] > window[first_max_i]:
            # higher price high -- was momentum also higher?
            roc_at_first = (window[first_max_i] - closes[-n - roc_period + first_max_i]
                           if -n - roc_period + first_max_i >= -len(closes) else None)
            if roc_at_first is not None and momentum < roc_at_first:
                divergence = "bearish"

    # Bias reflects the ROC direction primarily — acceleration is
    # informational (exposed as its own field), not a gate. First draft
    # required BOTH positive ROC and non-negative acceleration to call
    # it Bullish, which meant a tiny, noise-level deceleration
    # (e.g. -0.057%) could override a clearly positive +4% ROC and
    # force a Neutral read — caught by testing a plain steady uptrend
    # and getting an unexpected Neutral instead of Bullish.
    if roc_pct is not None and roc_pct > 0:
        bias = "Bullish"
    elif roc_pct is not None and roc_pct < 0:
        bias = "Bearish"
    else:
        bias = "Neutral"

    return {"roc_pct": roc_pct, "momentum": momentum,
           "acceleration": acceleration, "divergence": divergence,
           "bias": bias, "unavailable": False}


def volume_engine(candles, spike_ratio=1.8, lookback=20):
    """Volume Analysis Engine (increment 12) — Volume Spike, Average
    Volume, Relative Volume, Volume Breakout, Volume Confirmation.

    HONEST GAP, same disclosed pattern as Market Breadth elsewhere in
    this codebase: the INDEX itself has no real traded volume (NIFTY
    is a calculated value, not a traded instrument — the same fact
    already documented for AnchorPullback's VWAP-proxy design), and
    Dhan's index intraday candle response (broker_adapter.DhanClient.
    intraday()) doesn't include a volume field at all. This engine
    checks for volume data on whatever candles it's given and reports
    unavailable honestly when none exists, rather than fabricating a
    number — it will report unavailable when fed regime_candles (the
    current wiring) until/unless a futures-volume candle series is
    built and fed to it instead (not done in this pass — flagged as a
    genuine next step, not silently worked around)."""
    empty = {"current_volume": None, "average_volume": None,
            "relative_volume": None, "spike": False, "breakout": False,
            "confirmation": None, "bias": "Neutral", "unavailable": True,
            "reason": "no volume data on the supplied candles (index "
                     "candles carry no volume field — see this "
                     "function's own docstring)"}
    if not candles:
        return empty
    volumes = [c.get("volume") for c in candles if c.get("volume") is not None]
    if len(volumes) < lookback + 2:
        return empty

    current_volume = volumes[-1]
    avg_volume = sum(volumes[-lookback - 1:-1]) / lookback
    relative_volume = round(current_volume / avg_volume, 2) if avg_volume else None
    spike = bool(relative_volume and relative_volume >= spike_ratio)
    breakout = spike
    price_up = candles[-1]["close"] > candles[-2]["close"]
    confirmation = ("bullish" if spike and price_up else
                    "bearish" if spike and not price_up else None)
    bias = ("Bullish" if confirmation == "bullish" else
           "Bearish" if confirmation == "bearish" else "Neutral")

    return {"current_volume": current_volume,
           "average_volume": round(avg_volume, 2),
           "relative_volume": relative_volume, "spike": spike,
           "breakout": breakout, "confirmation": confirmation,
           "bias": bias, "unavailable": False, "reason": None}


def mtf_engine(regime):
    """Multi-Timeframe Analysis Engine (increment 13) — Trend
    Alignment, Trend Conflict, Higher-Timeframe Confirmation. Reuses
    RegimeAgent's OWN existing 1m/5m/15m confluence read (`regime:
    {symbol}`'s `tf_bias`/`confluence` fields, already computed every
    90s) rather than re-fetching additional timeframes — this
    system's confluence check already covers the core of what the
    spec asks for; 3m/30m/60m aren't currently fetched anywhere in
    this codebase and are NOT added here (would mean new API calls
    against the project's own "avoid new API calls, reuse existing
    feeds" convention for this confirmation-only engine) — flagged as
    a disclosed gap rather than silently expanded."""
    empty = {"confluence": None, "alignment": None, "higher_tf_confirms": None,
            "bias": "Neutral", "unavailable": True,
            "reason": "no regime data yet for this symbol"}
    if not regime:
        return empty
    confluence = regime.get("confluence")
    tf_bias = regime.get("tf_bias") or {}
    if confluence is None:
        return empty

    alignment = ("aligned" if confluence in ("strong-bull", "strong-bear") else
                "partial" if confluence in ("mixed-bull", "mixed-bear") else
                "conflict")
    higher_tf = tf_bias.get("15m") or tf_bias.get("tf15")
    higher_tf_confirms = None
    if higher_tf and confluence:
        bull_conf = confluence in ("strong-bull", "mixed-bull")
        higher_tf_confirms = ((higher_tf == "bull" and bull_conf) or
                              (higher_tf == "bear" and not bull_conf and
                               confluence in ("strong-bear", "mixed-bear")))

    bias = ("Bullish" if confluence in ("strong-bull", "mixed-bull") else
           "Bearish" if confluence in ("strong-bear", "mixed-bear") else "Neutral")

    return {"confluence": confluence, "alignment": alignment,
           "higher_tf_confirms": higher_tf_confirms, "bias": bias,
           "unavailable": False, "reason": None}


_BIAS_SCORE = {"Strong Bullish": 1.0, "Bullish": 0.5, "Neutral": 0.0,
              "Bearish": -0.5, "Strong Bearish": -1.0}
TECHNICAL_WEIGHTS = {"vwap": 0.10, "ema": 0.10, "macd": 0.10, "rsi": 0.05,
                     "adx": 0.10, "atr": 0.05, "supertrend": 0.10,
                     "ichimoku": 0.20, "bollinger": 0.05, "stoch_rsi": 0.05,
                     "momentum": 0.10, "volume": 0.05, "mtf": 0.05}


def technical_output(candles, spot, vwap, regime):
    """Final aggregation layer — Technical Score (0-100), Technical
    Confirmation (5-level bias + Confidence%/Trend Strength%/
    Momentum%/Volatility%), Indicator Summary, Indicator Agreement%,
    Confirmation Status, and a recommended stop-loss distance (reusing
    the ATR engine's own suggested_stop_distance, not a second
    formula). Runs every engine ONCE and reuses each result for both
    the individual output and this aggregation — no indicator is
    computed twice. Per the spec's own explicit framing, this whole
    engine is a CONFIRMATION layer: `bias` here is meant to be checked
    against the Option Chain/Institutional Activity engines' own bias
    by a higher-level caller, not treated as a standalone trade call."""
    results = {
        "vwap": vwap_engine(candles, spot, vwap),
        "ema": ema_engine(candles),
        "macd": macd_engine(candles),
        "rsi": rsi_engine(candles),
        "adx": adx_engine(candles),
        "atr": atr_engine(candles),
        "supertrend": supertrend_engine(candles),
        "ichimoku": ichimoku_engine(candles),
        "bollinger": bollinger_engine(candles),
        "stoch_rsi": stoch_rsi_engine(candles),
        "momentum": momentum_engine(candles),
        "volume": volume_engine(candles),
        "mtf": mtf_engine(regime),
    }

    available = {k: v for k, v in results.items() if not v.get("unavailable")}
    unavailable = [k for k in results if k not in available]

    if not available:
        return {"technical_bias": "Neutral", "technical_score": 0,
               "confidence_pct": 0, "trend_strength_pct": 0,
               "momentum_pct": 0, "volatility_pct": 0,
               "indicator_summary": results, "indicator_agreement_pct": 0,
               "confirmation_status": "Insufficient Data",
               "recommended_stop_distance": None,
               "unavailable": unavailable}

    total_weight = sum(TECHNICAL_WEIGHTS[k] for k in available)
    weighted_score = sum(_BIAS_SCORE.get(v.get("bias", "Neutral"), 0) *
                         TECHNICAL_WEIGHTS[k] for k, v in available.items()) / total_weight
    technical_score = round((weighted_score + 1) / 2 * 100)

    if weighted_score >= 0.5:
        technical_bias = "Strong Bullish"
    elif weighted_score >= 0.15:
        technical_bias = "Bullish"
    elif weighted_score > -0.15:
        technical_bias = "Neutral"
    elif weighted_score > -0.5:
        technical_bias = "Bearish"
    else:
        technical_bias = "Strong Bearish"

    bullish_count = sum(1 for v in available.values() if "Bullish" in v.get("bias", ""))
    bearish_count = sum(1 for v in available.values() if "Bearish" in v.get("bias", ""))
    agreeing = max(bullish_count, bearish_count)
    indicator_agreement_pct = round(agreeing / len(available) * 100) if available else 0

    coverage = total_weight / sum(TECHNICAL_WEIGHTS.values())
    confidence_pct = round(min(100, abs(weighted_score) * 100) * (0.5 + 0.5 * coverage))

    trend_strength_pct = results["ema"].get("trend_strength") or 0
    momentum_pct = (round(min(100, abs(results["momentum"].get("roc_pct") or 0) * 10))
                    if not results["momentum"].get("unavailable") else 0)
    volatility_pct = (round(min(100, (results["atr"].get("atr_pct") or 0) * 20))
                      if not results["atr"].get("unavailable") else 0)

    if indicator_agreement_pct >= 70 and confidence_pct >= 50:
        confirmation_status = "Strong Confirmation"
    elif indicator_agreement_pct >= 50:
        confirmation_status = "Partial Confirmation"
    else:
        confirmation_status = "No Confirmation"

    stop_distance = results["atr"].get("suggested_stop_distance")

    return {"technical_bias": technical_bias, "technical_score": technical_score,
           "confidence_pct": confidence_pct,
           "trend_strength_pct": trend_strength_pct,
           "momentum_pct": momentum_pct, "volatility_pct": volatility_pct,
           "indicator_summary": results,
           "indicator_agreement_pct": indicator_agreement_pct,
           "confirmation_status": confirmation_status,
           "recommended_stop_distance": stop_distance,
           "unavailable": unavailable}


def generate_technical_commentary(output, institutional_bias=None):
    """AI Interpretation — rule-based (zero-latency, always available,
    same approach as Feature #4/#5's narrative generators), matching
    the spec's own example phrasing. Built entirely from `technical_
    output()`'s already-computed result."""
    lines = []
    ind = output["indicator_summary"]
    vwap = ind.get("vwap", {})
    ema = ind.get("ema", {})
    macd = ind.get("macd", {})
    ichi = ind.get("ichimoku", {})
    adx = ind.get("adx", {})
    vol = ind.get("volume", {})

    if vwap.get("position") == "above" and ema.get("alignment") == "bullish":
        lines.append("Price remains above VWAP with strong EMA alignment.")
    elif vwap.get("position") == "below" and ema.get("alignment") == "bearish":
        lines.append("Price remains below VWAP with a bearish EMA alignment.")

    if macd.get("histogram_expansion"):
        direction = "bullish" if (macd.get("histogram") or 0) > 0 else "bearish"
        lines.append(f"MACD histogram expanding confirms {direction} momentum.")

    if ichi.get("bias") == "Strong Bullish":
        lines.append("Ichimoku Cloud shows strong bullish continuation.")
    elif ichi.get("bias") == "Strong Bearish":
        lines.append("Ichimoku Cloud shows strong bearish continuation.")

    if adx.get("state") == "Strong Trend":
        lines.append(f"ADX at {adx.get('adx')} confirms trend strength.")

    if vol.get("breakout"):
        lines.append("Volume expansion supports the breakout.")

    if institutional_bias and output["technical_bias"] != "Neutral":
        tech_dir = "Bullish" in output["technical_bias"]
        inst_dir = "BULL" in (institutional_bias or "").upper()
        if tech_dir != inst_dir and "NEUTRAL" not in (institutional_bias or "").upper():
            lines.append("Technical indicators do not confirm Option Chain activity.")

    return lines[:6]


def vwap_engine(candles, spot, vwap):
    """VWAP Engine (increment 1) — Above/Below VWAP, VWAP Cross,
    Distance from VWAP, VWAP Slope, VWAP Retest. Returns Bullish/
    Bearish/Neutral.

    `vwap` is the CURRENT vwap value — already computed elsewhere
    (Feature #1's spot/futures TWAP-proxy VWAP, or Feature #3's
    merge_levels VWAP level) and passed in, not recomputed here.
    `candles` (recent OHLC, already fetched by RegimeAgent every 90s)
    is used only to detect a recent cross and the VWAP-relative slope.

    Honest disclosed approximation: this codebase doesn't persist a
    full tracked VWAP *series* anywhere (only the current single
    value) — the "cross"/"slope" reads below compare each recent
    candle's close against that SAME current vwap value, which is a
    reasonable approximation for a short lookback window (VWAP moves
    slowly relative to a handful of candles) but not a true point-in-
    time VWAP-at-each-candle series. Same honesty standard already
    applied to every other approximation in this codebase (VWAP-as-
    TWAP-proxy itself being the precedent)."""
    if not candles or spot is None or vwap is None or not vwap:
        return {"position": None, "cross": None, "distance_pct": None,
               "slope": None, "retest": False, "bias": "Neutral",
               "unavailable": True}

    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < 2:
        return {"position": None, "cross": None, "distance_pct": None,
               "slope": None, "retest": False, "bias": "Neutral",
               "unavailable": True}

    distance_pct = round((spot - vwap) / vwap * 100, 3)
    position = "above" if spot > vwap else "below" if spot < vwap else "at"

    # Cross: did the last few candles flip sides relative to vwap?
    recent = closes[-5:]
    sides = ["above" if c >= vwap else "below" for c in recent]
    cross = None
    for i in range(1, len(sides)):
        if sides[i] != sides[i - 1]:
            cross = "bullish" if sides[i] == "above" else "bearish"

    # Slope: is the spot-vs-vwap distance itself widening (trending
    # away from vwap) or narrowing/reversing, over a short lookback.
    slope = None
    if len(closes) >= 6:
        prior_distance = (closes[-6] - vwap) / vwap * 100
        if distance_pct > prior_distance + 0.02:
            slope = "rising"
        elif distance_pct < prior_distance - 0.02:
            slope = "falling"
        else:
            slope = "flat"

    # Retest: price crossed vwap recently AND has come back close to
    # it again (a common continuation-confirmation pattern) — "close"
    # defined as within 0.05% of vwap, same order of magnitude as the
    # slope threshold above.
    retest = cross is not None and abs(distance_pct) < 0.05

    if position == "above" and slope in ("rising", "flat", None):
        bias = "Bullish"
    elif position == "below" and slope in ("falling", "flat", None):
        bias = "Bearish"
    else:
        # position and slope disagree (e.g. above VWAP but falling
        # back toward it) — a weakening move, not a clean signal
        bias = "Neutral"

    return {"position": position, "cross": cross, "distance_pct": distance_pct,
           "slope": slope, "retest": retest, "bias": bias,
           "unavailable": False}
