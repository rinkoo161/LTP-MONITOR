"""basis_residual.py — futures basis as an institutional-positioning signal.

v59.0 Phase B §5. The dashboard already shows raw basis. Raw basis is
mostly cost-of-carry: it widens with time to expiry and shrinks toward
zero at expiry, so watching it tells you the calendar, not the
positioning. What carries information is the part carry does NOT explain:

    fair_basis = spot x (r - q) x (days_to_expiry / 365)
    residual   = actual_basis - fair_basis
    residual_z = (residual - rolling_mean) / rolling_std

Reading, per the spec:
  sustained positive z            aggressive long futures positioning
  sharp compression in an up-move longs unwinding into strength
  sustained negative z            short build-up or hedging demand

TWO THINGS THIS MODULE REFUSES TO DO, both because the failure would be
silent:

1. It never substitutes q = 0. NIFTY ex-dates cluster Feb-Aug; a zero
   dividend assumption inflates fair basis in exactly those months and
   biases the residual the same way every year. With no dividend
   calendar in this repo, the configured estimate is used and the payload
   is stamped `approx=True` — visible in the API and rendered as an amber
   dot, never hidden.

2. It never emits z = 0 on a cold start. Fewer bars than the window
   means "not enough history", which is a different statement from "the
   residual is exactly average", and a chart cannot tell them apart once
   both are the number 0. `z` is None until the window fills.

DATA REALITY: futures candles were never archived (Phase A), so there is
no historical basis to backfill. This series builds FORWARD from the
first live cycle, exactly as the futures OI archive did in v58.66 — and
therefore has no z-score at all until `fut_residual_z_window` bars have
accumulated. Stated here so an empty panel is read as "not yet", not as
"broken".
"""
import math

import config

BOUNDS = {
    "fut_financing_rate_pct": (3.0, 12.0),
    "fut_dividend_yield_pct": (0.0, 4.0),
    "fut_residual_z_window": (50, 1000),
}
DEFAULTS = {
    "fut_financing_rate_pct": 6.5,
    "fut_dividend_yield_pct": 1.2,
    "fut_residual_z_window": 200,
}


def param(name, cfg=None):
    """Clamped on READ, per the standing rule."""
    lo, hi = BOUNDS[name]
    cfg = cfg if cfg is not None else config.load()
    try:
        v = float(cfg.get(name, DEFAULTS[name]))
    except (TypeError, ValueError):
        v = DEFAULTS[name]
    v = max(lo, min(hi, v))
    return int(v) if name == "fut_residual_z_window" else v


def dividend_yield(symbol, days_to_expiry, cfg=None):
    """(q_pct, approx) — dividend yield over the REMAINING contract life.

    Returns approx=True whenever the figure is the configured estimate
    rather than a real calendar. There is no dividend calendar in this
    repo today, so approx is True in practice; the shape is here so a
    calendar can be dropped in without changing any caller.
    """
    cfg = cfg if cfg is not None else config.load()
    cal = cfg.get("index_dividend_calendar") or {}
    entry = cal.get((symbol or "").upper())
    if entry:
        try:
            return float(entry), False
        except (TypeError, ValueError):
            pass
    return param("fut_dividend_yield_pct", cfg), True


def fair_basis(spot, days_to_expiry, symbol=None, cfg=None, r_pct=None,
               q_pct=None):
    """Cost-of-carry fair basis in POINTS.

    fair = spot x (r - q) x (T/365), with r and q as percentages.
    """
    cfg = cfg if cfg is not None else config.load()
    r = param("fut_financing_rate_pct", cfg) if r_pct is None else float(r_pct)
    if q_pct is None:
        q, approx = dividend_yield(symbol, days_to_expiry, cfg)
    else:
        q, approx = float(q_pct), False
    spot = float(spot)
    t = max(0.0, float(days_to_expiry)) / 365.0
    return spot * ((r - q) / 100.0) * t, {"r_pct": r, "q_pct": q,
                                          "approx": approx, "years": t}


def compute(symbol, spot, future, days_to_expiry, history_residuals=None,
            cfg=None):
    """One observation: actual basis, fair basis, residual and z.

    `history_residuals` is the prior residual series, oldest first. z is
    None until it holds at least `fut_residual_z_window` values — see the
    module docstring on why a cold start must not read as zero.
    """
    cfg = cfg if cfg is not None else config.load()
    actual = float(future) - float(spot)
    fair, meta = fair_basis(spot, days_to_expiry, symbol, cfg)
    residual = actual - fair
    window = param("fut_residual_z_window", cfg)
    hist = list(history_residuals or [])[-window:]
    z = None
    mean = sd = None
    if len(hist) >= window:
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
        sd = math.sqrt(var)
        # A flat series has no dispersion; dividing by it would produce
        # inf/NaN, and clamping to 0 would claim "exactly average" from
        # data that cannot support the claim.
        z = ((residual - mean) / sd) if sd > 0 else None
    return {
        "symbol": (symbol or "").upper(),
        "spot": float(spot), "future": float(future),
        "actual_basis": actual, "fair_basis": fair, "residual": residual,
        "residual_z": z, "z_window": window, "z_samples": len(hist),
        "z_ready": z is not None,
        "mean": mean, "stdev": sd,
        "days_to_expiry": float(days_to_expiry),
        "r_pct": meta["r_pct"], "q_pct": meta["q_pct"],
        "approx": meta["approx"],
    }


def reading(z):
    """Plain-language interpretation, or None when there is no z yet."""
    if z is None:
        return None
    if z >= 1.5:
        return "sustained premium — aggressive long futures positioning"
    if z <= -1.5:
        return "sustained discount — short build-up or hedging demand"
    return "within normal carry"


# --------------------------------------------------------------- the gate

def agrees(side, z, threshold=1.5):
    """Does the residual agree with a proposed direction?

    VETO ONLY. Returns True when there is no opinion — no z yet, or the
    residual is inside the band — so this can never be the reason a trade
    happens, only a reason one does not. It must never bypass an existing
    risk gate; callers consult it in ADDITION to their own checks.
    """
    if z is None:
        return True                     # no opinion: cannot veto
    if side == "LONG":
        return z > -threshold           # veto longs into a sustained discount
    if side == "SHORT":
        return z < threshold            # veto shorts into a sustained premium
    return True


def gate_for(strategy_key, side, z, cfg=None):
    """(allowed, why) honouring `<strategy>_require_basis_agreement`.

    Default OFF for every strategy: this ships as an observation first.
    """
    cfg = cfg if cfg is not None else config.load()
    # A per-strategy key wins; the global one covers everything else, so
    # "optional gate to ALL strategies" does not mean a config key per
    # strategy in perpetuity.
    key = f"{strategy_key}_require_basis_agreement"
    on = cfg.get(key)
    if on is None:
        on = cfg.get("require_basis_agreement", False)
    if not on:
        return True, f"{key} off"
    if z is None:
        return True, "no residual z yet (cold start) — cannot veto"
    ok = agrees(side, z)
    return ok, (f"basis residual z {z:+.2f} "
                f"{'agrees with' if ok else 'VETOES'} {side}")
