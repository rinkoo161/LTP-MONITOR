"""structure.py — market-structure primitives shared by the chart
websocket and Strategy 7 (SG-EMA).

Extracted from app.py 2026-07-26 (v51). Strategy 7's structure gate must
consume the IDENTICAL ZigZag the chart draws (the spec's parity
requirement), and agents.py cannot import app.py at module level
(circular: app imports agents). Moving the pure function here lets both
sides import one implementation instead of maintaining two.
"""


def zigzag_series(candles, deviation_pct=0.5):
    """ZigZag market-structure indicator, per explicit request.
    Standard reversal-threshold algorithm: tracks a running extreme in
    the current direction; once price reverses by more than `deviation
    _pct`% from that extreme, the extreme is confirmed as a pivot and
    tracking flips to the opposite direction. Each confirmed pivot is
    then classified against the PREVIOUS pivot of the SAME type
    (highs compared to the last high, lows to the last low, skipping
    the alternating opposite-type pivot in between) into Higher High/
    Higher Low/Lower High/Lower Low — the market-structure read the
    spec explicitly asks for, not just raw swing points.

    Returns a list of {"time", "price", "type": "high"/"low",
    "structure": "HH"/"HL"/"LH"/"LL"/None (first pivot of that type
    has nothing to compare against yet)}, in chronological order."""
    if not candles or len(candles) < 3:
        return []
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    times = [c["time"] for c in candles]

    pivots = []
    direction = None   # "up" while tracking toward a high, "down" toward a low
    extreme_idx = 0
    extreme_price = highs[0]

    for i in range(1, len(candles)):
        if direction in (None, "up"):
            if highs[i] >= extreme_price:
                extreme_price, extreme_idx = highs[i], i
                direction = "up"
            elif (extreme_price - lows[i]) / extreme_price * 100 >= deviation_pct:
                pivots.append({"time": times[extreme_idx], "price": extreme_price, "type": "high"})
                direction = "down"
                extreme_price, extreme_idx = lows[i], i
        if direction == "down":
            if lows[i] <= extreme_price:
                extreme_price, extreme_idx = lows[i], i
            elif (highs[i] - extreme_price) / extreme_price * 100 >= deviation_pct:
                pivots.append({"time": times[extreme_idx], "price": extreme_price, "type": "low"})
                direction = "up"
                extreme_price, extreme_idx = highs[i], i

    last_high = last_low = None
    for p in pivots:
        if p["type"] == "high":
            p["structure"] = ("HH" if last_high is None or p["price"] > last_high
                             else "LH" if p["price"] < last_high else "HH")
            last_high = p["price"]
        else:
            p["structure"] = ("HL" if last_low is None or p["price"] > last_low
                             else "LL" if p["price"] < last_low else "HL")
            last_low = p["price"]
    return pivots


def detect_liquidity_sweeps(candles, pivots=None, lookback=20, deviation_pct=0.5):
    """v58.9 (item 9) — Liquidity sweep detection: a candle whose WICK
    breaks beyond a prior confirmed swing high/low (the resting stop-
    loss orders / "liquidity" above a swing high or below a swing low
    in Smart Money Concepts terminology) but whose CLOSE comes back
    inside it — the classic "stop hunt then reverse" pattern. Reuses
    `zigzag_series()`'s own pivot detection rather than a second,
    parallel definition of "swing point" (the SAME pivots this module
    already computes for Strategy 7's structure gate).

      - bullish_sweep: a candle's LOW breaks below a prior confirmed
        pivot LOW, but its CLOSE is back ABOVE that pivot level —
        liquidity below support was grabbed, then rejected upward.
      - bearish_sweep: the mirror — a candle's HIGH breaks above a
        prior confirmed pivot HIGH, but its CLOSE comes back below it.

    Only the most recent `lookback` candles are scanned for sweep
    candles (older sweeps are stale and not relevant to a live entry
    decision); the pivot being swept can be from anywhere earlier in
    `pivots` since a swing point doesn't expire just because it's old.

    Returns a list of {"time", "type": "bullish_sweep"/"bearish_sweep",
    "swept_level": price, "close": price}, chronological order.
    """
    if not candles or len(candles) < 3:
        return []
    if pivots is None:
        pivots = zigzag_series(candles, deviation_pct)
    if not pivots:
        return []
    scan_from = max(0, len(candles) - lookback)
    sweeps = []
    for i in range(scan_from, len(candles)):
        c = candles[i]
        # Only pivots CONFIRMED before this candle's own time are real
        # prior swing points from this candle's perspective — using a
        # pivot from later in the series would be looking ahead.
        prior_lows = [p["price"] for p in pivots if p["type"] == "low" and p["time"] < c["time"]]
        prior_highs = [p["price"] for p in pivots if p["type"] == "high" and p["time"] < c["time"]]
        if prior_lows:
            most_recent_low = prior_lows[-1]
            if c["low"] < most_recent_low and c["close"] > most_recent_low:
                sweeps.append({"time": c["time"], "type": "bullish_sweep",
                              "swept_level": most_recent_low, "close": c["close"]})
        if prior_highs:
            most_recent_high = prior_highs[-1]
            if c["high"] > most_recent_high and c["close"] < most_recent_high:
                sweeps.append({"time": c["time"], "type": "bearish_sweep",
                              "swept_level": most_recent_high, "close": c["close"]})
    return sweeps


def detect_fair_value_gaps(candles, lookback=20):
    """v58.9 (item 9) — Fair Value Gap (FVG) detection: the standard
    3-candle imbalance pattern (Smart Money Concepts terminology) —
    candle 1 and candle 3 leave a gap between them that candle 2's
    range doesn't fill, marking a zone price tends to return to.

      - bullish_fvg: candle[i-2].high < candle[i].low — a gap below
        that can act as SUPPORT if price returns to it.
      - bearish_fvg: candle[i-2].low > candle[i].high — a gap above
        that can act as RESISTANCE if price returns to it.

    Only gaps formed within the most recent `lookback` candles are
    returned (an FVG from weeks ago is unlikely to still be a live
    reference zone for a same-session decision). `filled` marks
    whether any LATER candle's range has already traded back through
    the gap (a filled gap has already done its job and is less
    significant going forward, but still returned — the CALLER decides
    whether filled gaps still count for its purposes).

    Returns a list of {"time" (of the middle candle), "type":
    "bullish_fvg"/"bearish_fvg", "gap_low", "gap_high", "filled": bool},
    chronological order.
    """
    if not candles or len(candles) < 3:
        return []
    scan_from = max(2, len(candles) - lookback)
    gaps = []
    for i in range(scan_from, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c1["high"] < c3["low"]:
            gap_low, gap_high = c1["high"], c3["low"]
            filled = any(candles[j]["low"] <= gap_low for j in range(i + 1, len(candles)))
            gaps.append({"time": candles[i - 1]["time"], "type": "bullish_fvg",
                        "gap_low": gap_low, "gap_high": gap_high, "filled": filled})
        elif c1["low"] > c3["high"]:
            gap_low, gap_high = c3["high"], c1["low"]
            filled = any(candles[j]["high"] >= gap_high for j in range(i + 1, len(candles)))
            gaps.append({"time": candles[i - 1]["time"], "type": "bearish_fvg",
                        "gap_low": gap_low, "gap_high": gap_high, "filled": filled})
    return gaps


def wall_confluence(candles, level, direction, lookback=20, proximity_pct=0.3,
                    pivots=None):
    """v58.9 (item 9) — layers liquidity-sweep/FVG confluence ONTO an
    existing OI-wall level, per the roadmap's own framing ("Liquidity-
    sweep / FVG confluence on top of the OI-wall logic") — the OI wall
    stays the PRIMARY selection mechanism (which strike to sell); this
    only asks "does recent price action ALSO support this wall,"
    an additional confirmation layer, not a replacement for OI-wall
    selection.

    `direction`: "support" (checking a PUT wall, e.g. bull_put_spread's
    S1 — a BULLISH sweep or bullish FVG near/below this level
    reinforces "price should hold above here") or "resistance"
    (checking a CALL wall, e.g. bear_call_spread's R1 — a BEARISH sweep
    or bearish FVG near/above this level reinforces "price should stay
    below here").

    `proximity_pct`: how close (as % of level) a sweep/FVG needs to be
    to the wall level to count as confluence, rather than an unrelated
    pattern from a different part of the session.

    Returns {"confirmed": bool, "reasons": [str, ...]} — reasons is
    always populated (even when not confirmed, explaining why not) so
    a caller can log/display WHY confluence did or didn't apply,
    matching this project's own "never a silent number" convention.
    """
    reasons = []
    sweeps = detect_liquidity_sweeps(candles, pivots, lookback)
    gaps = detect_fair_value_gaps(candles, lookback)
    want_sweep_type = "bullish_sweep" if direction == "support" else "bearish_sweep"
    want_fvg_type = "bullish_fvg" if direction == "support" else "bearish_fvg"

    confirmed = False
    for s in sweeps:
        if s["type"] != want_sweep_type:
            continue
        if abs(s["swept_level"] - level) / level * 100 <= proximity_pct:
            reasons.append(f"{s['type']} at {s['swept_level']:.1f}, "
                          f"within {proximity_pct}% of the {level:.1f} wall")
            confirmed = True

    for g in gaps:
        if g["type"] != want_fvg_type:
            continue
        gap_mid = (g["gap_low"] + g["gap_high"]) / 2
        if abs(gap_mid - level) / level * 100 <= proximity_pct:
            reasons.append(f"{g['type']} ({g['gap_low']:.1f}-{g['gap_high']:.1f}), "
                          f"within {proximity_pct}% of the {level:.1f} wall"
                          + (" [already filled]" if g["filled"] else ""))
            confirmed = True

    if not reasons:
        reasons.append(f"no liquidity sweep or FVG found near the {level:.1f} "
                       f"{direction} wall within the last {lookback} candles")
    return {"confirmed": confirmed, "reasons": reasons}


