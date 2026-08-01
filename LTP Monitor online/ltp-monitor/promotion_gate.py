"""promotion_gate.py — what a strategy must clear to be promoted to live.

v59.0 item 17. The shipped rule is a bare sign test:

    profitable_now = trades >= min_conf and net_pnl > 0

which promotes the error terms of two models. Those errors are different
kinds and do NOT combine by addition:

  COST error is BIAS.      The flat fee_per_lot understates a real round
                           trip by roughly ₹25-82 depending on symbol. It
                           is systematic and does NOT shrink with sample
                           size — 10,000 trades of an understated cost is
                           still an understated cost.

  P&L-proxy error is VARIANCE. `pts x 0.5 x lot` misses gamma, theta and
                           IV; measured sd ₹1,143/trade. This DOES shrink
                           with sample size, as sd/sqrt(n).

THE APPLIED REQUIREMENT (item 26, 2026-08-01):

    net_per_trade  >  cost_bias  +  k * sqrt(own_sd^2/n + sd^2/cal_n)

with k = 2, `own_sd` the strategy's own per-trade P&L dispersion, and
`sd`/`cal_n` the ₹1,143 measured on 74 trades.

The two terms are doing genuinely different jobs. `own_sd^2/n` is the
sampling error of the measured edge and shrinks as the strategy trades.
`sd^2/cal_n` is the uncertainty in the BIAS CORRECTION — set by the
calibration sample, not the strategy's own n, so it does NOT shrink. It
is a floor of about ₹133 (₹266 at k=2), and correctly so: no amount of
trading fixes a bias someone measured on four sessions.

SUPERSEDED: `cost_bias + k*sqrt(own_sd^2 + sd^2)/sqrt(n)`. That form
treated the model error as independent additive noise on top of the
recorded P&L. Since `err = real - proxy`, independence was never
possible, and it measured violated by 34% (corr(error, proxy P&L) =
-0.49). `own_sd` already contains the model error; adding it again
double-counted. Kept as `required_margin()` and reported as
`legacy_required` so the change is visible, never for a decision.

ONE FINDING, NOT TWO. With `own_sd` in it, this gate IS a t-test on the
edge with model uncertainty added. So "0 of 11 pass the gate" and "0 of
11 reach t = 2" are the SAME statement measured two ways — they do not
corroborate each other and must never be presented as two independent
confirmations. The t-stat is the more useful form publicly, because it
needs neither error model.

A caller with no `own_sd` is DENIED, not waved through on the model term
alone. This function gates live orders; "unmeasurable" must not read as
"passed".

WHY THE PROXY'S MEASURED MEAN (+₹102) IS NOT NETTED OUT. It was measured
on 74 trades across 4 sessions in one regime, and its sign is
duration-dependent: +₹341 under 30 minutes, -₹212 past 90. Applying one
mean to strategies with different hold profiles would replace a known
bias with an unmeasured one. The mean is evidence that bias EXISTS; the
sd is what we can defend as uncertainty. Only the sd propagates.

THE 1,143 IS PROVISIONAL. n=74, 4 sessions, one volatility regime,
measured against an archive that keeps 5 days. It is labelled provisional
everywhere it is stored or displayed and must not harden into a constant.
Re-measure once a longer premium archive exists.

WIRED, as of 2026-08-01, into `backtester.is_live_enabled()` — ANDed
with the pre-existing checks and failing CLOSED. It can only ever
withhold live permission, never grant it, and paper trading is untouched
because both call sites already sit behind `not paper_mode`.
"""
import math

# --- provisional, see module docstring. Do not promote to a constant. ---
PROXY_SD_PER_TRADE = 1143.0
PROXY_SD_PROVENANCE = ("provisional: n=74 trades, 4 sessions (2026-07-27..30), "
                       "one regime, measured against a 5-day chain archive")
DEFAULT_K = 2.0
CALIBRATION_N = 74      # trades the ₹1,143 was measured on

# Cost bias per symbol: real round-trip cost minus what fee_per_lot
# charges, single-leg, 1 lot. Computed from options_costs at the MEASURED
# median quoted spread (0.65 pts -> 0.325 half-spread), not an assumed one.
COST_BIAS_PROVENANCE = ("options_costs at measured median quoted spread "
                        "0.65 pts; the distribution is skewed (mean 2.27, "
                        "max 15.80) so this is a LOWER bound on the bias")


def cost_bias(symbol, cfg=None, halfspread=0.325, legs=1, premium_pct=0.005,
              spot=None):
    """Systematic per-trade understatement of the flat model, in rupees."""
    import config
    import options_costs as oc
    cfg = cfg if cfg is not None else config.load()
    SPOT = {"NIFTY": 24300, "BANKNIFTY": 57300, "FINNIFTY": 26300,
            "SENSEX": 80000}
    s = spot or SPOT.get((symbol or "").upper(), 24000)
    lot = (cfg.get("lot_sizes") or {}).get((symbol or "").upper(), 75)
    prem = s * premium_pct
    real = oc.cost_round_trip(prem, prem, lot, legs=legs, cfg=cfg,
                              halfspread=halfspread)["total"]
    flat = oc.flat_model_cost(legs=legs, cfg=cfg)
    return real - flat


def combined_sd(own_sd=None, sd=PROXY_SD_PER_TRADE):
    """Independent error sources add in quadrature."""
    if not own_sd:
        return float(sd)
    return math.sqrt(float(own_sd) ** 2 + float(sd) ** 2)


def required_margin(n, symbol=None, k=DEFAULT_K, sd=PROXY_SD_PER_TRADE,
                    cfg=None, bias=None, own_sd=None):
    """Net rupees per trade a strategy must exceed to be established."""
    if not n or n <= 0:
        return None
    b = cost_bias(symbol, cfg) if bias is None else float(bias)
    return b + k * combined_sd(own_sd, sd) / math.sqrt(n)


def own_sd_from(m):
    """The strategy's per-trade sd, real if recorded, approximated if not.

    `pnl_sd` is written by backtester.metrics() from v59.0 onward. Older
    persisted entries predate it, so fall back to the win/loss profile —
    but that approximation ignores dispersion INSIDE each bucket and is
    a LOWER bound, which makes the gate more permissive. The fallback is
    flagged so a caller can tell the two apart.
    """
    if not m:
        return None, "absent"
    if m.get("pnl_sd"):
        return float(m["pnl_sd"]), "measured"
    p = (m.get("win_rate") or 0) / 100.0
    aw, al = m.get("avg_win") or 0, m.get("avg_loss") or 0
    mu = p * aw + (1 - p) * al
    var = p * aw * aw + (1 - p) * al * al - mu * mu
    if var <= 0:
        return None, "absent"
    return var ** 0.5, "approximated (lower bound — gate runs permissive)"


def evaluate(name, symbol, trades, net_pnl, k=DEFAULT_K, cfg=None,
             own_sd=None, own_sd_source=None):
    """(passes, detail) under the CALIBRATED gate (item 26).

        required = cost_bias + k * sqrt(own_sd^2/n + sd^2/cal_n)

    Replaced the quadrature form on 2026-08-01. That form assumed the
    proxy error was independent additive noise; `err = real - proxy`
    makes independence impossible, and it was measured violated by 34%.
    See required_margin_calibrated() for the derivation.

    Stricter than the superseded form on all 11 live strategies, and the
    gate is veto-only and fails closed, so applying it can only ever
    withhold live permission — never grant it to something the old form
    refused.
    """
    if not trades:
        return False, {"reason": "no trades"}
    per = net_pnl / trades
    b = cost_bias(symbol, cfg)
    if not own_sd:
        # No dispersion figure means the sampling term cannot be formed.
        # Deny rather than fall back to the model term alone: this
        # function gates live orders, and "unmeasurable" must not read
        # as "passed".
        return False, {"strategy": name, "symbol": symbol, "trades": trades,
                       "net_pnl": net_pnl, "net_per_trade": per,
                       "reason": "no per-trade dispersion — cannot evaluate",
                       "own_sd": None, "own_sd_source": own_sd_source,
                       "cost_bias": b, "required": None, "headroom": None,
                       "passes_stat_only": False, "t_own": None,
                       "sd_provenance": PROXY_SD_PROVENANCE,
                       "cost_provenance": COST_BIAS_PROVENANCE}
    csd = combined_sd(own_sd)
    stat = k * math.sqrt(float(own_sd) ** 2 / trades
                         + PROXY_SD_PER_TRADE ** 2 / CALIBRATION_N)
    req = b + stat
    # The superseded quadrature form, kept only so the report can show
    # what changed. Never used for a decision.
    legacy = b + k * csd / math.sqrt(trades)
    # t of the edge against the strategy's own variance alone — needs
    # NEITHER error model, which is why it is the reportable headline.
    t = (per / (own_sd / math.sqrt(trades))) if own_sd else None
    return per > req, {
        "strategy": name, "symbol": symbol, "trades": trades,
        "net_pnl": net_pnl, "net_per_trade": per,
        "cost_bias": b, "stat_margin": stat, "required": req,
        "headroom": per - req,
        "passes_stat_only": per > stat,     # gate WITHOUT the cost correction
        "own_sd": own_sd, "own_sd_source": own_sd_source,
        "combined_sd": csd, "t_own": t,
        "margin_form": "calibrated", "legacy_required": legacy,
        "cal_n": CALIBRATION_N,
        "k": k, "sd": PROXY_SD_PER_TRADE,
        "sd_provenance": PROXY_SD_PROVENANCE,
        "cost_provenance": COST_BIAS_PROVENANCE,
    }


def required_margin_calibrated(n, own_sd, symbol=None, k=DEFAULT_K, cfg=None,
                               bias=None, sd=PROXY_SD_PER_TRADE,
                               cal_n=CALIBRATION_N):
    """The applied margin (item 26). Derivation:

    The applied gate adds `sd/sqrt(n)` in quadrature, which assumes the
    proxy error is INDEPENDENT additive noise on top of the recorded
    P&L. Item 24 measured that assumption and it is false:

        corr(error, proxy P&L) = -0.49
        Var(proxy) = 1,996,251  vs  Var(real)+Var(err) = 3,014,973

    — violated by 34%. The error is not independent of the P&L because
    both are driven by the same spot move. So `own_sd` ALREADY contains
    the model error; adding it again double-counts.

    What the ₹1,143 genuinely buys us is uncertainty in the BIAS
    CORRECTION: mean(real) = mean(proxy) + mean(error), and mean(error)
    was estimated from 74 trades, so it carries ±sd/sqrt(74) ≈ ₹133.
    That term is set by the CALIBRATION sample, not the strategy's own
    n, so it does not shrink as a strategy accumulates trades — it is a
    floor, and correctly so: no amount of trading fixes a bias you
    measured on four sessions.

        required = cost_bias + k * sqrt(own_sd^2/n + sd^2/cal_n)

    APPLIED as of 2026-08-01 (item 26). Stricter than the superseded
    quadrature form on all 11 live strategies.
    """
    if not n or n <= 0 or not own_sd:
        return None
    b = cost_bias(symbol, cfg) if bias is None else float(bias)
    return b + k * math.sqrt(float(own_sd) ** 2 / n + float(sd) ** 2 / cal_n)


def evaluate_entry(name, symbol, m, k=DEFAULT_K, cfg=None):
    """(passes, detail) straight from a persisted backtest results dict."""
    if not m or not m.get("trades"):
        return False, {"reason": "no trades"}
    osd, src = own_sd_from(m)
    return evaluate(name, symbol, m.get("trades") or 0, m.get("net_pnl") or 0,
                    k=k, cfg=cfg, own_sd=osd, own_sd_source=src)
