"""strategy_docs.py — static, human-readable documentation for each
strategy's parameters and entry/exit logic. Pure data; used by the
version popup in the dashboard so a parameter dump like
{"buf_frac": 0.043} reads as "Breakout buffer: 4.3% of the opening range".
"""

DOCS = {
    "orb": {
        "title": "Opening Range Breakout (5m + anchor)",
        "indicators": ["Opening range high/low (first N minutes)",
                       "Session anchor (cumulative typical-price mean)"],
        "entry": [
            "Compute the high/low of the first `or_minutes` candles.",
            "If price closes beyond that range by `buf_frac` × range width, "
            "enter in the breakout direction.",
            "Skip if the opening range itself is smaller than "
            "`min_or_range_pct`% of spot — a dead open has no energy.",
            "If `anchor_filter` is on, the breakout must also be on the "
            "same side as the session anchor (extra confirmation).",
        ],
        "exit": ["Stop at the opposite side of the opening range.",
                 "Target 1 = entry + 1× range; move stop to breakeven.",
                 "Target 2 = entry + 2× range — full exit.",
                 "Square off at end of day."],
        "params": {
            "or_minutes": "Opening range window, in minutes",
            "buf_frac": "Breakout confirmation buffer (fraction of OR range)",
            "min_or_range_pct": "Minimum OR width required (% of spot price)",
            "anchor_filter": "Require breakout to agree with session anchor (1=on, 0=off)",
            "max_trades_per_day": "Cap on entries per day",
        },
    },
    "vwap_pullback": {
        "title": "Anchor Pullback (trend-following)",
        "indicators": ["Session anchor (typical-price mean — VWAP proxy; "
                      "index spot has no volume so a true VWAP isn't "
                      "computable, this is the honest substitute)",
                      "9-period EMA (resumption confirmation)"],
        "entry": [
            "Price must be trending relative to the anchor (above it and "
            "above the 9-EMA for longs, mirrored for shorts).",
            "Wait for a pullback to within `band_pct`% of the anchor.",
            "Enter when price resumes in the trend direction "
            "(crosses back above/below the 9-EMA).",
        ],
        "exit": ["Stop beyond the anchor by 2× the band.",
                 "Target 1 = the pullback depth re-extended + band; "
                 "move stop to breakeven.",
                 "Target 2 = 2× that distance.",
                 "Square off at end of day."],
        "params": {
            "band_pct": "Pullback proximity band (% of spot) that counts as 'touched the anchor'",
            "trend_ema": "EMA period defining the anchor trend filter",
            "resume_ema": "EMA period confirming resumption after pullback",
            "max_trades_per_day": "Cap on entries per day",
        },
    },
    "ema_mtf": {
        "title": "5/13 EMA Cross (multi-timeframe confirmed)",
        "indicators": ["Fast/slow EMA cross on 1-minute candles",
                      "Same EMA pair recomputed on 5m and 15m for confirmation"],
        "entry": [
            "Fast EMA crosses the slow EMA on the 1-minute chart.",
            "If `mtf_confirm` is on, the 5-minute AND 15-minute EMA "
            "relationship must already agree with the new cross direction "
            "— filters out 1-minute noise crosses against the larger trend.",
        ],
        "exit": ["Stop and targets are set from the EMA separation at "
                 "entry (a proxy for current volatility): stop = entry − "
                 "risk, target 1 = entry + risk, target 2 = entry + 2×risk.",
                 "Square off at end of day."],
        "params": {
            "fast": "Fast EMA period",
            "slow": "Slow EMA period",
            "mtf_confirm": "Require 5m + 15m EMA agreement (1=on, 0=off)",
            "max_trades_per_day": "Cap on entries per day",
        },
    },
    "bull_put_spread": {
        "title": "Bull Put Spread (OI wall)",
        "indicators": ["Option-chain OI walls (S1 support = highest "
                      "OI+volume PE strike below spot)"],
        "entry": [
            "Sell the PE at the S1 wall.",
            "Buy a PE 2 strikes lower as the hedge.",
            "Require the wall to be at least `wall_gap_frac` strike-gaps "
            "away from spot (too close = about to be breached).",
            "Require net credit to be at least `credit_min_frac` of the "
            "spread width (otherwise not worth the defined risk).",
        ],
        "exit": [
            "Profit target: `profit_capture` × credit captured.",
            "Loss limit: the smaller of (`loss_mult` × credit) or the "
            "spread's true max loss (width − credit).",
            "Immediate exit if spot breaches the short strike.",
            "Square off at end of day.",
        ],
        "params": {
            "wall_gap_frac": "Min. distance of the wall from spot, in strike-gaps",
            "credit_min_frac": "Min. credit required, as a fraction of spread width",
            "profit_capture": "Exit once this fraction of the credit is captured",
            "loss_mult": "Loss-limit multiple of credit (capped at true max loss)",
        },
    },
    "bear_call_spread": {
        "title": "Bear Call Spread (OI wall)",
        "indicators": ["Option-chain OI walls (R1 resistance = highest "
                      "OI+volume CE strike above spot)"],
        "entry": [
            "Sell the CE at the R1 wall.",
            "Buy a CE 2 strikes higher as the hedge.",
            "Same wall-distance and credit-fraction filters as the put spread.",
        ],
        "exit": ["Mirror of the bull put spread's exits, breach checked "
                 "against the short CE strike."],
        "params": {
            "wall_gap_frac": "Min. distance of the wall from spot, in strike-gaps",
            "credit_min_frac": "Min. credit required, as a fraction of spread width",
            "profit_capture": "Exit once this fraction of the credit is captured",
            "loss_mult": "Loss-limit multiple of credit (capped at true max loss)",
        },
    },
    "momentum_buy": {
        "title": "Momentum Option Buying (core AI signal)",
        "indicators": ["OI-derived bias score", "Market state "
                      "(trending/rangebound/divergence)", "Regime + "
                      "multi-timeframe confluence"],
        "entry": ["ATM CE/PE bought in the direction of chain bias when "
                  "the state is TRENDING and confidence clears the "
                  "configured minimum. This is the manually-confirmed "
                  "signal shown on the main dashboard — it is not gated "
                  "by the backtest live/paused system since it always "
                  "requires your confirmation before placing an order."],
        "exit": ["Stop at `sl_frac` of entry premium.",
                 "Target 1 at `t1_frac` of entry; trail stop to breakeven.",
                 "Target 2 at `t2_frac` — full exit.",
                 "Trailing stop activates once premium is "
                 "`trail_trigger`× entry, trailing `trail_gap` below peak.",
                 "Spot invalidation and end-of-day square-off also apply."],
        "params": {
            "sl_frac": "Stoploss as a fraction of entry premium",
            "t1_frac": "Target 1 as a multiple of entry premium",
            "t2_frac": "Target 2 as a multiple of entry premium",
            "min_confidence": "Minimum signal confidence required to enter",
            "trail_trigger": "Premium multiple of entry that activates trailing stop",
            "trail_gap": "Trailing stop distance below the peak (fraction)",
        },
    },
}
