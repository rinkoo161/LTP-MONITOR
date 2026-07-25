"""sizing.py — dynamic, risk-based position sizing.

Replaces the old fixed "lots_per_trade" with a calculation driven by:
  - configured capital and per-trade risk %
  - the ACTUAL risk of this specific trade (entry-to-stoploss distance
    for option buying; max_loss for spreads)
  - the exchange lot size
  - a hard lot cap (never bet the farm on one trade regardless of the
    math, and respect margin availability for spreads)

This is deliberately conservative: it always ROUNDS DOWN lots, and
falls back to the configured minimum (1 lot) only if the risk-based
calculation would allow zero lots but sizing is still permitted at all.
"""


def size_option_buy(cfg, symbol, entry, stoploss):
    """Lots for a single-leg option buy, sized to risk a configured % of
    capital on this specific trade's actual SL distance."""
    lot = cfg["lot_sizes"].get(symbol, 75)
    if not cfg.get("dynamic_sizing_enabled", False):
        return cfg.get("lots_per_trade", 1), "fixed (dynamic sizing off)"
    capital = cfg.get("backtest_capital", 200000)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0) / 100
    risk_budget = capital * risk_pct
    per_lot_risk = max(0.01, (entry - stoploss)) * lot
    lots = int(risk_budget // per_lot_risk)
    max_lots = cfg.get("max_lots_per_trade", 10)
    lots = max(1, min(lots, max_lots))
    why = (f"₹{risk_budget:.0f} risk budget ({risk_pct*100:.1f}% of "
          f"₹{capital:,.0f}) / ₹{per_lot_risk:.0f} risk-per-lot = "
          f"{lots} lot(s), capped at {max_lots}")
    return lots, why


def size_spread(cfg, symbol, max_loss_per_share):
    """Lots for a credit spread, sized both by risk budget AND by
    available margin (selling options blocks real margin, unlike
    buying which only costs premium)."""
    lot = cfg["lot_sizes"].get(symbol, 75)
    if not cfg.get("dynamic_sizing_enabled", False):
        return cfg.get("lots_per_trade", 1), "fixed (dynamic sizing off)"
    capital = cfg.get("backtest_capital", 200000)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0) / 100
    risk_budget = capital * risk_pct
    per_lot_risk = max(0.01, max_loss_per_share) * lot
    lots_by_risk = int(risk_budget // per_lot_risk)
    margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
    lots_by_margin = int(capital // margin_per_lot) if margin_per_lot else 99
    max_lots = cfg.get("max_lots_per_trade", 10)
    lots = max(1, min(lots_by_risk, lots_by_margin, max_lots))
    why = (f"risk budget allows {lots_by_risk} lot(s), margin "
          f"(₹{margin_per_lot:,.0f}/lot) allows {lots_by_margin}, "
          f"capped at {max_lots} — using {lots}")
    return lots, why
