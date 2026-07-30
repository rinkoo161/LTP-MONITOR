"""support_resistance.py — Feature #3 of the institutional-grade
dashboard spec: dynamic R1-R3 / S1-S3 and entry-criteria checking.

RETAINED, not rebuilt: analyzer.py's ranked_levels() already computes
R1-R3/S1-S3 from OI + OI-change + volume concentration, with strength
percentages and blue/yellow/pink color coding, and is already used
live by the spread-selling strategies (wall detection). That's a
solid, tested, institutionally-relevant S/R source — for options
trading specifically, OI walls are arguably the MOST relevant level
type (where large positions are actually built), more so than generic
pivot-style levels. Per the explicit instruction to retain a better
existing implementation rather than replace it: ranked_levels() stays
the PRIMARY source here, not superseded.

What's genuinely NEW in this module: Previous Day High/Low/Close
levels (didn't exist anywhere) and a VWAP-anchored level (VWAP itself
existed as of Feature #1, but wasn't used as a support/resistance
reference). These are combined with the existing OI-wall levels into
one merged, source-tagged R1-R3/S1-S3 view, plus the spec's own entry-
criteria framework (price above S1, S2 as emergency stop, R1/R2/R3 as
target ladder).

HONEST GAP: "Volume Profile" and "Price Acceptance" from the spec are
NOT implemented. Both need tick-level volume-at-price distribution
data over the session, which this system does not retain (candle data
here is OHLC only, no per-price volume histogram). Same honest-gap
pattern as Market Breadth in Feature #2 and FII/DII flows elsewhere in
this project — not faked with a placeholder, explicitly reported as
unavailable.
"""


def previous_day_levels(candles, symbol=None):
    """DB-first (2026-07-25, per explicit request to persist daily
    OHLC rather than re-deriving it from a live candle fetch every
    cycle): if `symbol` is given and history.py has a persisted
    daily_ohlc row for a prior day, use that directly. Falls back to
    deriving it from the live multi-day candle series (the original
    behavior) when no persisted data exists yet — e.g. the very first
    session after this feature was added, before any day has closed
    and been written to the DB. Never hard-fails to None just because
    the DB is still empty."""
    if symbol:
        try:
            import history
            db_prev = history.get_previous_day_ohlc(symbol)
            if db_prev:
                return db_prev
        except Exception:
            pass   # DB unavailable for some reason — fall through to
                  # the live-candle derivation below rather than
                  # losing Previous Day levels entirely over a
                  # transient DB issue
    from datetime import datetime
    from agents import IST, now_ist
    if not candles:
        return None
    today = now_ist().strftime("%Y-%m-%d")
    by_day = {}
    for c in candles:
        t = c.get("time")
        if t is None:
            continue
        d = datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")
        if d == today:
            continue
        by_day.setdefault(d, []).append(c)
    if not by_day:
        return None
    prev_date = max(by_day.keys())
    day_candles = by_day[prev_date]
    return {
        "date": prev_date,
        "high": max(c["high"] for c in day_candles),
        "low": min(c["low"] for c in day_candles),
        "close": day_candles[-1]["close"],
    }


def _source_label(source, side, oi, oi_chg):
    """Human-readable Source label, per explicit request ("Level Name
    / Current Distance / Strength / Source ... Source: Highest CE OI
    ... Source: Put Writing"). Reuses `oi`/`oi_chg` — already computed
    by analyzer.ranked_levels(), not a new lookup — to distinguish a
    level that's simply the highest-OI strike from one seeing FRESH
    writing activity this cycle (oi_chg is a large positive fraction
    of total oi), matching the example's own two distinct source
    phrasings for the same underlying oi_wall source type."""
    if source == "oi_wall":
        if oi and oi_chg and oi_chg > 0 and oi_chg / oi > 0.15:
            return "Fresh Call Writing" if side == "R" else "Fresh Put Writing"
        return "Highest CE OI" if side == "R" else "Highest PE OI"
    return {"prev_day_high": "Previous Day High",
           "prev_day_low": "Previous Day Low",
           "prev_day_close": "Previous Day Close",
           "vwap": "VWAP"}.get(source, source)


def merge_levels(oi_signal_lines, prev_day, vwap, spot):
    """Combine the existing OI-wall levels (analyzer.py's
    ranked_levels output — retained as-is, not recomputed here) with
    Previous Day and VWAP levels into a single source-tagged R1-R3/
    S1-S3 view. Deduplicates near-identical prices (within 0.1% of
    each other) since an OI wall sometimes lands very close to a
    prev-day high/low, which would otherwise show as two separate
    entries for effectively the same level.

    2026-07-25 — extended per explicit request for enhanced level
    labels ("Level Name / Current Distance / Strength / Source").
    Distance (points + %) computed here since `spot` is already a
    parameter; Source uses `_source_label()` above, reusing oi/oi_chg
    already present on oi_wall candidates rather than a new lookup."""
    oi_signal_lines = oi_signal_lines or {"R": [], "S": []}
    candidates_r, candidates_s = [], []

    for lvl in oi_signal_lines.get("R", []):
        candidates_r.append({"level": lvl["level"], "source": "oi_wall",
                            "strength": lvl.get("strength"),
                            "color": lvl.get("color"), "oi": lvl.get("oi"),
                            "oi_chg": lvl.get("oi_chg")})
    for lvl in oi_signal_lines.get("S", []):
        candidates_s.append({"level": lvl["level"], "source": "oi_wall",
                            "strength": lvl.get("strength"),
                            "color": lvl.get("color"), "oi": lvl.get("oi"),
                            "oi_chg": lvl.get("oi_chg")})

    if prev_day:
        for key, side in (("high", "R"), ("low", "S")):
            lst = candidates_r if side == "R" else candidates_s
            lst.append({"level": prev_day[key], "source": f"prev_day_{key}",
                       "strength": None, "color": None})
        # Previous close can act as either a support or resistance
        # depending on which side of it spot currently sits.
        pc = prev_day["close"]
        (candidates_r if pc > spot else candidates_s).append(
            {"level": pc, "source": "prev_day_close", "strength": None,
             "color": None})

    if vwap is not None:
        (candidates_r if vwap > spot else candidates_s).append(
            {"level": vwap, "source": "vwap", "strength": None, "color": None})

    def dedupe_and_rank(candidates, above):
        filtered = [c for c in candidates if
                   (c["level"] > spot if above else c["level"] < spot)]
        filtered.sort(key=lambda c: c["level"], reverse=not above)
        deduped = []
        for c in filtered:
            if deduped and abs(c["level"] - deduped[-1]["level"]) / spot < 0.001:
                continue   # within 0.1% of the last kept level — same level
            deduped.append(c)
        return deduped[:3]

    r_levels = dedupe_and_rank(candidates_r, above=True)
    s_levels = dedupe_and_rank(candidates_s, above=False)
    for i, lvl in enumerate(r_levels):
        lvl["label"] = f"R{i+1}"
        lvl["source_label"] = _source_label(lvl["source"], "R",
                                            lvl.get("oi"), lvl.get("oi_chg"))
        lvl["distance_pts"] = round(lvl["level"] - spot, 2) if spot else None
        lvl["distance_pct"] = round((lvl["level"] - spot) / spot * 100, 3) if spot else None
    for i, lvl in enumerate(s_levels):
        lvl["label"] = f"S{i+1}"
        lvl["source_label"] = _source_label(lvl["source"], "S",
                                            lvl.get("oi"), lvl.get("oi_chg"))
        lvl["distance_pts"] = round(lvl["level"] - spot, 2) if spot else None
        lvl["distance_pct"] = round((lvl["level"] - spot) / spot * 100, 3) if spot else None

    return {"R": r_levels, "S": s_levels,
           "unavailable": ["volume_profile", "price_acceptance"]}


def check_entry_criteria(direction, spot, levels):
    """Implements the spec's own entry-criteria/target-ladder framework:
    for a bullish trade, price should be above S1 (support holding),
    S2 is the emergency stop-loss zone, R1/R2/R3 are the profit-taking
    ladder. Bearish is the mirror. Returns a structured plan, or
    {"valid": False, "reason": ...} if the required levels aren't
    available yet (e.g. still early in the session before enough
    levels have formed)."""
    r = levels.get("R", [])
    s = levels.get("S", [])
    if direction == "bullish":
        if len(s) < 2 or len(r) < 1:
            return {"valid": False, "reason": "not enough levels formed yet "
                                              "(need S1/S2 and R1 minimum)"}
        s1, s2 = s[0]["level"], s[1]["level"]
        if spot <= s1:
            return {"valid": False, "reason": f"spot {spot} is not above "
                                              f"S1 {s1} — condition not met"}
        return {"valid": True, "direction": "bullish",
               "stop_loss_zone": s2, "invalidation_level": s1,
               "target1": r[0]["level"],
               "target2": r[1]["level"] if len(r) > 1 else None,
               "target3": r[2]["level"] if len(r) > 2 else None}
    elif direction == "bearish":
        if len(r) < 2 or len(s) < 1:
            return {"valid": False, "reason": "not enough levels formed yet "
                                              "(need R1/R2 and S1 minimum)"}
        r1, r2 = r[0]["level"], r[1]["level"]
        if spot >= r1:
            return {"valid": False, "reason": f"spot {spot} is not below "
                                              f"R1 {r1} — condition not met"}
        return {"valid": True, "direction": "bearish",
               "stop_loss_zone": r2, "invalidation_level": r1,
               "target1": s[0]["level"],
               "target2": s[1]["level"] if len(s) > 1 else None,
               "target3": s[2]["level"] if len(s) > 2 else None}
    return {"valid": False, "reason": f"unrecognized direction {direction!r}"}


def build_levels(analysis, spot, candles, symbol=None, future_vwap=None):
    """Single source of truth for the merged R1-R3/S1-S3 level set.

    Extracted 2026-07-26 so RegimeAgent._compute_levels() (which
    publishes `levels:{sym}` during market hours) and the chart
    websocket's on-demand fallback (which needs the SAME levels
    outside market hours, when RegimeAgent is idle) cannot drift
    apart. Previously the merge was inlined in RegimeAgent only, which
    is exactly why the chart's Key Levels panel sat on "Loading..."
    forever after hours while the option-chain-fed Key Levels ladder
    kept working -- the ladder reads TechnicalAgent's `analysis:{sym}`
    (not market-gated), the chart read RegimeAgent's bus key (gated).

    Every input is data this system already has:
      analysis     -- `analysis:{sym}` from TechnicalAgent (signal_lines,
                      i.e. analyzer.py's OI walls; primary S/R source)
      spot         -- `chain:{sym}`'s spot
      candles      -- any multi-day candle series, used ONLY as the
                      fallback path inside previous_day_levels(); the
                      DB-persisted daily_ohlc row wins when present, so
                      this may be empty outside market hours
      future_vwap  -- `future_ohlc:{sym}`'s vwap, optional

    Returns the merge_levels() dict, or None when there is genuinely
    nothing to build from (no spot) -- callers surface that as a real
    reason rather than an indefinite spinner.
    """
    if spot is None:
        return None
    prev_day = previous_day_levels(candles or [], symbol=symbol)
    return merge_levels((analysis or {}).get("signal_lines"), prev_day,
                        future_vwap, spot)
