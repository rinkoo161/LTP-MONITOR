"""ta_elliott.py — Strategy 9 (v58.29): "TA with Elliott".

The OTHER half of the Avadhut Sathe "Get the Ultimate Edge" deck.
Strategy 8 (ew_reversal.py) implemented three of the deck's seven
High Probability Setups — the reversal patterns. This implements the
deck's slide 10, "Marrying TA with Elliot", plus slides 11-18 and the
"Points to Note" on slide 28: the indicator layer that classifies
IMPULSE vs CORRECTIVE and times an entry at the end of a correction,
in the direction of the higher timeframe.

The deck's own framing, in its own order:

  slide 28  "Your 1st step should be to identify if price is in an
             impulse or correction."  ... "Never forget, in order to
             make profitable trades, you need an impulse... Must use
             Bollinger Bands."
  slide 16  band direction IS the impulse/corrective classifier:
             price tags a band AND the band turns with it -> impulse;
             flat band -> corrective (wave 4 or B); price tags a band
             but the band does NOT turn -> the correction is ending.
  slide 13  GMMA compression then expansion -> end of corrective wave,
             beginning of the next impulse.
  slide 14  "Reverse divergence" (price higher low, MACD lower low) ->
             end of corrective wave, beginning of next impulse. NOTE:
             this is what the wider literature calls HIDDEN divergence,
             and it is a CONTINUATION signal — which is exactly what
             the deck claims for it. Named hidden_* here so a future
             reader doesn't mistake it for ordinary divergence.
  slide 21  end of Wave 4 shows MACD doing a ZERO LINE reversal.
  slide 15  5th waves at lower degrees end with RSI divergence.
  slide 18  Bollinger + ADX identifies "dynamic" (strongly trending)
             waves.
  slide 20  the TIDE (higher timeframe) must favour the trade.
  slide 28  "When in doubt, Do Not Trade."

So: trade WITH the Tide, only at the END of a correction, only when
enough independent indicators agree, and never when the state is
ambiguous.

NO WAVE COUNTING IS PERFORMED. Every signal above is an indicator
state, not a wave label. That is deliberate — the wave-counting layer
is the least mechanizable part of the deck and this strategy is built
so it is not needed.

WHY THIS RUNS IN ITS OWN AGENT
------------------------------
compute_state() builds twelve GMMA EMAs, Bollinger bands, ADX, MACD,
RSI and a pivot series per symbol. PriceActionAgent already loops six
strategies across every symbol on a 60s cycle; adding this there would
multiply that cycle's work for a strategy whose inputs (5m/15m
candles) cannot meaningfully change more than once every few minutes.
TAElliottAgent owns it instead, on a slower interval, and PUBLISHES
the computed state to the bus so other agents read it instead of
recomputing — see agents.TAElliottAgent. compute_state() is also
memoised on the last candle timestamp, so two calls within the same
candle cost one computation.
"""

import structure

TA_ELLIOTT_DEFAULTS = {
    "bb_period": 20, "bb_stdev": 2.0, "bb_slope_eps": 0.00015,
    "bb_min_width_pct": 0.15,
    "gmma_compression_pct": 25.0, "gmma_lookback": 60,
    "gmma_timeframe": "1m",
    "gmma_min_separation_pct": 0.05,
    "adx_period": 14, "adx_dynamic_min": 20.0,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "rsi_period": 14,
    "tide_fast": 5, "tide_slow": 13, "tide_use_15m": 0,
    "min_confluence": 3,
    "require_tide": 1,
    "require_corrective_phase": 0,
    "zigzag_deviation_pct": 0.5,
    "stop_buffer_pct": 0.05,
    "rr_target": 2.0,
    "max_trades_per_day": 2,
}

# (lo, hi, relax_direction) — relax moves toward the permissive end.
# rr_target's floor is 1.95, never lower: the RiskAgent's risk-reward
# gate rejects anything below that, so a tuner permitted to step under
# it would silently produce a strategy whose every signal is
# auto-rejected downstream. Same reasoning as ew_reversal's bounds.
TA_ELLIOTT_BOUNDS = {
    "ta_elliott": {
        "min_confluence": (2, 5, -1),
        "bb_slope_eps": (0.0001, 0.0020, +1),
        "bb_min_width_pct": (0.02, 0.60, -1),
        "gmma_compression_pct": (10.0, 50.0, +1),
        "gmma_min_separation_pct": (0.01, 0.30, -1),
        "adx_dynamic_min": (10.0, 35.0, -1),
        "require_tide": (0, 1, -1),
        "require_corrective_phase": (0, 1, -1),
        "stop_buffer_pct": (0.0, 0.30, +1),
        "rr_target": (1.95, 3.5, -1),
    }
}

BINARY_KEYS = ("require_tide", "require_corrective_phase")


# ------------------------------------------------------------------ #
# primitives                                                          #
# ------------------------------------------------------------------ #

def _ema(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _rma(vals, n):
    """Wilder's smoothing — what RSI and ADX are actually defined on.
    An EMA(n) is NOT the same thing and produces visibly different ADX
    readings, which matters here because adx_dynamic_min is a
    threshold the deck's "dynamic wave" test depends on."""
    if not vals:
        return []
    out = [vals[0]]
    for v in vals[1:]:
        out.append((out[-1] * (n - 1) + v) / n)
    return out


def _sma(vals, n):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / min(i + 1, n))
    return out


def _stdev(vals, n):
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - n + 1):i + 1]
        m = sum(w) / len(w)
        out.append((sum((x - m) ** 2 for x in w) / len(w)) ** 0.5)
    return out


def _macd(closes, fast, slow, signal):
    ef, es = _ema(closes, fast), _ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = _ema(line, signal)
    return line, sig, [m - s for m, s in zip(line, sig)]


def _rsi(closes, n):
    if len(closes) < 2:
        return [50.0] * len(closes)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = _rma(gains, n), _rma(losses, n)
    out = []
    for g, l in zip(ag, al):
        if l == 0:
            out.append(100.0)
        else:
            rs = g / l
            out.append(100 - 100 / (1 + rs))
    return out


def _adx(candles, n):
    """Returns (adx, plus_di, minus_di) series aligned to `candles`."""
    if len(candles) < 2:
        z = [0.0] * len(candles)
        return z, z, list(z)
    tr, pdm, ndm = [0.0], [0.0], [0.0]
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = (candles[i - 1]["high"], candles[i - 1]["low"],
                      candles[i - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    atr, spdm, sndm = _rma(tr, n), _rma(pdm, n), _rma(ndm, n)
    pdi, ndi, dx = [], [], []
    for a, p, m in zip(atr, spdm, sndm):
        if a <= 0:
            pdi.append(0.0), ndi.append(0.0), dx.append(0.0)
            continue
        pv, mv = 100 * p / a, 100 * m / a
        pdi.append(pv)
        ndi.append(mv)
        dx.append(0.0 if (pv + mv) == 0 else 100 * abs(pv - mv) / (pv + mv))
    return _rma(dx, n), pdi, ndi


def _percentile(vals, pct):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(len(s) * pct / 100.0)))
    return s[k]


# ------------------------------------------------------------------ #
# deck concepts                                                       #
# ------------------------------------------------------------------ #

GMMA_SHORT = (3, 5, 8, 10, 12, 15)
GMMA_LONG = (30, 35, 40, 45, 50, 60)


def gmma_series_for(c1, c5, p):
    """Pick the candle series GMMA is computed on.

    2026-07-29, measured: `gmma_state` returned None on 79.1% of 211
    real observations. Not NEUTRAL — NOT COMPUTABLE. It needs
    max(GMMA_LONG) + 5 = 65 bars, and on 5m that is 325 minutes of a
    375-minute session, so the indicator does not exist until ~14:40
    IST. `gmma_expansion` firing 0% was never a threshold problem: the
    ribbon was simply absent for four fifths of every day.

    The 30-60 long bank is calibrated for DAILY charts, which is what
    the source deck uses it on. Intraday the same periods on 1m give a
    60-minute lookback — available from ~10:15, a sensible "recent"
    horizon for a strategy deciding on 5m structure — instead of a
    5.4-hour one that outlives the session.

    Same class of error as the Tide horizon fixed in v58.31: a period
    count copied from a daily chart onto an intraday series.
    """
    tf = p.get("gmma_timeframe", "1m")
    primary, fallback = (c1, c5) if tf == "1m" else (c5, c1)
    need = max(GMMA_LONG) + 5
    if primary and len(primary) >= need:
        return [c["close"] for c in primary], tf
    if fallback and len(fallback) >= need:
        return [c["close"] for c in fallback], ("5m" if tf == "1m" else "1m")
    # Neither series is long enough. Return the longer one so
    # gmma_state reports its own "not enough candles" reason rather
    # than us inventing one — but label it with the timeframe it
    # ACTUALLY came from, not the one that was requested. That label is
    # persisted to the calibration table as `gmma_tf`, so a wrong value
    # here would quietly corrupt the very data this logging exists to
    # provide.
    other = "5m" if tf == "1m" else "1m"
    if len(primary or []) >= len(fallback or []):
        return [c["close"] for c in (primary or [])], tf
    return [c["close"] for c in (fallback or [])], other


def gmma_state(closes, p):
    """Deck slide 13 — moving-average COMPRESSION then EXPANSION marks
    the end of a corrective wave and the start of the next impulse.

    Compression is measured as the short bank's spread relative to its
    own recent distribution (a percentile), not an absolute number, so
    the same setting works on NIFTY at 24,000 and SENSEX at 80,000.
    """
    if len(closes) < max(GMMA_LONG) + 5:
        return {"state": None, "reason": "not enough candles for GMMA"}
    shorts = [_ema(closes, n) for n in GMMA_SHORT]
    longs = [_ema(closes, n) for n in GMMA_LONG]
    px = closes[-1] or 1e-9
    spread_hist = []
    lb = min(int(p["gmma_lookback"]), len(closes))
    for i in range(len(closes) - lb, len(closes)):
        vals = [s[i] for s in shorts]
        spread_hist.append((max(vals) - min(vals)) / max(abs(closes[i]), 1e-9))
    cur = spread_hist[-1]
    thresh = _percentile(spread_hist, p["gmma_compression_pct"])
    s_now = [s[-1] for s in shorts]
    l_now = [l[-1] for l in longs]
    # Separation must be MEANINGFUL, not merely arithmetic. Found by
    # test: in a dead range every EMA converges to within a rounding
    # error of the same value, and min(short) > max(long) then comes
    # out True on floating-point noise — reporting a cleanly fanned
    # ribbon where there is actually a flat line. Same failure class as
    # the collapsed-Bollinger-band guard above: a degenerate geometry
    # must not be allowed to masquerade as a signal.
    gap_up = (min(s_now) - max(l_now)) / max(abs(px), 1e-9)
    gap_dn = (min(l_now) - max(s_now)) / max(abs(px), 1e-9)
    min_gap = p["gmma_min_separation_pct"] / 100
    above = gap_up >= min_gap
    below = gap_dn >= min_gap
    separated = above or below

    # SEPARATION is checked before the spread percentile, and a
    # separated ribbon can never be "compressed" regardless of what the
    # percentile says. Found by test: on a sustained rally the EMA
    # spread PEAKS mid-move and then eases as the trend reaches steady
    # state (all EMAs settle to a constant lag, and normalising by a
    # rising price shrinks the ratio further). The percentile test alone
    # therefore labelled a fully fanned-out ribbon "COMPRESSED" — the
    # exact opposite of the truth, and it would have told the strategy
    # a raging trend was a quiet coil. Compression means the bands are
    # INTERLEAVED and tight, which is what the deck's slide 13 chart
    # actually shows.
    #
    # Expansion is likewise measured over several bars rather than one:
    # a single-bar comparison flips on noise, and the deck's signal is
    # a sustained fanning-out, not a one-bar tick.
    ref = spread_hist[-5] if len(spread_hist) >= 5 else spread_hist[0]
    widening = cur > ref
    if separated and widening:
        return {"state": "EXPANDING_UP" if above else "EXPANDING_DOWN",
                "spread": cur, "thresh": thresh, "separated": True,
                "reason": ("GMMA expanding, short bank "
                           + ("above" if above else "below") + " long bank")}
    if not separated and cur <= thresh:
        return {"state": "COMPRESSED", "spread": cur, "thresh": thresh,
                "separated": False,
                "reason": (f"GMMA compressed - banks interleaved, spread "
                           f"{cur:.5f} <= p{p['gmma_compression_pct']:.0f}")}
    return {"state": "NEUTRAL", "spread": cur, "thresh": thresh,
            "separated": separated,
            "reason": "GMMA neither compressed nor expanding"}


def bollinger_state(candles, p):
    """Deck slides 16-17 — the band's DIRECTION is the impulse vs
    corrective classifier, and slide 28 makes it mandatory ("Must use
    Bollinger Bands").

      IMPULSE_UP/DOWN   price tags a band and the band turns with it
      CORRECTIVE_FLAT   band is flat -> a wave 4 or B is in progress
      CORRECTIVE_STALL  price tags a band but the band FAILS to turn
                        -> the deck's own "end of corrective wave"
                        buy/sell trigger
    """
    closes = [c["close"] for c in candles]
    n = int(p["bb_period"])
    if len(closes) < n + 3:
        return {"state": None, "reason": "not enough candles for Bollinger"}
    mid = _sma(closes, n)
    sd = _stdev(closes, n)
    upper = [m + p["bb_stdev"] * s for m, s in zip(mid, sd)]
    lower = [m - p["bb_stdev"] * s for m, s in zip(mid, sd)]
    px = closes[-1]
    # Slope normalised by price so one eps works across all indices.
    slope = (mid[-1] - mid[-3]) / 2 / max(abs(px), 1e-9)
    eps = p["bb_slope_eps"]
    width_now = upper[-1] - lower[-1]
    width_prev = upper[-3] - lower[-3]
    expanding = width_now > width_prev
    tag_up = candles[-1]["high"] >= upper[-1]
    tag_dn = candles[-1]["low"] <= lower[-1]
    ctx = {"mid": mid[-1], "upper": upper[-1], "lower": lower[-1],
           "slope": slope, "width": width_now, "expanding": expanding}

    # The deck's test (slide 16) is exactly two conditions: price
    # touches the band, AND the band moves in the direction of price.
    # An earlier draft also required the band WIDTH to be expanding —
    # that is not in the source document, and it made a sustained
    # steady trend (constant volatility, so constant width) classify as
    # NEUTRAL instead of IMPULSE. Width is kept in the payload for
    # consumers but is no longer part of the impulse test.
    #
    # A minimum band width IS required before any band tag is treated
    # as meaningful: in a dead range the bands collapse to a hair's
    # breadth and ordinary candle wicks tag them constantly, which
    # produced spurious CORRECTIVE_STALL "the correction is ending"
    # readings out of pure noise.
    meaningful = (width_now / max(abs(px), 1e-9)) >= p["bb_min_width_pct"] / 100
    if not meaningful:
        return dict(ctx, state="CORRECTIVE_FLAT",
                    reason=f"band collapsed (width {width_now / px * 100:.3f}% of "
                           f"price) - dead range, band tags are not meaningful")
    if tag_up and slope > eps:
        return dict(ctx, state="IMPULSE_UP",
                    reason="price at upper band with the band turning up")
    if tag_dn and slope < -eps:
        return dict(ctx, state="IMPULSE_DOWN",
                    reason="price at lower band with the band turning down")
    if tag_dn and slope >= -eps:
        return dict(ctx, state="CORRECTIVE_STALL", stall_dir=+1,
                    reason="price tagged the lower band but the band did not turn down")
    if tag_up and slope <= eps:
        return dict(ctx, state="CORRECTIVE_STALL", stall_dir=-1,
                    reason="price tagged the upper band but the band did not turn up")
    if abs(slope) <= eps and not expanding:
        return dict(ctx, state="CORRECTIVE_FLAT",
                    reason="flat band, contracting width - wave 4 or B in progress")
    return dict(ctx, state="NEUTRAL", reason="no Bollinger classification")


def divergences(candles, pivots, osc, tmap):
    """Regular and hidden divergence between price pivots and an
    oscillator sampled at those SAME pivots — one pivot definition, the
    project's parity rule, rather than a second private swing finder.

    hidden_bull (price higher low, oscillator lower low) is the deck's
    "Reverse Divergence" from slide 14 and is a CONTINUATION signal.
    """
    out = {"regular_bull": False, "regular_bear": False,
           "hidden_bull": False, "hidden_bear": False}
    lows = [p for p in pivots if p["type"] == "low"][-2:]
    highs = [p for p in pivots if p["type"] == "high"][-2:]
    if len(lows) == 2:
        a, b = lows
        oa, ob = tmap.get(a["time"]), tmap.get(b["time"])
        if oa is not None and ob is not None and oa < len(osc) and ob < len(osc):
            if b["price"] < a["price"] and osc[ob] > osc[oa]:
                out["regular_bull"] = True
            if b["price"] > a["price"] and osc[ob] < osc[oa]:
                out["hidden_bull"] = True
    if len(highs) == 2:
        a, b = highs
        oa, ob = tmap.get(a["time"]), tmap.get(b["time"])
        if oa is not None and ob is not None and oa < len(osc) and ob < len(osc):
            if b["price"] > a["price"] and osc[ob] < osc[oa]:
                out["regular_bear"] = True
            if b["price"] < a["price"] and osc[ob] > osc[oa]:
                out["hidden_bear"] = True
    return out


def tide_of(c15, p, c5=None):
    """Deck slide 20 — the TIDE. Returns +1/-1/None; None means NOT
    COMPUTABLE and every caller must SKIP the gate, never reject on it
    (this project's graceful-degradation rule).

    HORIZON, and why it is 5m and not 15m by default
    ------------------------------------------------
    EMA(13) on 15m candles is a 195-minute lookback. An Indian index
    session is 375 minutes and these strategies are fed TODAY's candles
    only, so a 15m Tide does not exist until 3h45m in (~13:00 IST) —
    measured, not estimated. That made the Tide gate inert for most of
    every session: S9 could not pick a direction and S8's tide gate
    skipped rather than filtered.

    Scaling the periods to a faster series does NOT fix it: an EMA over
    the same amount of TIME needs the same amount of time regardless of
    bar size. The horizon itself was the mistake. A 65-minute Tide on
    5m candles is available from ~10:20 and is the appropriate "one
    degree up" for strategies trading 1m/5m structure — which is what
    the deck's Triple Screen actually asks for, relative to the trading
    timeframe rather than an absolute daily/weekly.

    Set tide_use_15m to prefer the longer horizon when it exists; it
    then falls back to 5m rather than returning None, so the gate is
    never silently inert again.
    """
    src = None
    if p.get("tide_use_15m") and c15 and len(c15) >= int(p["tide_slow"]) + 2:
        src = c15
    elif c5 and len(c5) >= int(p["tide_slow"]) + 2:
        src = c5
    elif c15 and len(c15) >= int(p["tide_slow"]) + 2:
        src = c15
    if not src:
        return None
    closes = [c["close"] for c in src]
    ef = _ema(closes, int(p["tide_fast"]))
    es = _ema(closes, int(p["tide_slow"]))
    if ef[-1] > es[-1]:
        return +1
    if ef[-1] < es[-1]:
        return -1
    return None


# ------------------------------------------------------------------ #
# state computation (published to the bus, shared by other agents)     #
# ------------------------------------------------------------------ #

_CACHE = {}


def compute_state(symbol, c1, c5, c15, params=None):
    """Build the full TA-with-Elliott state for one symbol.

    Memoised on (symbol, last 5m candle timestamp, last 15m candle
    timestamp): two calls within the same candle return the identical
    object without recomputing. This is what makes it safe for other
    agents to read this state as often as they like.
    """
    p = dict(TA_ELLIOTT_DEFAULTS, **(params or {}))
    if not c5 or len(c5) < int(p["bb_period"]) + 3:
        return {"ok": False, "reason": "not enough 5m candles", "symbol": symbol}

    # Keyed on param VALUES, not identity. An earlier version used
    # id(params), which looked correct in a test that reused one dict
    # but never hit in production: TAElliottAgent rebuilds its params
    # dict every cycle, so id() changed each time and the cache missed
    # unconditionally — the memoisation this agent's whole performance
    # argument rests on was silently doing nothing. Values are all
    # scalars, so a sorted tuple is a safe, cheap identity.
    # The CLOSE is part of the key, not just the timestamp. The most
    # recent candle is still FORMING: its time stays fixed for the whole
    # bucket while its close keeps moving. Keying on time alone would
    # serve a stale state for every call inside that bucket — which is
    # most calls, since this agent's 180s cycle runs roughly twice per
    # 5m candle. Found while writing the backtest replay, where the
    # effect is starker (five 1m bars share one forming 5m candle).
    last5, last15 = c5[-1], (c15 or [{}])[-1]
    # .get() throughout: a caller may hand us candles without a
    # timestamp (the backtest resampler did exactly that until v58.31).
    # Missing time degrades the cache to close-only keying, which is
    # merely less efficient — it must never raise.
    key = (symbol, last5.get("time"), last5.get("close"),
           last15.get("time"), last15.get("close"),
           tuple(sorted((k, v) for k, v in p.items())))
    hit = _CACHE.get(symbol)
    if hit and hit[0] == key:
        return hit[1]

    closes5 = [c["close"] for c in c5]
    macd_line, macd_sig, macd_hist = _macd(closes5, int(p["macd_fast"]),
                                           int(p["macd_slow"]), int(p["macd_signal"]))
    rsi = _rsi(closes5, int(p["rsi_period"]))
    adx, pdi, ndi = _adx(c5, int(p["adx_period"]))
    bb = bollinger_state(c5, p)
    _gm_closes, _gm_tf = gmma_series_for(c1, c5, p)
    gm = gmma_state(_gm_closes, p)
    gm["timeframe"] = _gm_tf
    tide = tide_of(c15, p, c5=c5)

    piv5 = structure.zigzag_series(c5, p["zigzag_deviation_pct"])
    tmap5 = {c["time"]: i for i, c in enumerate(c5)}
    div_macd = divergences(c5, piv5, macd_hist, tmap5)
    div_rsi = divergences(c5, piv5, rsi, tmap5)

    # Deck slide 21 — "End of Wave 4 with MACD doing Zero Line
    # reversal". Detected as the MACD line crossing zero within the
    # last few bars, in either direction.
    zero_rev = 0
    for i in range(max(1, len(macd_line) - 4), len(macd_line)):
        if macd_line[i - 1] <= 0 < macd_line[i]:
            zero_rev = +1
        elif macd_line[i - 1] >= 0 > macd_line[i]:
            zero_rev = -1

    dynamic = adx[-1] >= p["adx_dynamic_min"] if adx else False

    # Deck slide 28's first instruction, made explicit: is price in an
    # impulse or a correction? Everything downstream keys off this.
    if bb["state"] in ("IMPULSE_UP", "IMPULSE_DOWN"):
        phase = "IMPULSE"
    elif bb["state"] in ("CORRECTIVE_FLAT", "CORRECTIVE_STALL"):
        phase = "CORRECTIVE"
    elif gm["state"] == "COMPRESSED":
        phase = "CORRECTIVE"
    else:
        phase = "UNCLEAR"

    state = {
        "ok": True, "symbol": symbol,
        "as_of": c5[-1].get("time"),      # per-symbol candle time, never wall clock
        "phase": phase,
        "tide": tide,
        "bb": bb, "gmma": gm,
        "adx": round(adx[-1], 2) if adx else None,
        "plus_di": round(pdi[-1], 2) if pdi else None,
        "minus_di": round(ndi[-1], 2) if ndi else None,
        "dynamic": bool(dynamic),
        "macd": round(macd_line[-1], 4), "macd_hist": round(macd_hist[-1], 4),
        "macd_zero_reversal": zero_rev,
        "rsi": round(rsi[-1], 2),
        "div_macd": div_macd, "div_rsi": div_rsi,
        "pivots_5m": len(piv5),
        # RAW inputs, not just the states derived from them. The
        # calibration table stored only conclusions, so it could show
        # that `bb_slope_eps` was too high without showing what it
        # should be, and could not diagnose the dead divergence signals
        # at all. Thresholds set from a distribution beat thresholds
        # set from a guess.
        "raw": {
            "bb_slope": round(bb.get("slope"), 8) if bb.get("slope") is not None else None,
            "bb_width_pct": (round(bb["width"] / max(abs(closes5[-1]), 1e-9) * 100, 4)
                             if bb.get("width") is not None else None),
            "gmma_spread": round(gm["spread"], 8) if gm.get("spread") is not None else None,
            "gmma_thresh": round(gm["thresh"], 8) if gm.get("thresh") is not None else None,
            "gmma_tf": _gm_tf,
            "pivots_5m": len(piv5),
            "pivot_lows": sum(1 for x in piv5 if x["type"] == "low"),
            "pivot_highs": sum(1 for x in piv5 if x["type"] == "high"),
        },
    }
    # Router hint consumed by other agents: impulse phases suit option
    # BUYING (a fast directional move beats theta), corrective phases
    # suit premium SELLING. Published, not enforced — the consuming
    # agent decides.
    state["route"] = ("BUY_OPTIONS" if phase == "IMPULSE"
                      else "SPREADS" if phase == "CORRECTIVE" else "NO_TRADE")
    _CACHE[symbol] = (key, state)
    return state


# ------------------------------------------------------------------ #
# strategy evaluation                                                 #
# ------------------------------------------------------------------ #

def evaluate(state, c1=None, params=None, taken_today=0, pivots=None):
    """Returns (setup_or_None, confluence).

    `confluence` mirrors ew_reversal's `detectors` / sg_ema's `gates`
    contract so the eligibility card and Shadow Journal render it with
    plumbing that already exists.

    Entry, in the deck's own order:
      1. Tide must favour the trade (slide 20) — a hard veto, but
         SKIPPED when the 15m series isn't available yet.
      2. Price must be in a CORRECTIVE phase that is ENDING, not an
         impulse already under way — the deck buys the end of wave 2/4,
         it does not chase.
      3. Enough independent indicators must agree (min_confluence).
      4. Otherwise: no trade. Slide 28, "When in doubt, Do Not Trade."
    """
    p = dict(TA_ELLIOTT_DEFAULTS, **(params or {}))
    conf = {}
    if not state or not state.get("ok"):
        why = (state or {}).get("reason", "no state")
        return None, {"state": f"skipped ({why})"}
    if taken_today >= p.get("max_trades_per_day", 2):
        return None, {"state": "skipped (daily cap reached)"}

    tide = state.get("tide")
    if p.get("require_tide"):
        if tide is None:
            conf["tide"] = "skipped (15m not computable yet)"
        elif tide == 0:
            return None, {"tide": False, "why": "tide flat"}
        else:
            conf["tide"] = True
    else:
        conf["tide"] = "skipped (gate off)"

    # Direction: with the Tide when we have one. Without a Tide we fall
    # back to the direction the corrective stall itself implies, which
    # is the only honest reading when the higher timeframe is absent.
    bb = state.get("bb") or {}
    d = tide if tide else bb.get("stall_dir")
    if not d:
        return None, dict(conf, direction="skipped (no direction available)")

    # The gate is "do not CHASE an impulse", not "must be positively
    # classified as corrective". Measured on replay: the Bollinger
    # classifier returns a positive CORRECTIVE reading on only ~2% of
    # bars and UNCLEAR on ~72% — price is usually mid-band with a
    # drifting mean, which is genuinely neither. Requiring a positive
    # CORRECTIVE label therefore made the strategy structurally unable
    # to trade at ANY confluence threshold, including 1 of 7.
    #
    # A bar that is not an impulse is a valid place to enter; the deck's
    # "When in doubt, Do Not Trade" is carried by min_confluence, which
    # is the real filter, not by the phase label. ta_require_corrective_
    # phase restores the strict reading for anyone who wants it.
    phase = state.get("phase")
    if phase == "IMPULSE":
        return None, dict(conf, phase="blocked (impulse already under way - "
                                      "this strategy buys the END of a correction)")
    if p.get("require_corrective_phase") and phase != "CORRECTIVE":
        return None, dict(conf, phase=f"blocked (phase={phase}, strict mode)")
    conf["phase"] = phase

    gm = (state.get("gmma") or {}).get("state")
    dm, dr = state.get("div_macd") or {}, state.get("div_rsi") or {}
    hits, why = [], []

    def add(name, ok, text):
        conf[name] = bool(ok)
        if ok:
            hits.append(name)
            why.append(text)

    add("bb_corrective_stall",
        bb.get("state") == "CORRECTIVE_STALL" and bb.get("stall_dir") == d,
        bb.get("reason", ""))
    add("gmma_expansion",
        (gm == "EXPANDING_UP" and d > 0) or (gm == "EXPANDING_DOWN" and d < 0),
        (state.get("gmma") or {}).get("reason", ""))
    add("macd_zero_reversal", state.get("macd_zero_reversal") == d,
        "MACD zero-line reversal in the trade direction (end of wave 4)")
    add("hidden_divergence",
        (dm.get("hidden_bull") and d > 0) or (dm.get("hidden_bear") and d < 0),
        "reverse/hidden MACD divergence - correction ending")
    add("regular_divergence",
        (dm.get("regular_bull") and d > 0) or (dm.get("regular_bear") and d < 0),
        "regular MACD divergence at the correction's extreme")
    add("rsi_divergence",
        (dr.get("regular_bull") and d > 0) or (dr.get("regular_bear") and d < 0),
        "RSI divergence")
    add("adx_dynamic", bool(state.get("dynamic")),
        f"ADX {state.get('adx')} >= {p['adx_dynamic_min']} (dynamic wave)")

    need = int(p["min_confluence"])
    conf["count"] = f"{len(hits)}/{need}"
    if len(hits) < need:
        return None, conf

    # Structural stop from the CHART's ZigZag on 1m (parity), falling
    # back to the Bollinger band when no usable pivot exists — never
    # silently skipping the trade for want of a pivot.
    spot = (c1 or [{}])[-1].get("close") or bb.get("mid")
    if not spot:
        return None, dict(conf, stop="skipped (no price)")
    stop = bb.get("lower") if d > 0 else bb.get("upper")
    side = "low" if d > 0 else "high"
    cand = [pv for pv in (pivots or []) if pv.get("structure") and pv["type"] == side]
    if cand and (spot - cand[-1]["price"]) * d > 0:
        stop = cand[-1]["price"]
        conf["stop_source"] = "zigzag pivot"
    else:
        conf["stop_source"] = "bollinger band"
    if stop is None or (spot - stop) * d <= 0:
        return None, dict(conf, stop="skipped (no valid stop on the correct side)")

    stop = stop * (1 - p["stop_buffer_pct"] / 100) if d > 0 else \
        stop * (1 + p["stop_buffer_pct"] / 100)
    risk = abs(spot - stop)
    if risk <= 0:
        return None, dict(conf, stop="skipped (zero risk distance)")
    rr = p["rr_target"]
    return {"dir": d, "entry_spot": round(spot, 2), "stop_spot": round(stop, 2),
            "t1_spot": round(spot + d * risk * rr, 2),
            "t2_spot": round(spot + d * risk * rr * 1.33, 2),
            "structural_stop": round(stop, 2),
            "setup_subtype": "ta_elliott",
            "why": f"{len(hits)}/{need} confluence at the end of a correction: "
                   + "; ".join(w for w in why if w)}, conf


def tune(params, direction):
    """One bounded relax (+1) / tighten (-1) step, same mechanics as
    pa_strategies.tune(). Binary keys flip rather than stepping — a
    (0,1) bound stepped by 0.25 yields truthy fractions that leave a
    gate nominally on while displaying a meaningless value."""
    p = dict(params)
    changes = []
    for key, (lo, hi, relax_dir) in TA_ELLIOTT_BOUNDS["ta_elliott"].items():
        step_dir = relax_dir * direction
        cur = p.get(key, TA_ELLIOTT_DEFAULTS[key])
        if key in BINARY_KEYS:
            new = 0 if step_dir < 0 else 1
        elif isinstance(TA_ELLIOTT_DEFAULTS[key], int) and key == "min_confluence":
            new = int(min(hi, max(lo, cur + (1 if step_dir > 0 else -1))))
        else:
            new = round(min(hi, max(lo, cur + (hi - lo) * 0.25 * step_dir)), 5)
        if new != cur:
            p[key] = new
            changes.append(f"{key} {cur}->{new}")
    return p, changes
