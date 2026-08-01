"""options_costs.py — premium-aware round-trip cost for index options.

v59.0 item 7. `fee_per_lot` charges a flat rupee amount per lot per
leg-transaction. For options that is closer to right than it was for
futures — option taxes are levied on PREMIUM, which is small — but it
omits the single largest component entirely: THE BID-ASK SPREAD.

An ATM index option quoted 119.5 / 120.5 costs half a point to cross on
each side. At a 75-lot that is ₹37.50 per transaction, ₹75 per round
trip — more than the entire statutory bill (~₹65) and more than the ₹60
the replay currently charges for the whole trade.

WHY THIS MATTERS MORE THAN THE FUTURES CASE. The futures defect was
large (10x) but confined to an instrument that was already disabled.
This one reaches `replay_spreads`, `replay_momentum`, `replay_pa`,
`replay_ew_reversal` and `replay_ta_elliott` — and the promotion rule is
a bare sign test:

    profitable_now = trades >= min_conf and net_pnl > 0
    entry["live_enabled"] = profitable_now

so any understated cost that carries a strategy from negative to
positive promotes it to trade real money.

NOTHING HERE IS WIRED INTO THE LIVE GATE. This module computes; item 7
is an audit and reports first.

Rates are config-driven with read-time clamping, same discipline as
futures_costs.py.
"""
import config

RATES = {
    # name: (default, lo, hi)
    "opt_brokerage_per_order": (20.0, 0.0, 100.0),
    "opt_stt_sell_pct":        (0.001, 0.0, 0.005),    # 0.1% of SELL premium
    "opt_exchange_txn_pct":    (0.0005, 0.0, 0.002),   # ~0.05% of premium
    "opt_sebi_turnover_pct":   (0.000001, 0.0, 0.00001),
    "opt_stamp_duty_pct":      (0.00003, 0.0, 0.0002),
    "opt_gst_pct":             (0.18, 0.0, 0.30),
    # The component fee_per_lot omits entirely. Expressed in PREMIUM
    # POINTS crossed per transaction: 0.5 means paying half a point of a
    # 1.0-point spread, which is what a marketable limit order achieves
    # on a liquid ATM strike. Raise it for wide/illiquid strikes.
    "opt_halfspread_points":   (0.5, 0.0, 10.0),
}


def rate(name, cfg=None):
    default, lo, hi = RATES[name]
    cfg = cfg if cfg is not None else config.load()
    try:
        v = float(cfg.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def cost_round_trip(premium_in, premium_out, lot, legs=1, cfg=None,
                    halfspread=None):
    """Round-trip cost in RUPEES for `legs` option legs, 1 lot each.

    legs=1 : a single-leg buy (the shape replay_pa/momentum/pa model)
    legs=2 : a two-leg credit spread (what replay_spreads models)

    Every leg is opened and closed, so a 2-leg spread crosses the spread
    FOUR times — which is why bid-ask dominates there.
    """
    cfg = cfg if cfg is not None else config.load()
    hs = rate("opt_halfspread_points", cfg) if halfspread is None else float(halfspread)
    legs = max(1, int(legs))
    buy_notional = float(premium_in) * lot * legs
    sell_notional = float(premium_out) * lot * legs

    brokerage = rate("opt_brokerage_per_order", cfg) * 2 * legs
    stt = rate("opt_stt_sell_pct", cfg) * sell_notional
    exch = rate("opt_exchange_txn_pct", cfg) * (buy_notional + sell_notional)
    sebi = rate("opt_sebi_turnover_pct", cfg) * (buy_notional + sell_notional)
    stamp = rate("opt_stamp_duty_pct", cfg) * buy_notional
    gst = rate("opt_gst_pct", cfg) * (brokerage + exch + sebi)
    statutory = brokerage + stt + exch + sebi + stamp + gst
    # 2 transactions per leg, each crossing half the quoted spread
    spread_cost = hs * lot * 2 * legs
    return {"statutory": statutory, "spread": spread_cost,
            "total": statutory + spread_cost,
            "halfspread_points": hs, "legs": legs, "lot": lot}


def flat_model_cost(legs=1, lots=1, cfg=None):
    """What fee_per_lot charges for the same trade, for comparison."""
    cfg = cfg if cfg is not None else config.load()
    return float(cfg.get("fee_per_lot", 40)) * 2 * legs * max(1, int(lots))
