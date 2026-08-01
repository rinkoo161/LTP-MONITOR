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


def deployed_capital(cfg, positions, spreads, futures=None):
    """How much capital is already tied up in open positions, spreads,
    and (2026-07-26, S4 Phase 2) futures — margin blocked for a futures
    contract, exactly like a sold spread leg, unlike a bought option
    which only costs premium.

    `futures` param added rather than silently ignoring the position
    type: Phase 1's enter_future() read a `capital_deployed` bus key
    that NOTHING EVER SET (confirmed by search — zero writers), so its
    margin gate always compared against a deployed figure of 0,
    silently skipping the "already deployed elsewhere" half of its own
    check. Fixed by routing through this shared function instead, the
    same one options and spreads already use."""
    total = 0.0
    for p in (positions or {}).values():
        total += (p.get("entry") or 0) * (p.get("qty") or 0)
    margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
    for sp in (spreads or {}).values():
        total += margin_per_lot * (sp.get("lots") or 1)
    for f in (futures or {}).values():
        total += f.get("margin") or (
            cfg.get("margin_per_lot_future", 110000) * (f.get("lots") or 1))
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


def cap_by_rupee_risk(cfg, symbol, entry, stoploss, lots,
                      key="futures_risk_per_trade_rupees"):
    """Hard ceiling on what ONE trade may lose, in rupees.

    2026-07-29 — the gap this closes. size_future() (and its siblings)
    short-circuit to `lots_per_trade` whenever `dynamic_sizing_enabled`
    is False, which is the DEFAULT, so the entire risk-budget block
    below that early return is dead code on a stock install. The
    consequence is in the journal: a single futures stop cost ₹7,468
    against a `daily_loss_limit` of ₹5,000 — one stop consuming 150% of
    the day's entire loss budget. The per-trade stop therefore could
    never bind, and the PORTFOLIO kill-switch became the de-facto stop:
    11 of 40 futures exits, -₹21,215, 89% of all futures losses.

    Practitioner guidance is consistent on the ordering: fix the
    maximum loss per trade FIRST, because it stabilises the "L" in both
    payoff ratio and expectancy, and design targets relative to it.
    This cap therefore applies in BOTH sizing modes — it is a risk
    ceiling, not a sizing strategy.

    Returns (lots, why). lots may be 0, meaning the trade cannot be
    taken at any size without breaching the per-trade risk cap.
    """
    cap = cfg.get(key, 0)
    if not cap or cap <= 0:
        return lots, ""
    lot = cfg["lot_sizes"].get(symbol, 75)
    per_lot_risk = abs(entry - stoploss) * lot
    if per_lot_risk <= 0:
        return lots, ""
    max_lots = int(cap // per_lot_risk)
    if max_lots < 1:
        return 0, (f"blocked: 1 lot risks ₹{per_lot_risk:,.0f} > per-trade "
                   f"cap ₹{cap:,.0f} ({abs(entry - stoploss):.0f}pt stop "
                   f"x {lot} lot size)")
    if max_lots < lots:
        return max_lots, (f"capped {lots}->{max_lots} lot(s): risk "
                          f"₹{per_lot_risk * max_lots:,.0f} <= cap ₹{cap:,.0f}")
    return lots, ""


def atr_points(candles, period=14):
    """Wilder ATR in POINTS from a candle series.

    Used to size futures stops off realised volatility instead of a
    fixed percentage of price. `futures_sl_pct` 0.4% on NIFTY at 24,200
    is a 97-point stop and `futures_target_pct` 0.8% a 194-point target
    — the first sits inside ordinary intraday noise, the second asks
    for roughly a full session's range. The journal shows precisely
    that: of 40 futures trades, ONE reached its stop and ZERO reached
    target. A percentage of price cannot describe a volatility regime.
    """
    if not candles or len(candles) < 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    n = min(period, len(trs))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def size_future(cfg, symbol, entry, stoploss, deployed=0):
    """Lots for a futures LONG/SHORT, sized by BOTH risk budget AND
    available margin (2026-07-26, S4 Phase 2) — a futures contract
    blocks real exchange margin regardless of direction, exactly like a
    sold spread leg, so this mirrors size_spread()'s two-constraint
    shape rather than size_option_buy()'s premium-only one.

    `entry`/`stoploss` are FUTURES PRICES (points), not premium — the
    caller passes whatever the entry-signal engine or manual entry
    computed as the stop, in the same units as the live future_ohlc
    close.
    """
    lot = cfg["lot_sizes"].get(symbol, 75)
    if not cfg.get("dynamic_sizing_enabled", False):
        base = cfg.get("lots_per_trade", 1)
        capped, _why = cap_by_rupee_risk(cfg, symbol, entry,
                                         stoploss, base)
        return capped, ("fixed (dynamic sizing off)"
                        + (" — " + _why if _why else ""))
    capital = cfg.get("backtest_capital", 200000)
    available = max(0.0, capital - deployed)
    risk_pct = cfg.get("risk_pct_per_trade", 1.0) / 100
    risk_budget = available * risk_pct
    per_lot_risk = max(0.01, abs(entry - stoploss)) * lot
    lots_by_risk = int(risk_budget // per_lot_risk)
    margin_per_lot = cfg.get("margin_per_lot_future", 110000)
    lots_by_margin = int(available // margin_per_lot) if margin_per_lot else 99
    max_lots = cfg.get("max_lots_per_trade", 10)
    if lots_by_margin < 1:
        lots = 0
        why = (f"available ₹{available:,.0f} (₹{deployed:,.0f} already "
              f"deployed of ₹{capital:,.0f}) can't cover margin for even "
              f"1 lot (₹{margin_per_lot:,.0f}/lot needed)")
    elif lots_by_risk >= 1:
        lots = min(lots_by_risk, lots_by_margin, max_lots)
        why = (f"available ₹{available:,.0f} (₹{deployed:,.0f} already "
              f"deployed of ₹{capital:,.0f}) — risk budget allows "
              f"{lots_by_risk} lot(s), margin (₹{margin_per_lot:,.0f}/lot) "
              f"allows {lots_by_margin}, capped at {max_lots} — using {lots}")
    else:
        # Same fallback this module already applies to size_option_buy
        # and size_spread (both bug-fixed 2026-07-22 for the identical
        # failure mode): a 1-2%-of-capital risk budget is routinely
        # smaller than one lot's stop-distance risk on an index future,
        # and margin — already a hard, defined-risk cap for a contract
        # that must be marked to market — is the real constraint here.
        # Hard-zeroing would silently block every futures trade whose
        # stop is wider than the risk_pct knob, even when margin clearly
        # allows it.
        lots = min(1, lots_by_margin, max_lots)
        why = (f"risk budget ₹{risk_budget:.0f} ({risk_pct*100:.1f}% of "
              f"₹{available:,.0f} available) is below this trade's "
              f"₹{per_lot_risk:.0f} per-lot risk — falling back to "
              f"minimum 1 lot (margin allows {lots_by_margin})")
    # 2026-07-30 -- the cap applies on EVERY path, which is what v58.39
    # claimed and did not do.
    #
    # v58.39 added cap_by_rupee_risk only to the `not
    # dynamic_sizing_enabled` early return above. With dynamic sizing ON
    # -- the live configuration -- the branch below sizes from a
    # PERCENTAGE-of-capital risk budget and never consulted the cap. A
    # live FINNIFTY short took 6 lots on a 38.8-point stop: 38.8 x 65 =
    # ₹2,522 per lot, ₹15,132 total, against a ₹2,500 per-trade cap. It
    # then lost ₹12,840 and tripped the portfolio kill-switch, which is
    # exactly the failure v58.39 existed to prevent.
    #
    # The docstring called it "a risk ceiling, not a sizing strategy".
    # A ceiling that only guards one of three return paths is not a
    # ceiling. Applying it here, after every branch, is the only place
    # that makes the claim true.
    capped, cap_why = cap_by_rupee_risk(cfg, symbol, entry, stoploss, lots)
    if cap_why:
        return capped, why + " | " + cap_why
    return lots, why


def risk_coherence(cfg=None):
    """Are the per-trade and portfolio risk numbers consistent?

    2026-08-02. These four are set independently and silently disagree:

        risk_pct_per_trade x backtest_capital   the per-trade budget
        option_risk_per_trade_rupees            the per-trade cap
        futures_risk_per_trade_rupees           the futures per-trade cap
        portfolio_max_drawdown                  the whole-book cap

    The cap was derived to EQUAL the budget, because a budget that
    dynamic sizing never applies (dynamic_sizing_enabled is False, so
    size_option_buy returns lots_per_trade verbatim) is dead
    configuration. But the two live in different keys and DEFAULTS
    already disagrees with the live config — risk_pct_per_trade is 1.0
    in DEFAULTS and 2.0 in the running config. Duplicating a number in
    two places and hoping they stay equal is this codebase's most
    repeated failure, so it is checked rather than hoped.

    REPORTS ONLY. Returns a list of problem strings, empty when clean.
    """
    import config as _c
    cfg = cfg if cfg is not None else _c.load()
    out = []
    budget = (cfg.get("backtest_capital", 0) or 0) * \
             (cfg.get("risk_pct_per_trade", 0) or 0) / 100.0
    cap = cfg.get("option_risk_per_trade_rupees", 0) or 0
    kill = cfg.get("portfolio_max_drawdown", 0) or 0
    if budget and cap and abs(cap - budget) > 1:
        out.append(f"option cap ₹{cap:,.0f} != per-trade budget ₹{budget:,.0f} "
                   f"(risk_pct_per_trade x backtest_capital) — one of them is "
                   f"not what anyone thinks it is")
    if cap and kill and cap >= kill:
        out.append(f"option cap ₹{cap:,.0f} >= portfolio cap ₹{kill:,.0f} — a "
                   f"single trade can trip the whole book, which makes the "
                   f"portfolio cap meaningless exactly when it is needed")
    daily = cfg.get("daily_loss_limit", 0) or 0
    if kill and daily and daily <= kill:
        out.append(f"daily_loss_limit ₹{daily:,.0f} <= portfolio cap "
                   f"₹{kill:,.0f} — the daily limit fires FIRST, so the "
                   f"portfolio kill-switch can never be the operative "
                   f"constraint. One of them is doing nothing.")
    if kill and daily and daily > kill and daily / kill > 5:
        out.append(f"daily_loss_limit ₹{daily:,.0f} is {daily/kill:.0f}x the "
                   f"portfolio cap ₹{kill:,.0f} — it needs {daily/kill:.0f} "
                   f"full kill-switch cycles to fire, which is close to "
                   f"unreachable in one session")
    if cap and kill and kill / cap < 2:
        out.append(f"portfolio cap ₹{kill:,.0f} permits only {kill/cap:.2f} "
                   f"concurrent trades at the ₹{cap:,.0f} per-trade cap — a "
                   f"portfolio limit should survive more than one position")
    return out
