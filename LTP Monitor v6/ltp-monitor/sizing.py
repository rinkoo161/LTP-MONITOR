"""sizing.py — dynamic, risk-based position sizing.

Replaces the old fixed "lots_per_trade" with a calculation driven by:
  - configured capital and per-trade risk %
  - the ACTUAL risk of this specific trade (entry-to-stoploss distance
    for option buying; max_loss for spreads)
  - the exchange lot size
  - capital ALREADY deployed across other open positions/spreads —
    sizing against gross capital while other trades are open
    overstates what's actually available for a NEW trade
  - a hard lot cap (never bet the farm on one trade regardless of the
    math, and respect margin availability for spreads)

This is deliberately conservative: it always ROUNDS DOWN lots, and
falls back to the configured minimum (1 lot) only if the risk-based
calculation would allow zero lots but sizing is still permitted at all.
"""


def deployed_capital(cfg, positions, spreads):
    """How much capital is already tied up in open positions and
    spreads — premium paid for long options, margin blocked for sold
    spread legs. A new trade should be sized against what's LEFT, not
    against gross capital, or concurrent positions can collectively
    over-commit far past the intended risk budget."""
    total = 0.0
    for p in (positions or {}).values():
        total += (p.get("entry") or 0) * (p.get("qty") or 0)
    margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
    for sp in (spreads or {}).values():
        total += margin_per_lot * (sp.get("lots") or 1)
    return total


def size_option_buy(cfg, symbol, entry, stoploss, deployed=0):
    """Lots for a single-leg option buy, sized to risk a configured % of
    AVAILABLE (not gross) capital on this specific trade's SL distance."""
    lot = cfg["lot_sizes"].get(symbol, 75)
    if not cfg.get("dynamic_sizing_enabled", False):
        return cfg.get("lots_per_trade", 1), "fixed (dynamic sizing off)"
    capital = cfg.get("backtest_capital", 200000)
    available = max(0.0, capital - deployed)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0) / 100
    risk_budget = available * risk_pct
    per_lot_risk = max(0.01, (entry - stoploss)) * lot
    per_lot_cost = entry * lot
    max_lots = cfg.get("max_lots_per_trade", 10)
    lots_by_risk = int(risk_budget // per_lot_risk)
    can_afford_one = available >= per_lot_cost
    if lots_by_risk >= 1:
        lots = min(lots_by_risk, max_lots)
        why = (f"₹{risk_budget:.0f} risk budget ({risk_pct*100:.1f}% of "
              f"₹{available:,.0f} available, ₹{deployed:,.0f} already deployed "
              f"of ₹{capital:,.0f}) / ₹{per_lot_risk:.0f} risk-per-lot = "
              f"{lots} lot(s), capped at {max_lots}")
    elif can_afford_one:
        # Bug found 2026-07-22: risk_budget (typically 1-2% of capital) is
        # routinely smaller than a single lot's entry-to-SL risk, and the
        # old code hard-zeroed here instead of falling back to the minimum
        # lot as this module's own docstring promises. Capital can still
        # cover the trade's actual cost, so take the minimum size rather
        # than blocking every trade whose SL distance is wider than the
        # target risk % — the risk_pct knob tightens sizing, it shouldn't
        # be a hard veto on trades the account can otherwise afford.
        lots = 1
        why = (f"risk budget ₹{risk_budget:.0f} ({risk_pct*100:.1f}% of "
              f"₹{available:,.0f} available) is below this trade's "
              f"₹{per_lot_risk:.0f} per-lot risk — falling back to minimum "
              f"1 lot (₹{per_lot_cost:,.0f} affordable)")
    else:
        lots = 0
        why = (f"can't afford even 1 lot — ₹{per_lot_cost:,.0f} needed, only "
              f"₹{available:,.0f} available (₹{deployed:,.0f} already deployed "
              f"of ₹{capital:,.0f})")
    return lots, why


def size_spread(cfg, symbol, max_loss_per_share, deployed=0):
    """Lots for a credit spread, sized both by risk budget AND by
    AVAILABLE margin (selling options blocks real margin, unlike
    buying which only costs premium) — both computed against capital
    remaining after other open positions/spreads, not gross capital."""
    lot = cfg["lot_sizes"].get(symbol, 75)
    if not cfg.get("dynamic_sizing_enabled", False):
        return cfg.get("lots_per_trade", 1), "fixed (dynamic sizing off)"
    capital = cfg.get("backtest_capital", 200000)
    available = max(0.0, capital - deployed)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0) / 100
    risk_budget = available * risk_pct
    per_lot_risk = max(0.01, max_loss_per_share) * lot
    lots_by_risk = int(risk_budget // per_lot_risk)
    margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
    lots_by_margin = int(available // margin_per_lot) if margin_per_lot else 99
    max_lots = cfg.get("max_lots_per_trade", 10)
    if lots_by_margin < 1:
        lots = 0
        why = (f"available ₹{available:,.0f} (₹{deployed:,.0f} already deployed "
              f"of ₹{capital:,.0f}) can't cover margin for even 1 lot "
              f"(₹{margin_per_lot:,.0f}/lot needed)")
    elif lots_by_risk >= 1:
        lots = min(lots_by_risk, lots_by_margin, max_lots)
        why = (f"available ₹{available:,.0f} (₹{deployed:,.0f} already deployed "
              f"of ₹{capital:,.0f}) — risk budget allows {lots_by_risk} lot(s), "
              f"margin (₹{margin_per_lot:,.0f}/lot) allows {lots_by_margin}, "
              f"capped at {max_lots} — using {lots}")
    else:
        # Bug found 2026-07-22: a credit spread's max loss per lot is
        # ALREADY a defined, capped figure that margin backs directly —
        # layering a 1-2%-of-capital risk_pct veto on top of that
        # routinely computes to 0 lots (e.g. ₹1,000 risk budget vs an
        # ₹11,000+ max-loss-per-lot spread), even though margin clearly
        # allows the trade. The old code hard-zeroed here every time,
        # silently blocking every spread strategy regardless of market
        # conditions. Margin availability already IS the risk control for
        # a defined-risk instrument, so fall back to the minimum lot
        # rather than vetoing trades the account can actually margin.
        lots = min(1, lots_by_margin, max_lots)
        why = (f"risk budget ₹{risk_budget:.0f} ({risk_pct*100:.1f}% of "
              f"₹{available:,.0f} available) is below this spread's "
              f"₹{per_lot_risk:.0f} max loss per lot — falling back to "
              f"minimum 1 lot (margin allows {lots_by_margin})")
    return lots, why
