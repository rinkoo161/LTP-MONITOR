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


def size_by_atr_risk(cfg, symbol, atr, delta=1.0, deployed=0):
    """ATR-based position sizing per rinkoo.docx (2026-07-23):
      Stop-loss buffer (points) = 1.5 x ATR
      Risk per lot (Rs) = SL buffer x lot_size x delta
      Number of lots = floor((capital x risk_pct%) / risk_per_lot)

    `delta` scales the SL distance for options vs futures — the docx
    is explicit about this: "If you are buying NIFTY Options, keep in
    mind that options move based on Delta (e.g., an At-The-Money
    option moves ~0.50 points for every 1-point move in NIFTY),
    meaning your option premium stop-loss distance in rupees will be
    roughly half of the index point distance." Pass delta=1.0 (default)
    for futures/direct index-point instruments, ~0.5 for ATM options.

    Validated against the docx's own worked example: capital
    Rs10,00,000, ATR 228.47 (SL buffer 342.7 pts), NIFTY lot 25 ->
    risk/lot Rs8,567.50; at 1% risk -> 1 lot (0.86% actual risk); at
    2% risk -> 2 lots (1.71% actual risk). Always rounds down — never
    a fractional lot, matching the docx's explicit rule.
    """
    lot = cfg["lot_sizes"].get(symbol, 75)
    capital = max(0.0, cfg.get("backtest_capital", 200000) - deployed)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0)
    sl_buffer_pts = 1.5 * atr
    risk_per_lot = sl_buffer_pts * lot * delta
    max_risk = capital * risk_pct / 100
    max_lots = cfg.get("max_lots_per_trade", 10)
    lots = int(max_risk // risk_per_lot) if risk_per_lot > 0 else 0
    lots = max(0, min(lots, max_lots))
    actual_risk = lots * risk_per_lot
    delta_note = f" x {delta:g} delta" if delta != 1.0 else ""
    why = (f"ATR-risk sizing: SL buffer {sl_buffer_pts:.1f}pts (1.5x ATR "
          f"{atr:.2f}) x {lot} lot{delta_note} = ₹{risk_per_lot:,.0f} "
          f"risk/lot; ₹{max_risk:,.0f} budget ({risk_pct:g}% of "
          f"₹{capital:,.0f} available) / ₹{risk_per_lot:,.0f} = {lots} "
          f"lot(s) (rounded down), actual risk ₹{actual_risk:,.0f} "
          f"({(actual_risk / capital * 100) if capital else 0:.2f}%)")
    return lots, why
