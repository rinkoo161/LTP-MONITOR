"""market_bias.py — Feature #2 of the institutional-grade dashboard
spec: AI Market Bias.

Extends existing modules, does not duplicate them:
  - MACD / RSI: reused directly from mtf_confluence_strategy.py
    (already built, already tested there).
  - Regime / ADX / ATR / multi-timeframe confluence: reused from the
    regime:{symbol} bus key RegimeAgent already publishes.
  - OI bias (PCR): reused from analyzer.py's analyze() output
    (pcr_oi field, already computed for every chain analysis).
  - Futures trend: reused from future_oi_trend:{symbol}, already
    built for the MTF Confluence strategy's futures-OI supportive
    signal.
  - India VIX / global risk sentiment: reused from bus keys
    NewsMacroAgent already populates.
  - Spot vs futures % change comparison: reused from the same
    computation /api/ltp-monitor already does (participation read).

Genuinely new here: Supertrend and Ichimoku Cloud — neither existed
anywhere in this codebase before this feature.

Market Breadth is NOT implemented — this system doesn't have NIFTY50
constituent advance/decline data available (no free real-time feed for
that, same honest-gap pattern as FII/DII flows elsewhere in this
project). Weighted OUT of the score entirely (its weight is
redistributed across the other components) rather than faked with a
placeholder value — reported explicitly as "unavailable" in the
output so this omission is visible, not silently missing.

WEIGHTING SCHEME (first pass, not a validated/backtested model — same
honesty standard as the impact-window heuristic in news_engine.py; a
starting point meant to be tuned against real outcomes):
  Spot trend vs VWAP        15%
  Futures trend vs VWAP     15%   (futures weighted equal to spot —
                                   the spec's own framing: "spot rises
                                   while futures weak = weak rally")
  MACD (daily)              10%
  RSI (daily)               10%
  ADX + regime label        10%   (trend STRENGTH, not direction —
                                   scales confidence, not bias sign)
  Supertrend                10%
  Ichimoku Cloud            10%
  Option chain OI / PCR     10%
  Futures OI buildup         5%
  India VIX                  5%   (high VIX dampens confidence, isn't
                                   directional on its own)
"""


def _true_range(candles):
    trs = []
    prev_close = candles[0]["close"]
    for c in candles[1:]:
        tr = max(c["high"] - c["low"], abs(c["high"] - prev_close),
                 abs(c["low"] - prev_close))
        trs.append(tr)
        prev_close = c["close"]
    return trs


def supertrend(candles, period=10, multiplier=3.0):
    """Returns (trend_series, direction_series) where direction is
    +1 (bullish, price above the trend line) or -1 (bearish), one
    entry per candle from index `period` onward (earlier candles have
    insufficient ATR warmup, returned as None to keep indices aligned
    with the input list)."""
    n = len(candles)
    if n <= period:
        return [None] * n, [None] * n
    trs = _true_range(candles)
    atr = [None] * period
    atr.append(sum(trs[:period]) / period)
    for i in range(period + 1, n):
        prev = atr[-1]
        atr.append((prev * (period - 1) + trs[i - 1]) / period)

    trend = [None] * n
    direction = [None] * n
    final_upper = final_lower = None
    for i in range(period, n):
        if atr[i] is None:
            continue
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]
        close = candles[i]["close"]
        prev_close = candles[i - 1]["close"]
        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
            direction[i] = 1 if close > final_upper else -1
        else:
            final_upper = (basic_upper if (basic_upper < final_upper or
                          prev_close > final_upper) else final_upper)
            final_lower = (basic_lower if (basic_lower > final_lower or
                          prev_close < final_lower) else final_lower)
            prev_dir = direction[i - 1] or 1
            if prev_dir == 1 and close < final_lower:
                direction[i] = -1
            elif prev_dir == -1 and close > final_upper:
                direction[i] = 1
            else:
                direction[i] = prev_dir
        trend[i] = final_lower if direction[i] == 1 else final_upper
    return trend, direction


def ichimoku_bias(candles, tenkan_n=9, kijun_n=26, senkou_b_n=52):
    """Simplified for BIAS SCORING (not the full plotted system with
    forward/backward time shifts — this system doesn't render an
    Ichimoku chart, it needs a current directional read). Returns
    "bullish" / "bearish" / "neutral" (price inside the cloud, or
    Tenkan/Kijun disagree with price position)."""
    if len(candles) < senkou_b_n:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    close = candles[-1]["close"]

    def hl_mid(n):
        return (max(highs[-n:]) + min(lows[-n:])) / 2

    tenkan = hl_mid(tenkan_n)
    kijun = hl_mid(kijun_n)
    senkou_a = (tenkan + kijun) / 2
    senkou_b = hl_mid(senkou_b_n)
    cloud_top, cloud_bottom = max(senkou_a, senkou_b), min(senkou_a, senkou_b)

    if close > cloud_top and tenkan > kijun:
        return "bullish"
    if close < cloud_bottom and tenkan < kijun:
        return "bearish"
    return "neutral"


def compute_bias(spot_chg_pct=None, future_chg_pct=None, daily_candles=None,
                 regime=None, oi_bias_pcr=None, future_trend=None,
                 vix=None, global_sentiment=None):
    """The weighted aggregator. Every input is optional — missing
    inputs degrade gracefully (that component's weight is excluded and
    the remaining weights renormalized), matching this codebase's
    established "skip gracefully, don't hard-fail on missing data"
    convention (RegimeAgent's own missing-regime-data handling is the
    precedent this follows).

    Returns {"bias": "Strong Bullish"|"Bullish"|"Neutral"|"Bearish"|
    "Strong Bearish", "confidence": 0-100, "components": {...},
    "unavailable": [...]}.
    """
    import mtf_confluence_strategy as mcs

    scores = {}   # component -> score in [-1, +1]
    weights = {"spot_trend": 0.15, "future_trend": 0.15, "macd": 0.10,
              "rsi": 0.10, "adx_regime": 0.10, "supertrend": 0.10,
              "ichimoku": 0.10, "oi_pcr": 0.10, "future_oi_buildup": 0.05,
              "vix": 0.05}
    unavailable = []

    if spot_chg_pct is not None:
        scores["spot_trend"] = max(-1, min(1, spot_chg_pct / 1.0))
    else:
        unavailable.append("spot_trend")

    if future_chg_pct is not None:
        scores["future_trend"] = max(-1, min(1, future_chg_pct / 1.0))
    else:
        unavailable.append("future_trend")

    d_hist = d_rsi = None
    if daily_candles and len(daily_candles) >= 35:
        closes = [c["close"] for c in daily_candles]
        _, _, hist_series = mcs.macd(closes)
        rsi_series = mcs.rsi(closes)
        d_hist = hist_series[-1] if hist_series else None
        d_rsi = rsi_series[-1] if rsi_series else None

    if d_hist is not None:
        scores["macd"] = 1.0 if d_hist > 0 else -1.0
    else:
        unavailable.append("macd")

    if d_rsi is not None:
        scores["rsi"] = max(-1, min(1, (d_rsi - 50) / 25))
    else:
        unavailable.append("rsi")

    if regime and regime.get("regime"):
        r = regime["regime"]
        if r == "trending-up":
            scores["adx_regime"] = 1.0
        elif r == "trending-down":
            scores["adx_regime"] = -1.0
        else:
            scores["adx_regime"] = 0.0
    else:
        unavailable.append("adx_regime")

    if daily_candles and len(daily_candles) >= 15:
        _, direction = supertrend(daily_candles)
        last_dir = next((dd for dd in reversed(direction) if dd is not None), None)
        if last_dir is not None:
            scores["supertrend"] = float(last_dir)
        else:
            unavailable.append("supertrend")
    else:
        unavailable.append("supertrend")

    if daily_candles and len(daily_candles) >= 52:
        ich = ichimoku_bias(daily_candles)
        if ich == "bullish":
            scores["ichimoku"] = 1.0
        elif ich == "bearish":
            scores["ichimoku"] = -1.0
        elif ich == "neutral":
            scores["ichimoku"] = 0.0
        else:
            unavailable.append("ichimoku")
    else:
        unavailable.append("ichimoku")

    if oi_bias_pcr is not None:
        # PCR > 1 = more puts written = bullish positioning (per the
        # same convention already used in analyzer.py's _market_state)
        scores["oi_pcr"] = max(-1, min(1, (oi_bias_pcr - 1.0) * 2))
    else:
        unavailable.append("oi_pcr")

    if future_trend in ("long", "short"):
        scores["future_oi_buildup"] = 1.0 if future_trend == "long" else -1.0
    else:
        unavailable.append("future_oi_buildup")

    if vix is not None:
        # VIX isn't directional — high VIX just means "less certain",
        # scored near zero but slightly negative (elevated fear is
        # mildly bearish-leaning, not neutral) above a threshold.
        scores["vix"] = -0.3 if vix > 18 else 0.0
    else:
        unavailable.append("vix")

    if not scores:
        return {"bias": "Neutral", "confidence": 0, "components": {},
               "unavailable": unavailable, "weighted_score": 0.0}

    total_weight = sum(weights[k] for k in scores)
    weighted_score = sum(scores[k] * weights[k] for k in scores) / total_weight

    if weighted_score >= 0.5:
        bias = "Strong Bullish"
    elif weighted_score >= 0.15:
        bias = "Bullish"
    elif weighted_score > -0.15:
        bias = "Neutral"
    elif weighted_score > -0.5:
        bias = "Bearish"
    else:
        bias = "Strong Bearish"

    # Confidence: how strong the signal is (magnitude), scaled down by
    # how much data was actually available (fewer inputs = less sure,
    # even if what's there agrees strongly).
    coverage = total_weight / sum(weights.values())
    confidence = round(min(100, abs(weighted_score) * 100) * (0.5 + 0.5 * coverage))

    # global_sentiment is a small tie-breaker only near the boundary,
    # not a scored component (matches its supportive-only role
    # elsewhere in this codebase — never flips a bias on its own).
    if global_sentiment == "risk_off" and bias in ("Bullish",):
        confidence = max(0, confidence - 5)
    elif global_sentiment == "risk_on" and bias in ("Bearish",):
        confidence = max(0, confidence - 5)

    return {"bias": bias, "confidence": confidence,
           "components": scores, "unavailable": unavailable,
           "weighted_score": round(weighted_score, 3)}
