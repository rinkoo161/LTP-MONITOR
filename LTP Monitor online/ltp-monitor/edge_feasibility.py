"""edge_feasibility.py — Tier 2's structural gate, as code.

v59.73, third-eye review Tier 2 (2026-08-09). The economics finding was
never a bug: on the restated record, spread costs run 2.2× the gross
edge (₹398 vs ₹184/trade) and the August window grossed ₹158/trade
against ₹176 of friction, at a 12-minute median hold. No parameter
tuning rescues a trade whose DESIGNED payoff cannot clear its own
round-trip cost — the counterparty for edges that small is exchange
infrastructure, and it always wins.

What code can enforce is the reference's structural-feasibility rule:

    a trade is admissible only when its DESIGNED gross edge — the
    profit its own target would deliver — is at least
    `min_edge_cost_ratio` (default 2×) the modelled round-trip cost.

This does not create an edge. It stops the system paying to express
edges it cannot possibly keep, which mechanically pushes the strategy
mix toward fewer, larger-edge, longer-hold trades — the only shape the
Tier 2 arithmetic admits.

Design rules:
  * ONE definition, imported by the live gates (RiskAgent, enter_future,
    strategies.evaluate) AND the replays — the admission bar must be
    identical in backtest and live or the backtest measures a different
    strategy (the 2026-08-06 spread-exit lesson, entry-side).
  * Leaf module: imports only config and the two cost models. agents/
    backtester/strategies import it; it imports none of them back.
  * Feasibility uses the DESIGNED edge (target geometry known at entry),
    never a realised or predicted P&L — no forecasting is smuggled in.
  * 1-lot basis for options/spreads: brokerage is per order, so the
    ratio only IMPROVES with size; gating at 1 lot is the conservative
    bound and keeps the bar independent of the sizing decision.
"""
import config as _config
import options_costs as _oc


def min_ratio(cfg=None):
    cfg = cfg if cfg is not None else _config.load()
    try:
        v = float(cfg.get("min_edge_cost_ratio", 2.0))
    except (TypeError, ValueError):
        v = 2.0
    return max(0.0, v)


def feasible(designed_gross, round_trip_cost, cfg=None):
    """(ok, ratio). ok is False when the cost itself is unknown (<= 0):
    an unpriceable trade must not pass a cost gate."""
    if not round_trip_cost or round_trip_cost <= 0:
        return False, 0.0
    ratio = float(designed_gross or 0) / float(round_trip_cost)
    return ratio >= min_ratio(cfg), ratio


def spread_feasible(credit, capture_frac, lot, cfg=None, legs=2):
    """(ok, human_detail) for a credit spread whose exit design captures
    `capture_frac` of the credit. Designed gross = credit × capture ×
    lot; cost = the notional round trip from entry credit to the exit
    value that capture implies."""
    cfg = cfg if cfg is not None else _config.load()
    capture_frac = max(0.0, min(1.0, float(capture_frac or 0)))
    designed = float(credit) * capture_frac * int(lot)
    try:
        cost = _oc.cost_round_trip(
            float(credit), float(credit) * (1.0 - capture_frac),
            int(lot), legs=legs, cfg=cfg)["total"]
    except Exception:
        return False, ("edge gate: cost model unavailable — trade "
                       "unpriceable, refused")
    ok, ratio = feasible(designed, cost, cfg)
    detail = (f"designed edge ₹{designed:.0f} vs round-trip cost "
              f"₹{cost:.0f} — {ratio:.1f}x "
              f"({'clears' if ok else 'BELOW'} the "
              f"{min_ratio(cfg):.1f}x feasibility bar)")
    return ok, detail


def option_buy_feasible(entry_premium, target_premium, lot, cfg=None):
    """(ok, human_detail) for a long option whose first target is
    `target_premium`. Designed gross = (target − entry) × lot."""
    cfg = cfg if cfg is not None else _config.load()
    designed = (float(target_premium or 0) - float(entry_premium or 0)) \
        * int(lot)
    try:
        cost = _oc.cost_round_trip(float(entry_premium or 0),
                                   float(target_premium or 0),
                                   int(lot), legs=1, cfg=cfg)["total"]
    except Exception:
        return False, ("edge gate: cost model unavailable — trade "
                       "unpriceable, refused")
    ok, ratio = feasible(designed, cost, cfg)
    detail = (f"designed edge ₹{designed:.0f} (T1) vs round-trip cost "
              f"₹{cost:.0f} — {ratio:.1f}x "
              f"({'clears' if ok else 'BELOW'} the "
              f"{min_ratio(cfg):.1f}x feasibility bar)")
    return ok, detail


def future_feasible(symbol, entry, target, lot_size, lots, cfg=None):
    """(ok, human_detail) for a futures position: designed gross =
    |target − entry| × qty vs the notional round trip entry→target."""
    cfg = cfg if cfg is not None else _config.load()
    import futures_costs as _fc
    qty = int(lot_size) * max(1, int(lots or 1))
    designed = abs(float(target or 0) - float(entry or 0)) * qty
    try:
        b = _fc.breakdown(symbol, float(entry), float(target),
                          lots=max(1, int(lots or 1)), cfg=cfg,
                          lot=int(lot_size))
        cost = b["statutory_rupees"] + b["items"]["slippage"]
    except Exception:
        return False, ("edge gate: futures cost model unavailable — "
                       "trade unpriceable, refused")
    ok, ratio = feasible(designed, cost, cfg)
    detail = (f"designed edge ₹{designed:.0f} vs round-trip cost "
              f"₹{cost:.0f} — {ratio:.1f}x "
              f"({'clears' if ok else 'BELOW'} the "
              f"{min_ratio(cfg):.1f}x feasibility bar)")
    return ok, detail


def target_reachable(entry, target1, cfg=None):
    """(ok, human_detail) — is target1 close enough to actually be hit?

    v59.86. The existing feasibility gate above asks whether the
    designed edge clears its COSTS. This asks the other structural
    question, which nothing was asking: can the target be REACHED?

    `analyzer.option_stop_geometry` builds target1 as
    `entry × (1 + stop_pct × 2)`, so target distance is welded to stop
    width — a wider stop mechanically buys a more distant target. The
    consequence, measured over 534 resolved shadow signals (all four
    symbols, five signal sources, RR median 2.00 in every bucket so the
    hit rates are directly comparable):

        move needed to reach T1     n    hit T1 first   E[R]
        <20%                      215       47.0%      +0.307
        20-40%                    144       22.9%      -0.398
        40-80%                    120       30.0%      -0.110
        >80%                       55       30.9%      +0.033

    The median signal needed 28.6%, i.e. the worst bucket. Survives
    three falsification checks: RR is constant across buckets, the
    effect holds independently in BOTH halves of the sample (first
    half 51.9% vs 35.0%, second half 46.9% vs 21.9%), and it is not one
    strategy or symbol in disguise.

    Honest caveats, because this is an in-sample cut acted on with ~12
    independent days: >80% is mildly positive (+0.033, n=55) so the
    relationship is NOT monotone — the cut at 20% is where the evidence
    is, not a smooth law. Resolution excludes 446 signals that timed
    out, and E[R] assumes stops and targets fill exactly, which
    overstates it. Re-derive from the shadow journal before trusting
    the numbers above at a larger sample.

    Set `signal_max_target_move_pct` to 0 to disable.
    """
    cfg = cfg if cfg is not None else _config.load()
    cap = float(cfg.get("signal_max_target_move_pct", 20.0) or 0)
    entry, target1 = float(entry or 0), float(target1 or 0)
    if cap <= 0:
        return True, "target reachability not enforced (cap disabled)"
    if entry <= 0 or target1 <= 0:
        return True, "target reachability not checked (no geometry)"
    move = (target1 - entry) / entry * 100
    ok = move <= cap
    return ok, (f"target1 needs a {move:.1f}% move "
                f"({'within' if ok else 'BEYOND'} the {cap:.0f}% "
                f"reachability cap)")
