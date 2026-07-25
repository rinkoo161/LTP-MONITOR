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

PA_NAMES = ("orb", "vwap_pullback", "ema_mtf")

PA_DEFAULTS = {
    "orb": {"or_minutes": 5, "buf_frac": 0.10, "min_or_range_pct": 0.08,
            "anchor_filter": 1, "max_trades_per_day": 3},
    "vwap_pullback": {"band_pct": 0.10, "trend_ema": 20, "resume_ema": 9,
                      "max_trades_per_day": 3},
    "ema_mtf": {"fast": 5, "slow": 13, "mtf_confirm": 1,
                "max_trades_per_day": 2},
}

# (lo, hi, relax_direction)  relax moves toward the permissive end
PA_BOUNDS = {
    "orb": {"buf_frac": (0.02, 0.25, -1), "min_or_range_pct": (0.02, 0.30, -1),
            "anchor_filter": (0, 1, -1)},
    "vwap_pullback": {"band_pct": (0.05, 0.35, +1)},
    "ema_mtf": {"mtf_confirm": (0, 1, -1)},
}

PA_META = {
    "orb": {"title": "Opening Range Breakout (5m + anchor)",
            "bias": "both directions"},
    "vwap_pullback": {"title": "Anchor Pullback (trend-following)",
                      "bias": "with the trend"},
    "ema_mtf": {"title": "5/13 EMA Cross (MTF confirmed)",
                "bias": "with the cross"},
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
        if touched and trending_up and closes[-2] < e_res_prev:
            return {"dir": +1, "entry_spot": spot,
                    "stop_spot": anchor - band * 2,
                    "t1_spot": spot + (spot - anchor) + band,
                    "t2_spot": spot + 2 * ((spot - anchor) + band),
                    "why": f"pullback to anchor {anchor:.0f}, resumed up"}
        if touched and trending_dn and closes[-2] > e_res_prev:
            return {"dir": -1, "entry_spot": spot,
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
    return None


def tune(name, params, direction):
    """One bounded relax (+1) or tighten (-1) step. Returns (new, changes)."""
    p = dict(params)
    changes = []
    for key, (lo, hi, relax_dir) in PA_BOUNDS.get(name, {}).items():
        step_dir = relax_dir * direction
        cur = p.get(key, PA_DEFAULTS[name][key])
        if key in ("anchor_filter", "mtf_confirm"):
            new = (0 if step_dir < 0 else 1)
        else:
            step = (hi - lo) * 0.25 * step_dir
            new = round(min(hi, max(lo, cur + step)), 3)
        if new != cur:
            p[key] = new
            changes.append(f"{key} {cur}->{new}")
    return p, changes
