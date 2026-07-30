"""ew_reversal.py — Strategy 8 (v58.28): EW-Reversal.

Ported from the Avadhut Sathe "GUE — Get the Ultimate Edge" workshop
deck (Elliott Wave + Advance TA). THREE reversal detectors live here
under ONE strategy id (`ew_reversal`), deliberately not three separate
strategies: they share the same pivot source, the same MACD-histogram
confirmation, the same stop/target construction, and the same daily
trade cap. Splitting them would triple the registration surface
(PA_NAMES / PA_DEFAULTS / PA_BOUNDS / config keys / docs) for three
variants of one idea — "price made a reversal pattern that the
histogram confirms".

  ending_diagonal : the deck's highest-value setup for option BUYING.
                    Five overlapping swings in a contracting wedge,
                    wave (iv) entering wave (i)'s price territory,
                    MACD-histogram divergence against price. Entry on
                    the break of the diagonal; the deck's own target is
                    "price retraces to the beginning of the diagonal" —
                    a fast, large move, which is the only kind that
                    beats theta on a weekly option.
  hs              : Head & Shoulder built from the deck's A-B-C framing
                    (wave C breaks the neckline). The deck's specific
                    tell is that the MACD histogram shows LESS bear
                    power at the neckline break than at the previous
                    low — "bears need less power to cause the
                    breakdown".
  failed_hs       : the mirror case, and a CONTINUATION trade, not a
                    reversal of the higher-degree trend. Per the deck:
                    an H&S fails when the TIDE is against it, and the
                    entry is taken once the termination point of wave B
                    is breached.

WHY THIS REUSES structure.zigzag_series()
-----------------------------------------
That function is already the single shared pivot definition consumed by
BOTH the chart and Strategy 7's structure gate — the project's explicit
parity requirement. A second, private swing detector here would mean
the Strategy 8 markers on the chart could disagree with the pivots the
chart itself draws, which is exactly the class of divergence the
existing module docstring was written to prevent. So: same pivots, same
deviation setting, no parallel definition.

NO-LOOKAHEAD NOTE
-----------------
zigzag_series() only EMITS a pivot once price has already reversed by
`deviation_pct` from the extreme — the running extreme it is currently
tracking is never returned. So every pivot this module sees is already
confirmed as of the last candle, and the pattern geometry cannot
repaint. The cost is honest and deliberate: the final pivot of a
pattern is only visible after price has moved `deviation_pct` away from
it, so entries here are structurally a little late. That is the correct
trade-off versus a detector that looks good in replay and fails live.

ISOLATION
---------
Nothing in this module is imported by any existing strategy. It is
called from exactly one new branch in PriceActionAgent.cycle(), behind
its own master switch (`strategy8_enabled`) and its own auto-deploy
gate (`s8_auto_deploy`, default OFF) — the same two-key pattern
Strategy 7 already uses.
"""


def _ema(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _macd_hist(closes, fast=12, slow=26, signal=9):
    """MACD histogram series aligned 1:1 with `closes`. Local rather
    than imported from pa_strategies so this module stays standalone —
    the deck uses the histogram for every one of its three patterns,
    and a shared import would couple S8's lifecycle to S1-S7's."""
    if len(closes) < slow + signal:
        return [0.0] * len(closes)
    ef, es = _ema(closes, fast), _ema(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sig = _ema(macd, signal)
    return [m - s for m, s in zip(macd, sig)]


def _time_index(candles):
    return {c["time"]: i for i, c in enumerate(candles)}


def _tide(c15, fast=5, slow=13, c5=None):
    """Higher-timeframe direction, the deck's "TIDE". Returns +1/-1/None.

    None means "not computable yet" and every caller MUST treat that as
    SKIP THE GATE, never as a rejection — the graceful-degradation rule
    this project has already had to fix twice (missing upstream data
    silently rejecting every signal)."""
    # v58.31 — falls back to the 5m series. EMA(13) on today-only 15m
    # candles is a 195-minute lookback in a 375-minute session, so a
    # 15m Tide does not exist until ~13:00 IST and this gate was inert
    # for most of every session. See ta_elliott.tide_of() for the full
    # reasoning and the measurement.
    src_c = c15 if (c15 and len(c15) >= slow + 2) else c5
    if not src_c or len(src_c) < slow + 2:
        return None
    closes = [c["close"] for c in src_c]
    ef, es = _ema(closes, fast), _ema(closes, slow)
    if ef[-1] > es[-1]:
        return +1
    if ef[-1] < es[-1]:
        return -1
    return None


def _span_ok(tmap, pivots, candles, min_bars):
    """Reject micro-patterns: the geometry must span at least
    `min_bars` candles end to end, otherwise a handful of adjacent
    1-minute wiggles satisfies the shape test and the "pattern" is
    noise."""
    try:
        first = tmap[pivots[0]["time"]]
        last = tmap[pivots[-1]["time"]]
    except KeyError:
        return False
    return (last - first) >= min_bars


def _hist_at(hist, tmap, pivot):
    i = tmap.get(pivot["time"])
    if i is None or i >= len(hist):
        return None
    return hist[i]


def _finalise(d, entry, stop, struct_target, rr, why, subtype, buf_pct):
    """Common stop/target construction for all three detectors.

    The structural target (diagonal origin / measured move) is the
    deck's own target and is kept — but it is placed at T2, with T1
    fixed at rr x risk. Reason, learned the hard way in this project
    already: the RiskAgent's risk-reward gate requires rr >= 1.95, and
    a purely structural T1 can land anywhere, including inside that
    gate, which silently rejects every signal the strategy produces.
    T1 therefore always satisfies the gate; T2 carries the structural
    objective when it is genuinely further away, otherwise it falls
    back to the standard 1.33x extension.
    """
    stop = stop * (1 - buf_pct / 100) if d > 0 else stop * (1 + buf_pct / 100)
    if (entry - stop) * d <= 0:
        return None                       # stop on the wrong side of price
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    t1 = entry + d * risk * rr
    if (struct_target - t1) * d > 0:
        t2 = struct_target                # structural objective is further out
    else:
        t2 = entry + d * risk * rr * 1.33
    return {"dir": d,
            "entry_spot": round(entry, 2),
            "stop_spot": round(stop, 2),
            "t1_spot": round(t1, 2),
            "t2_spot": round(t2, 2),
            "structural_stop": round(stop, 2),
            "structural_target": round(struct_target, 2),
            "setup_subtype": subtype,
            "why": why}


# ---------------------------------------------------------------- #
# Detector 1 — Ending Diagonal Triangle                             #
# ---------------------------------------------------------------- #

def _crossed(close, prev_close, level, direction):
    """Did price CROSS `level` on THIS bar, in `direction`?

    2026-07-30 -- found by the Pine parity oracle, which is exactly what
    it was built for. The S8 overlay marked `fHS` on roughly 12 of 30
    bars on a 5m NIFTY chart. A failed head-and-shoulder is a rare
    reversal-continuation event; firing on 40% of bars means no real
    constraint at all.

    The cause was in THIS module, not in Pine. Every break and reclaim
    test was a LEVEL comparison:

        broke     = close < p4["price"]
        reclaimed = close > pb["price"]

    Those remain true on EVERY subsequent bar once price is past the
    level, so the detector kept returning the same setup cycle after
    cycle instead of firing once at the break. Six such tests across the
    three detectors, all the same shape.

    Two things hid it. S8 was not evaluated at all until v58.37 (the
    auto_deploy gate sat before evaluation) and it still has no chart
    markers, so the repetition was never displayed. And
    `max_trades_per_day` capped the DAMAGE at two trades, so it never
    surfaced as a flood of orders -- the detector was wrong while the
    guardrail made the symptom invisible.

    direction -1 = downward break (price falls through the level)
    direction +1 = upward break or reclaim
    """
    if direction < 0:
        return close < level <= prev_close
    return close > level >= prev_close


def detect_ending_diagonal(candles, pivots, hist, tmap, p):
    """Deck slides 19 & 22. A motive wave found in wave 5 or wave C.

    Geometry on six alternating pivots p0..p5 (five legs):
      - the three pattern extremes still progress with the trend
        (p1 -> p3 -> p5 each further in the trend direction)
      - the counter-extremes progress too (p2 -> p4)
      - OVERLAP, the diagonal's signature: wave (iv) enters wave (i)'s
        price territory (p4 beyond p1) — this is what distinguishes a
        diagonal from a normal impulse, where it is forbidden
      - CONTRACTING: each successive leg width is smaller than the last
      - MACD histogram diverges against price at p5 vs p3

    Entry is the break of the diagonal, taken as a close beyond p4 (the
    wave-iv extreme, which is the lower/upper diagonal boundary's last
    confirmed touch). Target is the deck's own: the beginning of the
    diagonal, p0.
    """
    if len(pivots) < 6:
        return None
    p0, p1, p2, p3, p4, p5 = pivots[-6:]
    if not _span_ok(tmap, [p0, p5], candles, p["min_pattern_bars"]):
        return None
    close = candles[-1]["close"]
    # 2026-07-30 -- a break must be an EVENT, not a LEVEL. See _crossed().
    prev_close = candles[-2]["close"] if len(candles) >= 2 else close
    h3, h5 = _hist_at(hist, tmap, p3), _hist_at(hist, tmap, p5)

    # Bearish ending diagonal: terminates at a HIGH, expects a fall.
    if p0["type"] == "low" and p5["type"] == "high":
        rising = p5["price"] > p3["price"] > p1["price"] and p4["price"] > p2["price"]
        overlap = p4["price"] < p1["price"]
        w1 = p1["price"] - p0["price"]
        w2 = p3["price"] - p2["price"]
        w3 = p5["price"] - p4["price"]
        contracting = 0 < w3 < w2 < w1
        diverging = True
        if p["require_macd_divergence"]:
            diverging = (h3 is not None and h5 is not None and h5 < h3)
        broke = _crossed(close, prev_close, p4["price"], -1)
        if rising and overlap and contracting and diverging and broke:
            return _finalise(-1, close, p5["price"], p0["price"],
                             p["rr_target"],
                             f"ending diagonal (bearish): wave-iv {p4['price']:.0f} "
                             f"overlapped wave-i {p1['price']:.0f}, contracting "
                             f"{w1:.0f}->{w2:.0f}->{w3:.0f}, broke {p4['price']:.0f}",
                             "ending_diagonal", p["stop_buffer_pct"])

    # Bullish ending diagonal: terminates at a LOW, expects a rally.
    if p0["type"] == "high" and p5["type"] == "low":
        falling = p5["price"] < p3["price"] < p1["price"] and p4["price"] < p2["price"]
        overlap = p4["price"] > p1["price"]
        w1 = p0["price"] - p1["price"]
        w2 = p2["price"] - p3["price"]
        w3 = p4["price"] - p5["price"]
        contracting = 0 < w3 < w2 < w1
        diverging = True
        if p["require_macd_divergence"]:
            diverging = (h3 is not None and h5 is not None and h5 > h3)
        broke = _crossed(close, prev_close, p4["price"], +1)
        if falling and overlap and contracting and diverging and broke:
            return _finalise(+1, close, p5["price"], p0["price"],
                             p["rr_target"],
                             f"ending diagonal (bullish): wave-iv {p4['price']:.0f} "
                             f"overlapped wave-i {p1['price']:.0f}, contracting "
                             f"{w1:.0f}->{w2:.0f}->{w3:.0f}, broke {p4['price']:.0f}",
                             "ending_diagonal", p["stop_buffer_pct"])
    return None


# ---------------------------------------------------------------- #
# Detector 2 — Head & Shoulder (waves A-B-C breaking the neckline)   #
# ---------------------------------------------------------------- #

def _neckline_at(pa, pb, candles, tmap):
    """Neckline value projected to the CURRENT bar, by linear
    interpolation through the two intervening pivots — not a flat
    min()/max() shortcut, because a sloping neckline is the common
    case and a flat approximation systematically triggers early on a
    down-sloping neckline and late on an up-sloping one."""
    ia, ib = tmap.get(pa["time"]), tmap.get(pb["time"])
    if ia is None or ib is None or ib == ia:
        return min(pa["price"], pb["price"])
    slope = (pb["price"] - pa["price"]) / (ib - ia)
    return pb["price"] + slope * ((len(candles) - 1) - ib)


def detect_head_shoulder(candles, pivots, hist, tmap, p):
    """Deck slides 19 & 23. Six alternating pivots p0..p5 give
    left shoulder (p1), head (p3), right shoulder (p5), with p2/p4 the
    neckline anchors.

    The deck's distinctive confirmation is NOT a divergence in the
    usual sense: it asks that the MACD histogram show LESS bear power
    at the neckline break than at the previous low — "bears need less
    power to cause the breakdown", i.e. the move is efficient. That is
    implemented literally as hist(now) being less extreme than
    hist(p4), and is optional via require_macd_divergence.
    """
    # FIVE pivots, not six. The shoulders/head/neckline geometry only
    # ever references p1..p5 — an earlier draft demanded a sixth
    # (origin) pivot that the detector never reads, which silently
    # made every H&S in the first stretch of a session undetectable
    # because that origin pivot doesn't exist yet. The ending-diagonal
    # detector genuinely does need six (p0 IS its target), which is
    # why the two differ here.
    if len(pivots) < 5:
        return None
    p1, p2, p3, p4, p5 = pivots[-5:]
    if not _span_ok(tmap, [p1, p5], candles, p["min_pattern_bars"]):
        return None
    close = candles[-1]["close"]
    # 2026-07-30 -- a break must be an EVENT, not a LEVEL. See _crossed().
    prev_close = candles[-2]["close"] if len(candles) >= 2 else close
    hist_now = hist[-1] if hist else None
    h4 = _hist_at(hist, tmap, p4)
    tol = p["shoulder_tol_pct"]
    buf = p["neckline_buffer_pct"] / 100

    # Bearish H&S: shoulders are highs, head is the highest high.
    if p1["type"] == "high" and p3["type"] == "high" and p5["type"] == "high":
        head_ok = p3["price"] > p1["price"] and p3["price"] > p5["price"]
        sym = abs(p1["price"] - p5["price"]) / max(p3["price"], 1e-9) * 100 <= tol
        neck = _neckline_at(p2, p4, candles, tmap)
        broke = _crossed(close, prev_close, neck * (1 - buf), -1)
        power_ok = True
        if p["require_macd_divergence"]:
            power_ok = (hist_now is not None and h4 is not None and hist_now > h4)
        if head_ok and sym and broke and power_ok:
            return _finalise(-1, close, p5["price"], neck - (p3["price"] - neck),
                             p["rr_target"],
                             f"H&S: head {p3['price']:.0f} over shoulders "
                             f"{p1['price']:.0f}/{p5['price']:.0f}, neckline "
                             f"{neck:.0f} broken"
                             + (" with lesser bear power" if p["require_macd_divergence"] else ""),
                             "hs", p["stop_buffer_pct"])

    # Inverse H&S: shoulders are lows, head is the lowest low.
    if p1["type"] == "low" and p3["type"] == "low" and p5["type"] == "low":
        head_ok = p3["price"] < p1["price"] and p3["price"] < p5["price"]
        sym = abs(p1["price"] - p5["price"]) / max(p3["price"], 1e-9) * 100 <= tol
        neck = _neckline_at(p2, p4, candles, tmap)
        broke = _crossed(close, prev_close, neck * (1 + buf), +1)
        power_ok = True
        if p["require_macd_divergence"]:
            power_ok = (hist_now is not None and h4 is not None and hist_now < h4)
        if head_ok and sym and broke and power_ok:
            return _finalise(+1, close, p5["price"], neck + (neck - p3["price"]),
                             p["rr_target"],
                             f"inverse H&S: head {p3['price']:.0f} under shoulders "
                             f"{p1['price']:.0f}/{p5['price']:.0f}, neckline "
                             f"{neck:.0f} broken"
                             + (" with lesser bull power" if p["require_macd_divergence"] else ""),
                             "hs", p["stop_buffer_pct"])
    return None


# ---------------------------------------------------------------- #
# Detector 3 — Failed Head & Shoulder (continuation)                #
# ---------------------------------------------------------------- #

def detect_failed_hs(candles, pivots, hist, tmap, p, tide):
    """Deck slides 20 & 24. This is a CONTINUATION trade of the
    higher-degree trend, not a reversal — the deck is explicit that an
    H&S fails precisely when the TIDE is against it ("H&S on daily
    fails when weekly Tide is up").

    Geometry follows slide 24's own A-B-C labelling rather than the
    full six-point H&S: A (low), B (high), C (lower low that falsely
    breaks below A — the "False neckline BD" annotation on that slide),
    then the entry once the termination point of wave B is breached.

    `tide` may be None (not computable). Per this project's graceful-
    degradation rule that SKIPS the gate rather than rejecting — a
    missing higher timeframe must never silently veto every signal.
    """
    if len(pivots) < 3:
        return None
    pa, pb, pc = pivots[-3:]
    if not _span_ok(tmap, [pa, pc], candles, p["min_pattern_bars"]):
        return None
    close = candles[-1]["close"]
    # 2026-07-30 -- a break must be an EVENT, not a LEVEL. See _crossed().
    prev_close = candles[-2]["close"] if len(candles) >= 2 else close

    # Failed bearish H&S -> long continuation.
    if pa["type"] == "low" and pb["type"] == "high" and pc["type"] == "low":
        false_break = pc["price"] < pa["price"]
        reclaimed = _crossed(close, prev_close, pb["price"], +1)
        tide_ok = True
        if p["require_tide"] and tide is not None:
            tide_ok = tide > 0
        if false_break and reclaimed and tide_ok:
            height = pb["price"] - pc["price"]
            return _finalise(+1, close, pc["price"], pb["price"] + height,
                             p["rr_target"],
                             f"failed H&S: false breakdown to {pc['price']:.0f} "
                             f"below {pa['price']:.0f}, reclaimed wave-B "
                             f"{pb['price']:.0f}"
                             + (" with tide up" if p["require_tide"] and tide is not None else ""),
                             "failed_hs", p["stop_buffer_pct"])

    # Failed inverse H&S -> short continuation.
    if pa["type"] == "high" and pb["type"] == "low" and pc["type"] == "high":
        false_break = pc["price"] > pa["price"]
        reclaimed = _crossed(close, prev_close, pb["price"], -1)
        tide_ok = True
        if p["require_tide"] and tide is not None:
            tide_ok = tide < 0
        if false_break and reclaimed and tide_ok:
            height = pc["price"] - pb["price"]
            return _finalise(-1, close, pc["price"], pb["price"] - height,
                             p["rr_target"],
                             f"failed inverse H&S: false breakout to {pc['price']:.0f} "
                             f"above {pa['price']:.0f}, lost wave-B "
                             f"{pb['price']:.0f}"
                             + (" with tide down" if p["require_tide"] and tide is not None else ""),
                             "failed_hs", p["stop_buffer_pct"])
    return None


# ---------------------------------------------------------------- #
# Public entry point                                                #
# ---------------------------------------------------------------- #

DETECTOR_ORDER = ("ending_diagonal", "hs", "failed_hs")


def evaluate(c1, c5=None, c15=None, params=None, taken_today=0, pivots=None,
             shared_tide=None):
    """Returns (setup_or_None, detectors).

    `detectors` mirrors evaluate_sg_ema()'s `gates` contract so the
    eligibility card and Shadow Journal can render S8 with the plumbing
    that already exists for S7: each key is True (fired), False (shape
    not present) or a "skipped (<why>)" string.

    `pivots` must be structure.zigzag_series() output for the SAME 1m
    candles — the chart's own pivots (parity requirement). Passing None
    makes this function compute nothing and return a clean skip rather
    than silently building a second, different pivot series.

    Detectors run in DETECTOR_ORDER and the FIRST match wins. Ending
    diagonal is first deliberately: it is the deck's highest-conviction
    pattern and the only one whose target ("retrace to the beginning of
    the diagonal") is large and fast enough to reliably beat theta on a
    weekly option.
    """
    from pa_strategies import PA_DEFAULTS
    p = dict(PA_DEFAULTS["ew_reversal"], **(params or {}))
    detectors = {k: False for k in DETECTOR_ORDER}

    if not c1 or len(c1) < 30:
        return None, {k: "skipped (not enough candles)" for k in DETECTOR_ORDER}
    if taken_today >= p.get("max_trades_per_day", 2):
        return None, {k: "skipped (daily cap reached)" for k in DETECTOR_ORDER}
    if pivots is None:
        return None, {k: "skipped (no pivot series supplied)" for k in DETECTOR_ORDER}

    confirmed = [pv for pv in pivots if pv.get("structure")]
    if len(confirmed) < 3:
        return None, {k: "skipped (fewer than 3 confirmed pivots)"
                      for k in DETECTOR_ORDER}

    closes = [c["close"] for c in c1]
    hist = _macd_hist(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])
    tmap = _time_index(c1)
    # v58.29 — prefer the Tide already computed and published by
    # TAElliottAgent (bus key ta_state:{sym}) over recomputing the same
    # 15m EMA stack here. Falls back to local computation whenever the
    # shared value is absent, so this module still works standalone
    # (tests call it with no bus at all).
    tide = shared_tide if shared_tide is not None else _tide(c15, c5=c5)

    enabled = {
        "ending_diagonal": p.get("ending_diagonal_enabled", 1),
        "hs": p.get("hs_enabled", 1),
        "failed_hs": p.get("failed_hs_enabled", 1),
    }

    for key in DETECTOR_ORDER:
        if not enabled[key]:
            detectors[key] = "skipped (subtype off)"
            continue
        if key in ("ending_diagonal", "hs"):
            ev = (detect_ending_diagonal if key == "ending_diagonal"
                  else detect_head_shoulder)(c1, confirmed, hist, tmap, p)
            # v58.29 — the deck states plainly that these patterns FAIL
            # when the TIDE is against them ("H&S on daily fails when
            # weekly Tide is up"). v58.28 only enforced that inside
            # failed_hs, so a plain H&S could fire a short into a
            # rising Tide — precisely the failure the deck warns about.
            # Opt-in so v58.28's behaviour is reproducible by turning
            # it off. Tide None (not computable) SKIPS, never rejects.
            if ev and p.get("require_tide_all_detectors") and tide is not None:
                if ev["dir"] != tide:
                    detectors[key] = (f"blocked (tide {'up' if tide > 0 else 'down'} "
                                      f"against a {'long' if ev['dir'] > 0 else 'short'})")
                    continue
        else:
            if p.get("require_tide") and tide is None:
                detectors[key] = "skipped (tide not computable yet)"
                continue
            ev = detect_failed_hs(c1, confirmed, hist, tmap, p, tide)
        if ev:
            detectors[key] = True
            return ev, detectors
    return None, detectors
