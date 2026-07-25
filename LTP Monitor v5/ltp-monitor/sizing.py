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
    lots = int(risk_budget // per_lot_risk)
    max_lots = cfg.get("max_lots_per_trade", 10)
    lots = max(1, min(lots, max_lots)) if risk_budget >= per_lot_risk else 0
    why = (f"₹{risk_budget:.0f} risk budget ({risk_pct*100:.1f}% of "
          f"₹{available:,.0f} available, ₹{deployed:,.0f} already deployed "
          f"of ₹{capital:,.0f}) / ₹{per_lot_risk:.0f} risk-per-lot = "
          f"{lots} lot(s), capped at {max_lots}")
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
    lots = max(0, min(lots_by_risk, lots_by_margin, max_lots))
    lots = max(1, lots) if lots_by_risk >= 1 and lots_by_margin >= 1 else 0
    why = (f"available ₹{available:,.0f} (₹{deployed:,.0f} already deployed "
          f"of ₹{capital:,.0f}) — risk budget allows {lots_by_risk} lot(s), "
          f"margin (₹{margin_per_lot:,.0f}/lot) allows {lots_by_margin}, "
          f"capped at {max_lots} — using {lots}")
    return lots, why
