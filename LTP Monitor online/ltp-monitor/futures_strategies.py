"""futures_strategies.py — S11/S12/S13/S14 as CAUSAL bar-by-bar evaluators.

v59.0 Phase A §4.2.

The interface is the point. Every strategy is a function

    decide(bars, i, state, params) -> None | {"side", "stop", "target", "why"}

that may look at `bars[:i+1]` and nothing else. Lookahead is not
prevented by review or by a test alone — it is prevented by never handing
the future to the function. The prefix-invariance test then verifies the
property holds end to end, but the design makes the common mistake
impossible rather than detectable.

`bars` are 1-minute OHLCV dicts for ONE session, ascending.

MODELLING NOTE, stated because it changes what these results mean:
futures candles were never archived (only their volume, and only for 5
sessions), so the replay drives these strategies with INDEX candles as a
futures proxy. Intraday, the basis is near-constant, so returns and
signal geometry are faithful; absolute levels are not, and notional-based
costs carry a ~0.5-1% error from the missing basis. That is disclosed in
every result rather than buried.
"""

# --------------------------------------------------------------- helpers

def _sess_minute(bar):
    """Minutes since 09:15 for a bar, from its own timestamp."""
    import datetime as dt
    t = dt.datetime.fromtimestamp(bar["ts"])
    return (t.hour - 9) * 60 + (t.minute - 15)


def _hhmm_to_min(s):
    try:
        h, m = str(s).split(":")
        return (int(h) - 9) * 60 + (int(m) - 15)
    except Exception:
        return None


def _vwap(bars, upto):
    """Session VWAP through `upto` inclusive. Returns None when volume is
    absent — never silently substitutes an unweighted mean, because that
    is a different indicator wearing the same name."""
    num = den = 0.0
    for b in bars[:upto + 1]:
        v = b.get("v")
        if not v:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        num += tp * v
        den += v
    return (num / den) if den else None


def _stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5


# ------------------------------------------------------------------- S11

S11_DEFAULTS = {
    "fim_open_window_min": 30,
    "fim_min_abs_open_ret_pct": 0.15,
    "fim_entry_time": "14:45",
    "fim_exit_time": "15:25",
    "fim_sl_pct": 0.35,
}
S11_BOUNDS = {
    "fim_open_window_min": (15, 60),
    "fim_min_abs_open_ret_pct": (0.05, 0.50),
    "fim_sl_pct": (0.15, 0.80),
}


def s11_decide(bars, i, state, p):
    """Intraday Momentum: the opening window's direction, re-expressed
    late in the session and closed before the bell.

    The strongest evidence base of the four and the fewest parameters,
    which is why the spec puts it first.
    """
    if state.get("done"):
        return None
    entry_min = _hhmm_to_min(p.get("fim_entry_time", "14:45"))
    if entry_min is None or _sess_minute(bars[i]) < entry_min:
        return None
    w = int(p.get("fim_open_window_min", 30))
    if i < w:
        return None
    open_px = bars[0]["o"]
    window_close = None
    for b in bars[:i + 1]:
        if _sess_minute(b) >= w:
            break
        window_close = b["c"]
    if not open_px or window_close is None:
        return None
    ret_pct = (window_close - open_px) / open_px * 100
    if abs(ret_pct) < float(p.get("fim_min_abs_open_ret_pct", 0.15)):
        return None
    side = "LONG" if ret_pct > 0 else "SHORT"
    px = bars[i]["c"]
    sl_pct = float(p.get("fim_sl_pct", 0.35))
    sign = 1 if side == "LONG" else -1
    state["done"] = True
    return {"side": side,
            "stop": (px * (1 - sign * sl_pct / 100)) if sl_pct else None,
            "target": None,                       # exits on time, by design
            "exit_at_min": _hhmm_to_min(p.get("fim_exit_time", "15:25")),
            "why": f"open-window {ret_pct:+.2f}% over {w}m"}


# ------------------------------------------------------------------- S12

S12_DEFAULTS = {
    "fvr_band_sigma": 2.0,
    "fvr_sl_pct": 0.30,
    "fvr_max_trades_per_day": 3,
    "fvr_daily_loss_cap_pct": 1.5,
    "fvr_regime_required": "rangebound",
}
S12_BOUNDS = {
    "fvr_band_sigma": (1.5, 3.0),
    "fvr_sl_pct": (0.15, 0.60),
    "fvr_max_trades_per_day": (1, 6),
    "fvr_daily_loss_cap_pct": (0.5, 3.0),
}


def s12_decide(bars, i, state, p):
    """VWAP mean reversion — enter against a stretch from session VWAP,
    target VWAP itself, hard stop beyond.

    REQUIRES REAL VOLUME. `_vwap` returns None when volume is absent and
    this returns None rather than falling back to a typical-price mean:
    an unweighted average is a different indicator, and dressing it as
    VWAP would produce a result that looks like an answer and is not.
    """
    if state.get("trades_today", 0) >= int(p.get("fvr_max_trades_per_day", 3)):
        return None
    if state.get("day_stopped"):
        return None
    if i < 20:
        return None
    vw = _vwap(bars, i)
    if vw is None:
        state["no_volume"] = True
        return None
    devs = []
    for k in range(max(0, i - 60), i + 1):
        v = _vwap(bars, k)
        if v is not None:
            devs.append(bars[k]["c"] - v)
    sd = _stdev(devs)
    if sd <= 0:
        return None
    px = bars[i]["c"]
    z = (px - vw) / sd
    band = float(p.get("fvr_band_sigma", 2.0))
    if abs(z) < band:
        return None
    side = "SHORT" if z > 0 else "LONG"          # revert toward VWAP
    sign = 1 if side == "LONG" else -1
    sl_pct = float(p.get("fvr_sl_pct", 0.30))
    state["trades_today"] = state.get("trades_today", 0) + 1
    return {"side": side,
            "stop": px * (1 - sign * sl_pct / 100),
            "target": vw,                        # the mean it reverts to
            "why": f"{z:+.2f} sigma from VWAP {vw:.1f}"}


# ------------------------------------------------------------------- S13

S13_DEFAULTS = {
    "forb_or_minutes": 5,
    "forb_buf_frac": 0.10,
    "forb_min_or_range_pct": 0.08,
    "forb_min_breakout_vol_mult": 1.5,
    "forb_sl_at_or_opposite": 1,
    "forb_target_r": 2.0,
    "forb_max_trades_per_day": 1,
}
S13_BOUNDS = {
    "forb_or_minutes": (3, 30),
    "forb_buf_frac": (0.02, 0.50),
    "forb_min_or_range_pct": (0.02, 0.40),
    "forb_min_breakout_vol_mult": (1.0, 3.0),
    "forb_target_r": (1.0, 4.0),
}


def s13_decide(bars, i, state, p):
    """ORB, ported from S2 with a breakout-volume gate added.

    The OR-range minimum filter is KEPT deliberately: only ~30% of
    sessions are genuine trend days, and ORB without a volatility gate is
    a coin flip. `require_volume` lets the caller evaluate the price
    logic on sessions where volume is unavailable rather than silently
    passing a gate that was never tested.
    """
    if state.get("trades_today", 0) >= int(p.get("forb_max_trades_per_day", 1)):
        return None
    orm = int(p.get("forb_or_minutes", 5))
    if _sess_minute(bars[i]) <= orm:
        return None
    or_bars = [b for b in bars[:i + 1] if _sess_minute(b) < orm]
    if len(or_bars) < 2:
        return None
    hi = max(b["h"] for b in or_bars)
    lo = min(b["l"] for b in or_bars)
    rng = hi - lo
    if rng <= 0:
        return None
    ref = or_bars[0]["o"] or bars[0]["o"]
    if (rng / ref * 100) < float(p.get("forb_min_or_range_pct", 0.08)):
        return None                               # dead open, no energy
    buf = rng * float(p.get("forb_buf_frac", 0.10))
    c = bars[i]["c"]
    if c > hi + buf:
        side, stop = "LONG", lo
    elif c < lo - buf:
        side, stop = "SHORT", hi
    else:
        return None
    mult = float(p.get("forb_min_breakout_vol_mult", 1.5))
    if state.get("require_volume") and mult > 1.0:
        vols = [b.get("v") or 0 for b in bars[:i + 1]]
        if not any(vols):
            state["no_volume"] = True
            return None
        avg = sum(vols) / max(len(vols), 1)
        if avg <= 0 or (bars[i].get("v") or 0) < mult * avg:
            return None
    state["trades_today"] = state.get("trades_today", 0) + 1
    r = abs(c - stop)
    sign = 1 if side == "LONG" else -1
    return {"side": side, "stop": stop,
            "target": c + sign * r * float(p.get("forb_target_r", 2.0)),
            "why": f"OR {lo:.1f}-{hi:.1f} ({rng/ref*100:.2f}%) broken by {buf:.1f}"}


# ------------------------------------------------------------------- S14

S14_DEFAULTS = {
    "s14_stop_mult": 1.5,
    "s14_target_mult": 2.75,
    "s14_atr_period": 14,
    "s14_entry_min": 30,
    "s14_max_trades_per_day": 1,
}
S14_BOUNDS = {
    "s14_stop_mult": (0.5, 3.0),
    "s14_target_mult": (1.0, 5.0),
    "s14_atr_period": (5, 30),
}


def _atr(bars, i, n):
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        prev = bars[k - 1]["c"] if k else bars[k]["c"]
        trs.append(max(bars[k]["h"] - bars[k]["l"],
                       abs(bars[k]["h"] - prev), abs(bars[k]["l"] - prev)))
    return sum(trs) / len(trs) if trs else None


def s14_decide(bars, i, state, p):
    """The EXISTING engine's shape, re-tested with working ATR geometry.

    Phase 0 found the live engine never used its ATR geometry at all —
    `enter_future()` silently fell back to fixed percentages whenever ATR
    was unavailable, and did so on all 40 trades, giving a 0.8% target
    that nothing reached. This runs the same trend-following idea with
    the ATR geometry actually engaged, which is the only way to tell a
    bad edge from a broken exit.
    """
    if state.get("trades_today", 0) >= int(p.get("s14_max_trades_per_day", 1)):
        return None
    if _sess_minute(bars[i]) < int(p.get("s14_entry_min", 30)):
        return None
    n = int(p.get("s14_atr_period", 14))
    a = _atr(bars, i, n)
    if not a:
        return None
    closes = [b["c"] for b in bars[max(0, i - 20):i + 1]]
    if len(closes) < 10:
        return None
    fast = sum(closes[-5:]) / 5
    slow = sum(closes) / len(closes)
    if fast == slow:
        return None
    side = "LONG" if fast > slow else "SHORT"
    sign = 1 if side == "LONG" else -1
    px = bars[i]["c"]
    state["trades_today"] = state.get("trades_today", 0) + 1
    return {"side": side,
            "stop": px - sign * a * float(p.get("s14_stop_mult", 1.5)),
            "target": px + sign * a * float(p.get("s14_target_mult", 2.75)),
            "why": f"trend {side.lower()}, ATR {a:.1f}"}


STRATEGIES = {
    "s11_momentum": (s11_decide, S11_DEFAULTS, S11_BOUNDS),
    "s12_vwap_reversion": (s12_decide, S12_DEFAULTS, S12_BOUNDS),
    "s13_orb": (s13_decide, S13_DEFAULTS, S13_BOUNDS),
    "s14_existing": (s14_decide, S14_DEFAULTS, S14_BOUNDS),
}


def defaults_for(name):
    return dict(STRATEGIES[name][1])


def clamp(name, params):
    """Read-time clamping, per the standing rule. On a 15x-levered
    instrument a parameter validated only on write is not validated."""
    _, defaults, bounds = STRATEGIES[name]
    out = dict(defaults)
    for k, v in (params or {}).items():
        if k in bounds:
            lo, hi = bounds[k]
            try:
                v = max(lo, min(hi, type(lo)(v)))
            except (TypeError, ValueError):
                v = defaults[k]
        out[k] = v
    return out
