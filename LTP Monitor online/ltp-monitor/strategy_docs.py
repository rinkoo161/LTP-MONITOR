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
        "note_v58_48": "Deck setups 4/5/6 (Wave 3, Wave 5, end of Wave C) "
                       "are available here as OPTIONAL confirmations — they "
                       "are the same trade this strategy already takes, with "
                       "a way to judge WHICH pullback is worth taking. All "
                       "OFF by default. Setup 7 is a counter-trend fade and "
                       "lives in Strategy 8 instead.",
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
    # 2026-07-26 (v54) — added so the consolidated Strategies-page table
    # can show the same "Configuration" detail view (indicators/entry/
    # exit/params) for EVERY strategy family, not just the ones the
    # backtester versions. These three don't go through backtester's
    # version-history/rollback system (no per-symbol tuned versions
    # exist for them yet), so their detail view is params + logic only,
    # sourced from live config rather than a version snapshot.
    "sg_ema": {
        "title": "Strategy 7 \u2014 Structure-Gated EMA Cross (SG-EMA)",
        "indicators": ["5/13 EMA cross on 1m candles (same as ema_mtf)",
                       "5m + 15m EMA agreement (MTF confirm)",
                       "ZigZag structure (HH/HL/LH/LL pivots)",
                       "AI Market Bias score (Feature #2)"],
        "entry": [
            "Fast/slow EMA crosses on the 1m chart (identical to ema_mtf).",
            "If mtf_confirm is on, 5m AND 15m EMA relationship must "
            "already agree with the cross direction.",
            "If require_structure is on, the last CONFIRMED ZigZag pivot "
            "must be HH/HL for a long or LH/LL for a short \u2014 skipped "
            "(not blocked) when no pivot is confirmed yet.",
            "If require_ai_bias is on, the AI bias score must agree in "
            "direction and exceed min_ai_bias \u2014 skipped when bias isn't "
            "computed yet, never blocks on missing data.",
        ],
        "exit": ["Structural stop: last confirmed same-side pivot \u00b1 "
                 "structural_stop_buffer_pct (EMA-separation fallback if "
                 "no pivot exists).",
                 "Target = rr_target \u00d7 the structural risk distance.",
                 "Structure-break exit: a new adverse confirmed pivot "
                 "after entry closes the position.",
                 "Standard trailing stop, time stop, portfolio kill-"
                 "switch, and 15:15 EOD square-off all apply."],
        "params": {
            "s7_ema_fast": "Fast EMA period",
            "s7_ema_slow": "Slow EMA period",
            "s7_mtf_confirm": "Require 5m+15m EMA agreement (1=on, 0=off)",
            "s7_require_structure": "Require the ZigZag structure gate",
            "s7_require_ai_bias": "Require the AI-bias gate",
            "s7_min_ai_bias": "Minimum |AI bias score| to confirm",
            "s7_structural_stop_buffer_pct": "Buffer beyond the pivot, as % of spot",
            "s7_rr_target": "Risk:reward multiple for the target",
            "s7_max_trades_per_day": "Cap on entries per day",
        },
    },
    "ew_reversal": {
        "title": "Strategy 8 \u2014 EW-Reversal (ending diagonal / H&S / failed H&S)",
        "indicators": ["ZigZag pivots (the SAME series the chart draws)",
                       "MACD histogram (12/26/9) on 1m",
                       "15m EMA stack as the higher-timeframe \"Tide\""],
        "entry": [
            "ENDING DIAGONAL: five overlapping swings in a contracting "
            "wedge where wave (iv) enters wave (i)'s price territory, "
            "with MACD-histogram divergence against price. Entry on the "
            "break of the diagonal.",
            "HEAD & SHOULDER: shoulders within shoulder_tol_pct of each "
            "other, head beyond both, and a close through the sloping "
            "neckline. The confirmation is the deck's own: LESS bear "
            "power on the histogram at the break than at the previous "
            "low \u2014 \"bears need less power to cause the breakdown\".",
            "FAILED H&S: a false break beyond the neckline that is then "
            "reclaimed past the termination point of wave B. This is a "
            "CONTINUATION trade, so it additionally requires the Tide "
            "to be against the failed pattern \u2014 skipped, never "
            "blocked, when the 15m series isn't available yet.",
            "Detectors run in order (ending diagonal, H&S, failed H&S) "
            "and the first match wins; each can be disabled on its own.",
        ],
        "exit": ["Structural stop: beyond the pattern's own invalidation "
                 "pivot (wave-v extreme, right shoulder, or false-break "
                 "extreme) plus stop_buffer_pct.",
                 "T1 = rr_target \u00d7 the structural risk distance, so the "
                 "risk-reward gate can never auto-reject the signal.",
                 "T2 carries the deck's structural objective (the "
                 "beginning of the diagonal, or the measured move) when "
                 "that is further out than T1; otherwise 1.33 \u00d7 T1.",
                 "Standard trailing stop, time stop, portfolio kill-"
                 "switch and 15:15 EOD square-off all apply."],
        "params": {
            "s8_ending_diagonal_enabled": "Enable the ending-diagonal detector",
            "s8_hs_enabled": "Enable the Head & Shoulder detector",
            "s8_failed_hs_enabled": "Enable the failed-H&S detector",
            "s8_zigzag_deviation_pct": "ZigZag reversal threshold (matches the chart)",
            "s8_require_macd_divergence": "Require the histogram confirmation",
            "s8_require_tide": "Require Tide agreement (failed H&S only)",
            "s8_min_pattern_bars": "Minimum candles the pattern must span",
            "s8_shoulder_tol_pct": "Max shoulder asymmetry, as % of the head",
            "s8_neckline_buffer_pct": "Buffer beyond the neckline for a valid break",
            "s8_stop_buffer_pct": "Buffer beyond the invalidation pivot, as % of price",
            "s8_rr_target": "Risk:reward multiple for T1 (must stay >= 1.95)",
            "s8_max_trades_per_day": "Cap on entries per day",
        },
    },
    "momentum_confluence": {
        "title": "Momentum Confluence (TradingView Pine Script port)",
        "indicators": ["RSI(14) with divergence detection",
                       "5/13 EMA cross",
                       "MACD (12/26/9)",
                       "Stochastic",
                       "EMA bias"],
        "entry": [
            "TWO independent paths — either alone is sufficient.",
            "PATH 1, confluence reversal: an RSI divergence, PLUS a 5/13 "
            "EMA cross within `cross_lookback` bars, PLUS at least "
            "`min_confluence` of four agreeing (MACD, RSI, Stochastic, "
            "EMA bias).",
            "PATH 2, the \"weapon\" pattern: equal highs or equal lows "
            "within `eq_tol_pct`, followed by an EMA break through them.",
            "Ported from a Pine Script already validated live on "
            "TradingView, so the entry logic is a port rather than a "
            "fresh design.",
        ],
        "exit": ["Stop at the entry candle's own low (long) or high "
                 "(short) — structural, not a fixed percentage.",
                 "Target is `rr_target` x that risk distance.",
                 "EARLY EXIT (v58.47): the position is closed the moment "
                 "the MACD histogram's slope reverses against it, held "
                 "for `hist_turn_confirm_bars` consecutive bars. This is "
                 "the Pine original's own exit and was the one "
                 "documented simplification in the port; it needed a new "
                 "mechanism, since every other PA strategy expresses its "
                 "exit as fixed prices set at signal time rather than a "
                 "condition re-evaluated on future candles.",
                 "Standard trailing stop, portfolio kill-switch and "
                 "15:15 EOD square-off all apply."],
        "params": {
            "min_confluence": "How many of the four must agree (2-4)",
            "cross_lookback": "Bars within which the EMA cross must have occurred",
            "eq_tol_pct": "Tolerance for calling two highs/lows 'equal'",
            "rr_target": "Risk:reward multiple for the target (>= 1.95)",
            "max_trades_per_day": "Cap on entries per day",
        },
    },
    "ta_elliott": {
        "title": "Strategy 9 \u2014 TA with Elliott (Marrying TA with Elliot)",
        "indicators": ["Bollinger Bands (20,2) \u2014 band DIRECTION classifies "
                       "impulse vs corrective",
                       "GMMA (3,5,8,10,12,15 vs 30,35,40,45,50,60) \u2014 "
                       "compression then expansion",
                       "MACD (12/26/9) line, histogram, and zero-line reversal",
                       "RSI(14) divergence (Wilder smoothing)",
                       "ADX(14) \u2014 \"dynamic wave\" confirmation",
                       "15m EMA stack as the higher-timeframe Tide",
                       "ZigZag pivots (the SAME series the chart draws)"],
        "entry": [
            "Step 1, per the deck: classify IMPULSE vs CORRECTIVE from "
            "Bollinger band direction. Price tagging a band with the "
            "band turning = impulse; flat band = wave 4 or B; price "
            "tagging a band while the band FAILS to turn = the "
            "correction is ending.",
            "Entries are only taken in a CORRECTIVE phase \u2014 this "
            "strategy buys the end of a correction, it does not chase "
            "an impulse already under way.",
            "The Tide must favour the trade (hard veto), skipped and "
            "never blocking when the 15m series isn't available.",
            "At least min_confluence of seven independent signals must "
            "agree: Bollinger corrective stall, GMMA expansion, MACD "
            "zero-line reversal, reverse/hidden MACD divergence, "
            "regular MACD divergence, RSI divergence, ADX dynamic.",
            "Otherwise no trade \u2014 the deck's \"When in doubt, Do Not "
            "Trade\" encoded as a state, not a preference.",
        ],
        "exit": ["Structural stop at the last confirmed ZigZag pivot on "
                 "the correct side, falling back to the Bollinger band "
                 "when no usable pivot exists.",
                 "T1 = rr_target \u00d7 the structural risk distance, so the "
                 "risk-reward gate can never auto-reject the signal; "
                 "T2 = 1.33 \u00d7 T1.",
                 "Standard trailing stop, time stop, portfolio kill-"
                 "switch and 15:15 EOD square-off all apply."],
        "params": {
            "ta_elliott_enabled": "Master switch (state still publishes when off)",
            "ta_auto_deploy": "Allow the strategy to actually fire signals",
            "ta_min_confluence": "How many of the seven signals must agree",
            "ta_require_tide": "Require higher-timeframe agreement",
            "ta_bb_period": "Bollinger period",
            "ta_bb_stdev": "Bollinger standard deviations",
            "ta_bb_slope_eps": "Slope threshold separating flat from turning",
            "ta_gmma_compression_pct": "Percentile below which GMMA counts as compressed",
            "ta_adx_dynamic_min": "ADX above which a wave counts as dynamic",
            "ta_rsi_period": "RSI period",
            "ta_zigzag_deviation_pct": "ZigZag reversal threshold (matches the chart)",
            "ta_stop_buffer_pct": "Buffer beyond the stop reference, as % of price",
            "ta_rr_target": "Risk:reward multiple for T1 (must stay >= 1.95)",
            "ta_max_trades_per_day": "Cap on entries per day",
        },
    },
    "mtf_confluence": {
        "title": "MACD + Stoch Confluence (rinkoo.docx)",
        "indicators": ["Daily MACD histogram", "Weekly MACD histogram",
                      "Daily RSI(14)", "Daily Stochastic %K/%D",
                      "Daily Bollinger %B", "Futures OI buildup (supportive)",
                      "Global markets sentiment (supportive)"],
        "entry": [
            "BULLISH (all 5 mandatory): daily MACD histogram > 0 and "
            "rising; weekly MACD histogram turning up after being down; "
            "daily RSI(14) > 40; daily Stochastic bullish cross from "
            "oversold (<20); daily price in the upper Bollinger Band "
            "zone (%B > 0.8).",
            "BEARISH is the exact mirror of all 5 conditions.",
            "Futures OI buildup and global risk-on/risk-off sentiment "
            "are SUPPORTIVE only \u2014 they boost confidence but never "
            "block a signal that already meets the 5 mandatory rules.",
        ],
        "exit": ["Feeds a normal BUY_CE/BUY_PE signal into the existing "
                 "option pipeline \u2014 same stop/target/trailing/EOD rules "
                 "as the core momentum strategy."],
        "params": {
            "mtf_confluence_enabled": "Master switch",
            "mtf_min_confidence": "Minimum confidence (%) required to trade",
            "mtf_max_trades_per_day": "Cap on entries per day, per symbol",
        },
    },
    "futures_signal": {
        "title": "Futures Signal (S4 Phase 2, hybrid entry engine)",
        "indicators": ["Regime + multi-timeframe confluence (same gate "
                      "every directional options strategy uses)",
                      "Current-month futures OI-buildup direction"],
        "entry": [
            "Base direction: today's regime allows the direction "
            "(trending-up=LONG only, trending-down=SHORT only) AND "
            "timeframe confluence agrees, at or above the configured "
            "minimum confidence.",
            "Futures-specific confirmation: the current-month contract's "
            "OWN OI-buildup direction. Only an ACTUAL CONFLICT blocks "
            "(e.g. short buildup opposing a LONG signal) \u2014 missing OI "
            "data skips this gate rather than blocking.",
            "Sized via risk-budget + margin (sizing.size_future), same "
            "minimum-lot fallback as options/spreads.",
        ],
        "exit": ["Direction-aware stop/target (futures_sl_pct/"
                 "futures_target_pct), trailing stop, portfolio kill-"
                 "switch, and 15:15 EOD square-off."],
        "params": {
            "futures_strategy_enabled": "Master switch",
            "futures_auto_deploy": "Let the engine enter automatically",
            "futures_require_oi_confirm": "Require the futures OI-buildup gate",
            "futures_min_regime_confidence": "Minimum regime confidence (%) to trade",
            "futures_cooldown_min": "Cooldown between entries per symbol (minutes)",
            "futures_max_trades_per_day": "Cap on entries per day, per symbol",
            "futures_sl_pct": "Stop-loss, % of entry price",
            "futures_target_pct": "Target, % of entry price",
            "futures_live_enabled": "SECOND switch required (with paper_mode off) for real orders",
        },
    },
}
