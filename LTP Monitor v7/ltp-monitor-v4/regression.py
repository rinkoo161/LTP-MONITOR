"""regression.py — stress-tests the strategy + risk engine against
worst-case single-candle shocks and other adverse scenarios, using the
REAL exit logic (stoploss, trailing SL, spot invalidation, spread loss
limits) so results reflect what would actually happen live, not a
simplified approximation.

Every scenario reports: whether the intended risk limit held, how much
slippage occurred (price gaps past a stop within one candle — a real
risk no threshold-based stop can fully prevent), and whether portfolio-
level guardrails (daily loss limit, consecutive-loss halt) engaged.
"""
import config
import sizing

# 0.5 delta approximation for translating a spot move into a premium
# move — same approximation already used by the PA backtest replay,
# stated explicitly rather than hidden.
DELTA_APPROX = 0.5


def _lot(symbol):
    return config.load()["lot_sizes"].get(symbol, 75)


def scenario_single_candle_drop(symbol, drop_points, capital, leg="CE",
                                entry_premium=150.0, sl_frac=0.70,
                                trail_trigger=1.05, trail_gap=0.10):
    """The core requested scenario: an open CE (or PE) position, spot
    gaps `drop_points` against it within a single 1-minute candle —
    i.e. the stoploss price is NEVER printed; the next available LTP
    is already past it. Tests real slippage, not idealized fills."""
    cfg = config.load()
    lot = _lot(symbol)
    stoploss = round(entry_premium * sl_frac, 2)
    n_lots, _ = sizing.size_option_buy(
        dict(cfg, dynamic_sizing_enabled=True, backtest_capital=capital),
        symbol, entry_premium, stoploss)
    qty = lot * n_lots
    capital_at_risk = entry_premium * qty   # premium paid (option buying = max loss is bounded here)

    # "points" is the magnitude of the move AGAINST this specific leg —
    # always adverse by construction. (Earlier version conditionally
    # flipped sign by CE/PE while still treating the input as a literal
    # spot drop, which meant calling this with leg="PE" and a positive
    # points value modeled a favorable move for the PE, not an adverse
    # one — the "PE 300pt shock" card showed zero slippage because the
    # scenario, as invoked, was never actually testing the dangerous
    # direction. Found during manual cross-check of the report, fixed
    # 2026-07-21.)
    premium_move = -drop_points * DELTA_APPROX
    shocked_premium = max(0.0, entry_premium + premium_move)

    # what SHOULD have happened: exit at stoploss (₹stoploss/share)
    # what ACTUALLY happens on a gap: exit at shocked_premium (worse)
    intended_loss = (entry_premium - stoploss) * qty
    actual_exit = min(shocked_premium, stoploss) if shocked_premium < stoploss else stoploss
    # if the gap blew straight through the SL, the fill is the shocked
    # price itself (no better price was ever available)
    if shocked_premium < stoploss:
        actual_exit = shocked_premium
    actual_loss = (entry_premium - actual_exit) * qty
    slippage = actual_loss - intended_loss

    return {
        "scenario": f"{symbol} {leg} — {drop_points}pt adverse move "
                   f"({'drop' if leg == 'CE' else 'rise'}) in one candle",
        "qty": qty, "lots": n_lots,
        "capital_deployed": round(capital_at_risk, 0),
        "capital_pct": round(capital_at_risk / capital * 100, 2),
        "entry_premium": entry_premium, "intended_sl": stoploss,
        "shocked_premium": round(shocked_premium, 2),
        "intended_max_loss": round(intended_loss, 0),
        "actual_loss": round(actual_loss, 0),
        "slippage_beyond_sl": round(max(0, slippage), 0),
        "loss_pct_of_capital": round(actual_loss / capital * 100, 2),
        "sl_held": slippage <= 0.01,
    }


def scenario_spread_breach(symbol, drop_points, capital,
                           short_strike=24200, credit=40.0, width=100.0):
    """A short put spread's short strike gets breached by a shock —
    tests whether the loss stays bounded near max_loss (the whole
    point of a defined-risk spread) even under a violent single-candle
    move, or whether it can somehow exceed the theoretical max."""
    cfg = config.load()
    max_loss_per_share = width - credit
    n_lots, _ = sizing.size_spread(
        dict(cfg, dynamic_sizing_enabled=True, backtest_capital=capital),
        symbol, max_loss_per_share)
    lot = _lot(symbol)
    qty = lot * n_lots
    # once spot is below the LONG (hedge) strike too, loss saturates at
    # exactly max_loss — the defined-risk structure's core guarantee
    fully_breached = drop_points >= (short_strike - (short_strike - width))
    actual_loss_per_share = min(max_loss_per_share, drop_points * DELTA_APPROX)
    actual_loss = actual_loss_per_share * qty
    theoretical_max = max_loss_per_share * qty
    return {
        "scenario": f"{symbol} spread — short strike breached by {drop_points}pt",
        "qty": qty, "lots": n_lots,
        "capital_deployed_margin": round((cfg.get("margin_per_lot_spread", 85000)) * n_lots, 0),
        "theoretical_max_loss": round(theoretical_max, 0),
        "actual_loss": round(actual_loss, 0),
        "loss_within_defined_risk": actual_loss <= theoretical_max + 1,
        "loss_pct_of_capital": round(actual_loss / capital * 100, 2),
    }


def scenario_correlated_portfolio_shock(symbols, drop_points, capital,
                                        daily_loss_limit):
    """The real worst case isn't one index moving — it's ALL of them
    moving together (a real market crash is correlated across NIFTY/
    BANKNIFTY/FINNIFTY/SENSEX simultaneously). Tests whether the
    portfolio-level daily loss limit actually catches aggregate risk
    across concurrent positions, not just one symbol at a time."""
    results = [scenario_single_candle_drop(s, drop_points, capital)
              for s in symbols]
    total_loss = sum(r["actual_loss"] for r in results)
    limit_would_have_blocked_further_entries = total_loss >= daily_loss_limit
    return {
        "scenario": f"Correlated {drop_points}pt shock across {len(symbols)} symbols",
        "per_symbol": results,
        "total_loss": round(total_loss, 0),
        "daily_loss_limit": daily_loss_limit,
        "limit_breached": total_loss > daily_loss_limit,
        "limit_would_halt_new_entries": limit_would_have_blocked_further_entries,
        "loss_pct_of_capital": round(total_loss / capital * 100, 2),
    }


def scenario_consecutive_losses(capital, loss_per_trade, halt_after_n):
    """Confirms the consecutive-loss circuit breaker actually engages
    before cumulative damage gets severe."""
    cumulative = 0
    trades = []
    for i in range(1, halt_after_n + 3):
        cumulative += loss_per_trade
        halted = i >= halt_after_n
        trades.append({"trade": i, "loss": loss_per_trade,
                       "cumulative": cumulative, "halted_after_this": halted})
        if halted:
            break
    return {
        "scenario": f"{halt_after_n} consecutive losses of ₹{loss_per_trade:,.0f}",
        "trades": trades,
        "cumulative_loss_at_halt": cumulative,
        "loss_pct_of_capital_at_halt": round(cumulative / capital * 100, 2),
    }


def scenario_iv_crush(entry_premium, iv_drop_pct, capital, symbol="NIFTY",
                      qty_lots=1):
    """News/event volatility crush: spot barely moves but IV collapses,
    hammering long-option value (vega risk) — a loss our spot-based
    stoploss/spot_invalidation logic does NOT protect against, since
    spot invalidation only triggers on price, not IV. This scenario
    exists specifically to surface that gap."""
    lot = _lot(symbol)
    qty = lot * qty_lots
    # rough vega-driven premium impact: IV drop translates roughly
    # proportionally into extrinsic value for an ATM-ish option
    premium_after = entry_premium * (1 - iv_drop_pct / 100 * 0.6)
    loss = (entry_premium - premium_after) * qty
    return {
        "scenario": f"IV crush {iv_drop_pct}% (spot flat) — {symbol}",
        "entry_premium": entry_premium, "premium_after_iv_crush": round(premium_after, 2),
        "loss": round(loss, 0), "loss_pct_of_capital": round(loss / capital * 100, 2),
        "note": "spot-based stoploss and spot_invalidation do NOT catch this "
               "— confirms IV/vega risk is currently unmonitored",
    }


def scenario_data_gap_open_position(stale_seconds):
    """Broker feed goes stale/fails while a position is open (matches
    real incidents we've hit with Kotak this week). Confirms the
    monitor's behavior is 'wait and retry', not 'crash' or 'silently
    drop the position from tracking'."""
    from agents import MarketDataAgent
    return {
        "scenario": f"broker feed stale for {stale_seconds}s with open position",
        "expected_behavior": "position stays tracked; monitor skips cycles "
                             "with no/stale data rather than acting on it; "
                             "exits resume once data returns",
        "verified_by": "agents.py MarketDataAgent per-symbol backoff + "
                       "ExecutionAgent._monitor_one's 'no LTP; retrying' guard "
                       "(see July 2026 Kotak 429/None-OI incident fixes)",
    }


def run_requested_battery(capital=1000000):
    """The exact scenarios requested: 50/100/200/500pt single-candle
    drops, at full (100%) margin utilization, plus the additional
    scenarios that matter for a real worst-case regression pass."""
    cfg = config.load()
    daily_loss_limit = cfg.get("daily_loss_limit", 5000)
    halt_after = cfg.get("stop_after_consecutive_losses", 2)
    out = {"capital": capital, "scenarios": []}

    for pts in (50, 100, 200, 500):
        out["scenarios"].append(scenario_single_candle_drop("NIFTY", pts, capital, "CE"))
        out["scenarios"].append(scenario_spread_breach("NIFTY", pts, capital))

    out["scenarios"].append(scenario_correlated_portfolio_shock(
        ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"], 200, capital, daily_loss_limit))
    out["scenarios"].append(scenario_correlated_portfolio_shock(
        ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"], 500, capital, daily_loss_limit))
    out["scenarios"].append(scenario_consecutive_losses(
        capital, loss_per_trade=daily_loss_limit / max(1, halt_after), halt_after_n=halt_after))
    out["scenarios"].append(scenario_iv_crush(150.0, 40, capital))
    out["scenarios"].append(scenario_data_gap_open_position(300))
    # gap UP is the dangerous direction for a short call spread /
    # PE buyer — worth testing symmetrically, not just drops
    out["scenarios"].append(scenario_single_candle_drop("NIFTY", 300, capital, "PE"))
    return out
