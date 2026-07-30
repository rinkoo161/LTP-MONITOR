"""mtf_confluence_strategy.py — "MACD+Stoch Confluence" strategy from
rinkoo.docx (2026-07-23), the first new strategy requested there.

Rule set (written spec, not the more elaborate Pine Script divergence/
pivot/Fib-extension port — that's a separate, larger follow-up, see
ROADMAP.md):

  BULLISH confluence (all mandatory):
    1. Daily MACD histogram > 0 (above zero line) AND rising (uptick)
    2. Weekly MACD histogram turning up after being down
    3. Daily RSI(14) > 40
    4. Daily Stochastic %K crosses above %D from an oversold reading
       (<20 in the last few bars)
    5. Daily price in the upper Bollinger Band zone (%B > 0.8)
  Supportive (boosts confidence, not mandatory — matches the docx's
  own "mandatory conditions" + "supportive/better" structure, same
  pattern as the Triple Screen / 3rd Wave checklists in
  docs/strategy-reference/):
    6. Futures long buildup (price up + OI up) on the current-month
       future — degrades gracefully to "unavailable" if the hybrid
       websocket futures feed isn't running, rather than blocking on
       missing data.

  BEARISH confluence is the exact mirror (histogram<0 falling, weekly
  turning down after being up, RSI<60, stochastic bearish cross from
  overbought >80, %B<0.2, futures short buildup).

Recommended action per the docx's Futures & Options Guide reference
(docs/strategy-reference/future_and_options.pdf): bullish -> Bull Put
Spread / Covered Put / Long Future / Buy CE; bearish -> mirror. This
module returns BUY_CE/BUY_PE (feeding the existing options pipeline,
already tested end to end) as the primary actionable signal — the
spread/future recommendations are surfaced as text guidance alongside
it, not separately auto-executed (this system doesn't have a futures
position type at all yet; see ROADMAP.md).

All indicator math below is plain Python (no TA library dependency,
consistent with this project's zero-heavy-dependency approach) and
unit-tested against hand-computed reference values in
test_mtf_confluence.py.
"""
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- indicators

def adx_di(candles, period=14):
    """ADX + +DI/-DI — extracted 2026-07-25 from RegimeAgent._adx()'s
    existing math (agents.py) so Feature #7's Technical Analysis
    Engine can reuse the EXACT SAME calculation (which already existed
    and is already live in regime classification) instead of a second,
    potentially-drifting copy. RegimeAgent._adx() itself was refactored
    to call this and return just the `adx` value, confirmed byte-for-
    byte identical output for the same input before shipping.

    Returns (adx, plus_di, minus_di) — `adx` matches what RegimeAgent's
    own regime classification already uses (a simplified single-period
    DX read, not the fully Wilder-smoothed multi-period ADX some
    platforms show — same simplification RegimeAgent already made,
    documented there as "good enough to distinguish trending vs
    rangebound without a full library"). Returns (0, 0, 0) when there
    isn't enough data."""
    if len(candles) < period + 2:
        return 0, 0, 0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        dn = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if up > dn and up > 0 else 0)
        minus_dm.append(dn if dn > up and dn > 0 else 0)
        tr = max(candles[i]["high"] - candles[i]["low"],
                 abs(candles[i]["high"] - candles[i - 1]["close"]),
                 abs(candles[i]["low"] - candles[i - 1]["close"]))
        trs.append(tr)
    window = min(period, len(trs))
    atr_val = sum(trs[-window:]) / window
    if atr_val == 0:
        return 0, 0, 0
    pdi = 100 * (sum(plus_dm[-window:]) / window) / atr_val
    mdi = 100 * (sum(minus_dm[-window:]) / window) / atr_val
    if pdi + mdi == 0:
        return 0, pdi, mdi
    dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    return dx, pdi, mdi


def ema(values, period):
    """Standard EMA, SMA-seeded. Returns a list the same length as
    `values`, with None for indices before the seed is available."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram), each a list aligned
    to `closes` (None where not yet computable)."""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [None if (a is None or b is None) else a - b
                 for a, b in zip(ema_fast, ema_slow)]
    # signal line = EMA of the macd_line, computed only over its
    # non-None tail
    first_valid = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if first_valid is None:
        return macd_line, [None] * len(closes), [None] * len(closes)
    tail = macd_line[first_valid:]
    sig_tail = ema(tail, signal)
    signal_line = [None] * first_valid + sig_tail
    hist = [None if (m is None or s is None) else m - s
            for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def rsi(closes, period=14):
    """Wilder's RSI. Returns a list aligned to `closes`."""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        chg = closes[i] - closes[i - 1]
        gain = max(chg, 0)
        loss = max(-chg, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def stochastic(highs, lows, closes, k_period=14, k_smooth=1, d_period=3):
    """Returns (%K, %D), each aligned to closes."""
    n = len(closes)
    raw_k = [None] * n
    for i in range(k_period - 1, n):
        hh = max(highs[i - k_period + 1:i + 1])
        ll = min(lows[i - k_period + 1:i + 1])
        raw_k[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    k = _sma_skip_none(raw_k, k_smooth)
    d = _sma_skip_none(k, d_period)
    return k, d


def _sma_skip_none(values, period):
    if period <= 1:
        return list(values)
    out = [None] * len(values)
    for i in range(len(values)):
        window = values[max(0, i - period + 1):i + 1]
        if len(window) < period or any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


def bollinger_percent_b(closes, period=20, mult=2.0):
    """%B = (close - lower_band) / (upper_band - lower_band). >1 means
    price is above the upper band, <0 means below the lower band."""
    n = len(closes)
    out = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        basis = sum(window) / period
        var = sum((c - basis) ** 2 for c in window) / period
        dev = mult * (var ** 0.5)
        upper, lower = basis + dev, basis - dev
        out[i] = 0.5 if upper == lower else (closes[i] - lower) / (upper - lower)
    return out


def true_range(candles):
    tr = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return tr


def atr(candles, period=14):
    """Wilder's ATR — single latest value, not a full series (that's
    all the position-sizing formula needs)."""
    if len(candles) < period + 1:
        return None
    tr = true_range(candles)
    a = sum(tr[1:period + 1]) / period
    for t in tr[period + 1:]:
        a = (a * (period - 1) + t) / period
    return a


def resample_weekly(daily_candles):
    """Group daily candles into ISO-week OHLC bars. daily_candles must
    be oldest-first, each a dict with time (epoch seconds)/open/high/
    low/close."""
    weeks = {}
    order = []
    for c in daily_candles:
        dt = datetime.fromtimestamp(c["time"], IST)
        wk = dt.isocalendar()[:2]  # (iso_year, iso_week)
        if wk not in weeks:
            weeks[wk] = {"time": c["time"], "open": c["open"], "high": c["high"],
                        "low": c["low"], "close": c["close"]}
            order.append(wk)
        else:
            w = weeks[wk]
            w["high"] = max(w["high"], c["high"])
            w["low"] = min(w["low"], c["low"])
            w["close"] = c["close"]
    return [weeks[wk] for wk in order]


# ---------------------------------------------------------------- strategy

def _macd_hist_uptick_after_down(hist, lookback=10):
    """Weekly 'turning up after being down': the histogram was
    genuinely negative (below zero) within `lookback` bars, and the
    most recent bar is rising vs the one before it.

    Bug found 2026-07-23 (own testing): the original check was
    `min(valid[:-1]) < valid[-2]`, which is true for almost ANY rising
    series regardless of whether it was ever actually negative — e.g.
    a purely monotonic [1,2,3,...,10] incorrectly passed. Fixed to
    explicitly require a negative reading in the lookback window."""
    valid = [h for h in hist[-lookback:] if h is not None]
    if len(valid) < 3:
        return False
    was_down = any(v < 0 for v in valid[:-1])
    turning_up = valid[-1] > valid[-2]
    return was_down and turning_up


def _macd_hist_downtick_after_up(hist, lookback=10):
    """Mirror of the above — was genuinely positive, now turning down."""
    valid = [h for h in hist[-lookback:] if h is not None]
    if len(valid) < 3:
        return False
    was_up = any(v > 0 for v in valid[:-1])
    turning_down = valid[-1] < valid[-2]
    return was_up and turning_down


def _stoch_bull_cross_from_oversold(k, d, lookback=5, oversold=20):
    if len(k) < 2 or k[-1] is None or d[-1] is None or k[-2] is None or d[-2] is None:
        return False
    crossed_up = k[-2] <= d[-2] and k[-1] > d[-1]
    recent_k = [v for v in k[-lookback:] if v is not None]
    recent_d = [v for v in d[-lookback:] if v is not None]
    was_oversold = (recent_k and min(recent_k) < oversold) or \
                   (recent_d and min(recent_d) < oversold)
    return crossed_up and was_oversold


def _stoch_bear_cross_from_overbought(k, d, lookback=5, overbought=80):
    if len(k) < 2 or k[-1] is None or d[-1] is None or k[-2] is None or d[-2] is None:
        return False
    crossed_dn = k[-2] >= d[-2] and k[-1] < d[-1]
    recent_k = [v for v in k[-lookback:] if v is not None]
    recent_d = [v for v in d[-lookback:] if v is not None]
    was_overbought = (recent_k and max(recent_k) > overbought) or \
                     (recent_d and max(recent_d) > overbought)
    return crossed_dn and was_overbought


def evaluate(daily_candles, future_buildup=None, global_sentiment=None, min_bars=60):
    """Core evaluation. `daily_candles`: oldest-first list of
    {time,open,high,low,close}. `future_buildup`: "long" / "short" /
    None (unavailable — degrades gracefully, never blocks on this).
    `global_sentiment`: "risk_on" / "risk_off" / None — a broader,
    weaker supportive signal than future_buildup (derived from US
    index moves, not specific to this symbol), so it gets a smaller
    confidence adjustment (+/-5 vs future_buildup's +/-10) and is
    applied on top of whatever future_buildup already produced.

    Returns None (no confluence) or a dict:
      {"direction": "bullish"|"bearish", "confidence": int,
       "reasons": [...], "daily_atr14": float}
    """
    if len(daily_candles) < min_bars:
        return None
    closes = [c["close"] for c in daily_candles]
    highs = [c["high"] for c in daily_candles]
    lows = [c["low"] for c in daily_candles]

    _, _, d_hist = macd(closes)
    d_rsi = rsi(closes)
    d_k, d_d = stochastic(highs, lows, closes)
    d_pctb = bollinger_percent_b(closes)
    daily_atr14 = atr(daily_candles[-30:])

    weekly = resample_weekly(daily_candles)
    if len(weekly) < 30:
        return None   # not enough weekly history for a meaningful weekly MACD
    w_closes = [c["close"] for c in weekly]
    _, _, w_hist = macd(w_closes)

    if d_hist[-1] is None or d_rsi[-1] is None or d_pctb[-1] is None or w_hist[-1] is None:
        return None

    bull_conditions = {
        "daily MACD histogram above zero and rising":
            d_hist[-1] > 0 and d_hist[-2] is not None and d_hist[-1] > d_hist[-2],
        "weekly MACD turning up after being down": _macd_hist_uptick_after_down(w_hist),
        "daily RSI(14) > 40": d_rsi[-1] > 40,
        "daily Stochastic bullish cross from oversold": _stoch_bull_cross_from_oversold(d_k, d_d),
        "daily price in upper Bollinger Band zone (%B > 0.8)": d_pctb[-1] > 0.8,
    }
    bear_conditions = {
        "daily MACD histogram below zero and falling":
            d_hist[-1] < 0 and d_hist[-2] is not None and d_hist[-1] < d_hist[-2],
        "weekly MACD turning down after being up": _macd_hist_downtick_after_up(w_hist),
        "daily RSI(14) < 60": d_rsi[-1] < 60,
        "daily Stochastic bearish cross from overbought": _stoch_bear_cross_from_overbought(d_k, d_d),
        "daily price in lower Bollinger Band zone (%B < 0.2)": d_pctb[-1] < 0.2,
    }

    bull_met = sum(bull_conditions.values())
    bear_met = sum(bear_conditions.values())

    # Mandatory: ALL 5 conditions for either side — matches the docx's
    # "then deeply Bull put strategy..." wording (a firm recommendation,
    # not a partial-match maybe). Futures buildup is supportive only —
    # it can raise confidence but a missing/absent reading never blocks.
    if bull_met == 5:
        direction = "bullish"
        reasons = [k for k, v in bull_conditions.items() if v]
        confidence = 85
        if future_buildup == "long":
            confidence = 95
            reasons.append("futures long buildup (supportive)")
        elif future_buildup == "short":
            confidence = 75   # supportive signal conflicts — lower, don't block
            reasons.append("futures buildup is SHORT — conflicts with bullish "
                           "confluence, confidence reduced (not blocked)")
    elif bear_met == 5:
        direction = "bearish"
        reasons = [k for k, v in bear_conditions.items() if v]
        confidence = 85
        if future_buildup == "short":
            confidence = 95
            reasons.append("futures short buildup (supportive)")
        elif future_buildup == "long":
            confidence = 75
            reasons.append("futures buildup is LONG — conflicts with bearish "
                           "confluence, confidence reduced (not blocked)")
    else:
        return None

    if global_sentiment == "risk_on":
        if direction == "bullish":
            confidence = min(100, confidence + 5)
            reasons.append("global markets risk-on (supportive)")
        elif direction == "bearish":
            confidence = max(0, confidence - 5)
            reasons.append("global markets risk-on — conflicts with bearish "
                           "confluence, confidence reduced (not blocked)")
    elif global_sentiment == "risk_off":
        if direction == "bearish":
            confidence = min(100, confidence + 5)
            reasons.append("global markets risk-off (supportive)")
        elif direction == "bullish":
            confidence = max(0, confidence - 5)
            reasons.append("global markets risk-off — conflicts with bullish "
                           "confluence, confidence reduced (not blocked)")

    return {
        "direction": direction, "confidence": confidence, "reasons": reasons,
        "daily_atr14": daily_atr14,
    }


# Recommended F&O actions per docs/strategy-reference/future_and_options.pdf
# — surfaced as guidance text alongside the BUY_CE/BUY_PE signal this
# module actually emits (see pa_strategies.py integration). This system
# has no futures position type yet, so "Long the Future" is descriptive
# only until that's built (see ROADMAP.md).
RECOMMENDED_ACTIONS = {
    "bullish": ["Bull Put Spread", "Covered Put", "Long Future", "Buy CE"],
    "bearish": ["Bear Call Spread", "Covered Call", "Short Future", "Buy PE"],
}
