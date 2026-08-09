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
