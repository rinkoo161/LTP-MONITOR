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

v59.66 (2026-08-09, third-eye review Tier 1) — three holes closed:

  1. k WAS 2.0 FOR A SINGLE TEST, BUT THE FAMILY RAN THOUSANDS. The
     tuner searches 71 free parameters and the trial count before
     2026-08-08 is unrecoverable; the strategy-reset memo pre-committed
     a floor of N=1000 trials. The bar for "best of N" is the expected
     MAXIMUM of N null draws, not 2. `deflation_k()` computes it from
     the recorded trial count with the pre-committed floor — the count
     can only ever RAISE the bar, never lower it below 3.255.

  2. n WAS THE TRADE COUNT, BUT TRADES SHARE DAYS. Same-day trades on
     one index are driven by one day's regime — 313 trades over 17 days
     is closer to 17 observations than 313. When the caller supplies
     day-level dispersion the sampling term uses the DAY as the
     observation unit, and fewer than `gate_min_days` distinct days is
     an automatic DENY: too few independent observations is not a pass
     with wide error bars, it is "cannot evaluate".

  3. THE NUMBERS WERE IN-SAMPLE. The tuner selected parameters on the
     same days the gate then scored. `evaluate_entry()` now scores ONLY
     the `oos` sub-metrics (days strictly after the active version's
     adoption — walk-forward, so selection bias is structurally
     excluded), and DENIES when no out-of-sample window exists yet. A
     freshly tuned version starts live-disabled and earns eligibility
     only by surviving days it was not fitted on.
"""
import math
import time

# --- provisional, see module docstring. Do not promote to a constant. ---
PROXY_SD_PER_TRADE = 1143.0
PROXY_SD_PROVENANCE = ("provisional: n=74 trades, 4 sessions (2026-07-27..30), "
                       "one regime, measured against a 5-day chain archive, "
                       "at lot sizes NIFTY 75 / BANKNIFTY 30 / FINNIFTY 65 / "
                       "SENSEX 20 (NIFTY and FINNIFTY have since been corrected "
                       "to 65 / 60, so this rupee figure is ~15% / ~8% high for "
                       "those symbols)")
# WHY THE LOT SIZE IS PART OF THE PROVENANCE (v59.0 item 42).
# ₹1,143 is an absolute rupee quantity, and rupees per trade scale with
# contract size. On 2026-08-01 the config lot map was corrected from
# NIFTY 75 / FINNIFTY 65 to 65 / 60, so "n=74, 4 sessions, one regime" no
# longer identifies the measurement — the same 74 trades re-measured
# today would produce a smaller number. Anything expressed in rupees has
# to name the contract size it was calibrated at, or it silently means
# something different after the next exchange revision.
#
# Deliberately NOT rescaled by hand. The gate's verdict is unaffected: t
# is scale-invariant because net and sd scale together, and `required`
# moved only 1-3%. Applying a scalar correction to a provisional constant
# would add a second unverified transform to a number already labelled
# provisional. Re-measure it properly when the chain archive has depth.
DEFAULT_K = 2.0         # single-test k; kept for the legacy/report forms.
                        # evaluate() no longer defaults to it — see deflation_k().
CALIBRATION_N = 74      # trades the ₹1,143 was measured on

# --- multiple-testing deflation (v59.66) --------------------------------
# The strategy-reset memo pre-committed N=1000 (hurdle 3.255) because the
# trial count before trial_log existed is unrecoverable. That floor is a
# commitment, not an estimate: the recorded count may RAISE the bar as
# trials accumulate past 1000, but nothing may lower it back below the
# pre-committed value — re-estimating a pre-registration downward after
# seeing results is exactly the move pre-registration exists to prevent.
PRECOMMITTED_TRIALS = 1000
PRECOMMITTED_K = 3.255
_K_CACHE = {"at": 0.0, "k": None}   # trial_log is a file read; cache 10 min


def expected_max_abs_t(n):
    """E[max |t|] of n independent null draws — asymptotic Gumbel form.

        sqrt(2 ln n) - (ln ln n + ln 4π) / (2 sqrt(2 ln n))

    The strategies are not independent (shared symbols, days, underlying
    moves), which makes the true benchmark somewhat LOWER — but by an
    amount this data cannot quantify, so the independent-draw figure is
    used as the defensible upper anchor and the pre-commit floor covers
    the rest."""
    n = max(2, int(n))
    a = math.sqrt(2.0 * math.log(n))
    return a - (math.log(math.log(n)) + math.log(4.0 * math.pi)) / (2.0 * a)


def deflation_k():
    """k for the applied gate: max(pre-committed 3.255, E[max|t|] of the
    recorded trial count). Monotone up, never down — see PRECOMMITTED_K."""
    if _K_CACHE["k"] is not None and time.time() - _K_CACHE["at"] < 600:
        return _K_CACHE["k"]
    n = 0
    try:
        import trial_log
        n = trial_log.count()
    except Exception:
        pass          # unreadable log ⇒ floor applies; the floor IS the fallback
    k = max(PRECOMMITTED_K,
            expected_max_abs_t(max(n, PRECOMMITTED_TRIALS)))
    _K_CACHE.update(at=time.time(), k=k)
    return k

# Cost bias per symbol: real round-trip cost minus what fee_per_lot
# charges, single-leg, 1 lot. Computed from options_costs at the MEASURED
# median quoted spread (0.65 pts -> 0.325 half-spread), not an assumed one.
COST_BIAS_PROVENANCE = ("options_costs at measured median quoted spread "
                        "0.65 pts; the distribution is skewed (mean 2.27, "
                        "max 15.80) so this is a LOWER bound on the bias")


def wilson_upper(wins, n, z=1.96):
    """95% Wilson-score UPPER bound on a win rate.

    v59.66 — for any rule that suppresses or promotes on a small-sample
    win rate. A raw `wins/n < 0.35` on n=5 fires on evidence consistent
    with a 43%+ true rate; requiring the upper CONFIDENCE bound below
    the threshold means the data actually rules the threshold out.
    Dependency-free by house style."""
    if not n:
        return 1.0
    ph = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = ph + z2 / (2.0 * n)
    margin = z * math.sqrt(ph * (1.0 - ph) / n + z2 / (4.0 * n * n))
    return (centre + margin) / denom


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


def _deny(reason, name=None, symbol=None, **extra):
    """A denial payload with the FULL key set, so gate_report and the
    dashboard can render any verdict without KeyError — the 0-trade
    crash in gate_report (third-eye Tier 1) came from denial dicts that
    carried fewer keys than passing ones."""
    d = {"strategy": name, "symbol": symbol, "trades": None, "net_pnl": None,
         "net_per_trade": None, "reason": reason, "own_sd": None,
         "own_sd_source": None, "cost_bias": None, "stat_margin": None,
         "required": None, "headroom": None, "passes_stat_only": False,
         "t_own": None, "t_day": None, "n_days": None, "day_sd": None,
         "clustering": None, "window": None, "k": None,
         "sd_provenance": PROXY_SD_PROVENANCE,
         "cost_provenance": COST_BIAS_PROVENANCE}
    d.update(extra)
    return False, d


def evaluate(name, symbol, trades, net_pnl, k=None, cfg=None,
             own_sd=None, own_sd_source=None,
             n_days=None, day_sd=None, window=None):
    """(passes, detail) under the CALIBRATED gate (item 26 + v59.66).

        required = cost_bias + k * sqrt(SE_edge^2 + sd^2/cal_n)

    where SE_edge is the standard error of the per-trade edge:

      day-clustered (n_days & day_sd supplied — the production path):
          SE_edge = day_sd * sqrt(n_days) / trades
        The DAY is the observation unit. Same-day trades on one index
        share one day's regime; counting them as independent draws was
        how 313 trades masqueraded as 313 observations (Tier 1, 2026-08-09).
        Requires n_days >= cfg gate_min_days, else DENY.

      per-trade (day fields absent):
          SE_edge = own_sd / sqrt(trades)
        Kept for callers that assert their trades are independent
        (tests, ad-hoc analysis). evaluate_entry() — the path that
        gates real orders — always supplies the day fields or denies.

    k defaults to deflation_k(): the expected max |t| of the searched
    family under the null, floored at the pre-committed 3.255. Passing
    k explicitly is for reports that show what a different bar implies —
    never for a live decision.

    Replaced the quadrature form on 2026-08-01 (see
    required_margin_calibrated for the derivation); veto-only, fails
    closed, so tightening it can only ever withhold live permission.
    """
    if not trades:
        return _deny("no trades", name, symbol)
    if k is None:
        k = deflation_k()
        k_source = "deflated max-of-N (trial_log, pre-commit floor 3.255)"
    else:
        k_source = "caller-specified"
    per = net_pnl / trades
    b = cost_bias(symbol, cfg)
    if not own_sd:
        # No dispersion figure means the sampling term cannot be formed.
        # Deny rather than fall back to the model term alone: this
        # function gates live orders, and "unmeasurable" must not read
        # as "passed".
        return _deny("no per-trade dispersion — cannot evaluate",
                     name, symbol, trades=trades, net_pnl=net_pnl,
                     net_per_trade=per, own_sd_source=own_sd_source,
                     cost_bias=b, k=k)
    if n_days is not None:
        import config as _config
        min_days = int((cfg if cfg is not None else _config.load())
                       .get("gate_min_days", 10))
        if day_sd is None:
            return _deny("day-clustered dispersion missing (pnl_sd_day) — "
                         "cannot form the independent-observation term",
                         name, symbol, trades=trades, net_pnl=net_pnl,
                         net_per_trade=per, n_days=n_days, cost_bias=b, k=k,
                         window=window)
        if n_days < min_days:
            return _deny(f"only {n_days} independent day(s) in the window — "
                         f"gate needs {min_days} (gate_min_days). Too few "
                         f"observations is 'cannot evaluate', not a pass",
                         name, symbol, trades=trades, net_pnl=net_pnl,
                         net_per_trade=per, n_days=n_days, day_sd=day_sd,
                         cost_bias=b, k=k, window=window)
        # SE of the per-trade mean with days as the unit: net = Σ daily,
        # Var(net) = n_days·day_sd², so SE(net/trades) = day_sd·√n_days/trades.
        se_edge = float(day_sd) * math.sqrt(n_days) / trades
        clustering = f"day-clustered ({n_days} days)"
    else:
        se_edge = float(own_sd) / math.sqrt(trades)
        clustering = "none — caller asserts independent trades"
    csd = combined_sd(own_sd)
    stat = k * math.sqrt(se_edge ** 2
                         + PROXY_SD_PER_TRADE ** 2 / CALIBRATION_N)
    req = b + stat
    # The superseded quadrature form, kept only so the report can show
    # what changed. Never used for a decision.
    legacy = b + k * csd / math.sqrt(trades)
    # t of the edge against the strategy's own variance alone — needs
    # NEITHER error model, which is why it is the reportable headline.
    # t_day is the clustered version and is the honest one of the two.
    t = (per / (own_sd / math.sqrt(trades))) if own_sd else None
    t_day = ((net_pnl / n_days) / (day_sd / math.sqrt(n_days))
             if (n_days and day_sd) else None)
    return per > req, {
        "strategy": name, "symbol": symbol, "trades": trades,
        "net_pnl": net_pnl, "net_per_trade": per,
        "cost_bias": b, "stat_margin": stat, "required": req,
        "headroom": per - req,
        "passes_stat_only": per > stat,     # gate WITHOUT the cost correction
        "own_sd": own_sd, "own_sd_source": own_sd_source,
        "combined_sd": csd, "t_own": t, "t_day": t_day,
        "n_days": n_days, "day_sd": day_sd, "clustering": clustering,
        "window": window,
        "margin_form": "calibrated", "legacy_required": legacy,
        "cal_n": CALIBRATION_N,
        "k": k, "k_source": k_source, "sd": PROXY_SD_PER_TRADE,
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


def evaluate_entry(name, symbol, m, k=None, cfg=None):
    """(passes, detail) straight from a persisted backtest results dict.

    v59.66 — scores ONLY the `oos` sub-metrics: trades from days strictly
    after the active version's adoption date, attached by
    backtester.run_all(). The tuner selects parameters on the archive it
    can see, so the full-sample numbers are the in-sample optimum of a
    71-parameter search — the gate must never read them. Walk-forward by
    adoption date excludes selection bias structurally: a version can
    only score on days that did not exist when it was chosen.

    A results dict with no `oos` window (pre-v59.66 entry, or a candidate
    adopted today) is DENIED, not scored on the full sample. Fail-closed
    is the established direction for this gate; the window fills in as
    the daily backtest refreshes results.
    """
    if not m or not m.get("trades"):
        return _deny("no trades", name, symbol)
    oos = m.get("oos") if isinstance(m.get("oos"), dict) else None
    if oos is None:
        return _deny("results carry no out-of-sample window — pre-v59.66 "
                     "entry; the next daily backtest attaches it",
                     name, symbol, trades=m.get("trades"),
                     net_pnl=m.get("net_pnl"))
    if not oos.get("trades"):
        return _deny(f"0 out-of-sample trades ({oos.get('window') or 'window empty'}) "
                     "— the gate only scores days the active parameters "
                     "were not fitted on",
                     name, symbol, trades=m.get("trades"),
                     net_pnl=m.get("net_pnl"), window=oos.get("window"))
    osd, src = own_sd_from(oos)
    return evaluate(name, symbol, oos.get("trades") or 0,
                    oos.get("net_pnl") or 0,
                    k=k, cfg=cfg, own_sd=osd, own_sd_source=src,
                    n_days=oos.get("days_tested"),
                    day_sd=oos.get("pnl_sd_day"),
                    window=oos.get("window"))
