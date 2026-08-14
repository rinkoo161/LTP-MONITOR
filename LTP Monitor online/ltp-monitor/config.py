"""
Local settings store: saves credentials and trading preferences to
config.json next to the app, so nothing needs to be typed in the terminal.
Environment variables still work as fallback.

NOTE: config.json holds your API keys in plain text on YOUR machine.
Keep the folder private; don't sync it to cloud drives or git.
"""

import json
import os
import store
import threading

import pa_strategies

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(store.home(), "config.json")
_OLD_PATH = os.path.join(BASE, "config.json")
os.makedirs(os.path.dirname(PATH), exist_ok=True)
# one-time migration: settings previously lived inside the app folder and
# were lost on every version update — copy them to the persistent store
if os.path.exists(_OLD_PATH) and not os.path.exists(PATH):
    try:
        import shutil
        shutil.copy(_OLD_PATH, PATH)
    except Exception:
        pass
_lock = threading.Lock()

DEFAULTS = {
    "dhan_client_id": "",
    "dhan_access_token": "",
    # ---- MarketSense read-only bridge (2026-08-08) ----
    # MarketSense is the separate NSE-filings intelligence platform (own
    # process, :8100, own data sources). This link only POLLS its REST
    # API for display: events/watchlist/risk flags/levels onto the bus.
    # It never places orders and introduces no second broker — Dhan
    # remains the only broker in this app (operator constraint).
    "marketsense_enabled": True,
    "marketsense_url": "http://127.0.0.1:8100",
    "marketsense_poll_sec": 300,
    # Order gate fed by MarketSense's own risk verdicts (2026-08-08).
    # Only "hard_block" stops an order; "penalty"/"suppressed" are
    # recorded and do NOT block — treating an advisory downgrade as a
    # veto would hand a separate process the power to halt trading.
    # Telegram notifier + READ-ONLY chat (2026-08-08). Ships OFF, like
    # every other integration here, and needs a token pasted in Settings.
    # It cannot place, modify or exit an order — see telegram_bot.py.
    # NOTE data leaves the machine: positions, P&L and symbols are sent
    # to api.telegram.org. That is inherent to the feature, not a leak,
    # but it is why the default is False.
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_pnl_interval_min": 30,
    "telegram_alert_min_severity": "medium",
    "marketsense_risk_gate_enabled": True,
    # A flag is only honoured while the LINK is fresh. MarketSense
    # keeps the last good values on the bus through an outage, so
    # without an age limit a hard_block set before a crash would block
    # that symbol forever. 900s = 3x the 300s poll, so a single missed
    # poll does not disarm the gate. FAILS OPEN by design: an outage in
    # an optional advisory service must not stop trading, the same rule
    # the LLM layer already follows.
    "marketsense_max_flag_age_sec": 900,
    # ---- Session boundaries (NSE change effective 2026-08-03) ----
    # Index F&O now trades until 15:40 (was 15:30), while INTRADAY F&O
    # positions are auto-squared by the broker at 15:25. Those are two
    # different times and the code previously had one. Configurable
    # because this change is proof they get revised.
    #   fno_squareoff_time  we must be FLAT by here (margin before 15:25)
    #   fno_close_time      the market is still OPEN until here
    "fno_open_time": "09:15",
    "fno_squareoff_time": "15:22",
    "fno_close_time": "15:40",
    # 2026-08-04 — F&O stocks stop trading continuously at 15:15 and enter
    # the closing call auction. NIFTY/BANKNIFTY/FINNIFTY constituents ARE
    # F&O stocks, so from this minute the INDEX stops being discovered and
    # simply repeats its last value until the auction publishes the
    # official close (observed ~15:28, a ~150-point step on 2026-08-04).
    #
    # Verified against the broker, not inferred: Dhan returns 0 flat 1m
    # index bars for 15:15-15:30 on 2026-07-30 and 30 of 32 on 2026-08-03,
    # the first session under the new rules. The bars are REAL broker data
    # and are still stored — only INDICATOR input is filtered, because an
    # ATR or a ZigZag pivot computed across that step reads a 150-point
    # breakout that never traded.
    "cas_freeze_time": "15:15",
    # 2026-08-04, Phase 1 of the stock-options work. Underlyings whose
    # chains and futures candles are ARCHIVED so their liquidity can be
    # measured — and which are NEVER traded. Deliberately a SEPARATE key
    # from the bus "symbols" list: that list drives strategy, risk and
    # execution, so adding a name there would trade it. This one is read
    # only by the daily archiver.
    #
    # Validated against Dhan's scrip master on save (see
    # instrument_registry.validate) — a name with no options, or a typo,
    # is rejected with a reason rather than silently archiving nothing.
    # Promotion from here to the traded list is a separate, explicit
    # decision that should require evidence the instrument is tradable
    # for this system, not just that data exists.
    "watch_symbols": [],
    # 2026-08-06 — watchlist chain snapshots run SLOWER than the index
    # cadence (chain_snapshot_interval_sec, 60s) on purpose. The
    # option-chain endpoint is shared with the four traded symbols and
    # 429'd at 09:31 that morning; a name that is never traded must not
    # spend the budget the traded ones need.
    "watch_snapshot_interval_sec": 300,
    # 2026-08-06 — directional options had NO re-entry cooldown while
    # spreads, futures and news re-alerts all did. SENSEX 78700 CE
    # opened and closed five times in 38s off one stale analysis pack.
    "option_reentry_cooldown_sec": 180,
    # 2026-08-06 — nothing checked that a signal's entry price belonged
    # to the strike/leg it named. An AI signal priced SENSEX 78800 CE at
    # Rs 123.45 (the 79400 CE) while it traded at Rs 363.60. Below the
    # first figure the signal is left alone (normal 60s staleness);
    # between them stop/targets are rescaled with the risk-reward
    # preserved; above the second the signal is REJECTED rather than
    # laundered into a plausible-looking trade.
    "signal_entry_tolerance_pct": 10.0,
    "signal_entry_rescale_max_pct": 40.0,
    # 2026-08-06 — per-symbol HOLD. Data, analysis, regime, chain
    # snapshots and the archive all continue; only ENTRIES are refused,
    # so the evidence needed to decide whether to resume keeps
    # accruing. EXITS are never blocked. BANKNIFTY held on explicit
    # instruction: -Rs 40,781 at 28% win over 292 trades, and 0-for-3
    # since the per-trade caps — the worst symbol in both regimes.
    "paused_symbols": [],
    # 2026-08-06 — an AI auto-exit fired ONE SECOND after entry citing
    # "current position shows no profit", which is vacuous that early.
    # Advisory alerts are unaffected; only the AUTOMATIC exit waits.
    "option_ai_min_hold_sec": 120,
    # 2026-08-06 — floor used only when ATR is unavailable. 12 closes on
    # spot invalidation cost Rs 2,368 with a median hold of 82s; every
    # one came from a level set 0-8 points from spot on a 24,650 index.
    "signal_invalidation_min_pct": 0.15,
    "market_data_feed": "rest",  # "rest" (default, proven) or "websocket"
                                 # (dhan_ws.py — run test_dhan_ws.py against
                                 # a live account first, see its docstring)
    "anthropic_api_key": "",
    "theme": "dark",
    "paper_mode": True,          # simulate orders until explicitly disabled
    "auto_execute": False,       # autopilot may only place orders when True
    "min_confidence": 70,        # AI confidence needed to act
    # v59.83 signal repeat suppression. The 13 Aug journal published
    # NIFTY BUY_PE 24350 at 09:17/09:21/09:28/09:39/09:42 — one trade,
    # five risk evaluations. The 120s cooldown is shorter than every
    # one of those gaps and the 15-min backoff only arms on HARD
    # reject reasons, so neither caught it. A setup is re-published
    # only when something below actually moved. These are suppression
    # bounds, NOT entry criteria — loosening them cannot create a
    # trade that the risk gate would otherwise refuse, it can only
    # re-ask a question already answered.
    # v59.86 target reachability. analyzer.option_stop_geometry builds
    # target1 as entry x (1 + stop_pct x 2), so a wider stop mechanically
    # buys a more DISTANT target. Measured over 534 resolved shadow
    # signals, with RR median 2.00 in every bucket so hit rates compare
    # directly: a target needing <20% of premium was reached 47.0% of
    # the time (E[R] +0.307); 20-40% was reached 22.9% (E[R] -0.398),
    # and the median signal sat at 28.6% -- inside the worst bucket.
    # Holds independently in both halves of the sample. This is a
    # SUPPRESSION bound: raising it cannot create a trade the other
    # gates would refuse, only re-admit ones measured to lose. Set 0
    # to disable the check entirely.
    "signal_max_target_move_pct": 20.0,
    "signal_dedup_enabled": True,
    "signal_repeat_window_sec": 900,   # same setup stays suppressed this long
    "signal_repeat_conf_delta": 5,     # confidence points that count as news
    "signal_repeat_spot_move_pct": 0.15,   # % underlying move that re-opens it
    "signal_repeat_geometry_pct": 10.0,    # % move in entry/SL/T1 that re-opens it
    "max_trades_per_day": 3,     # hard cap for autopilot
    # DO NOT raise above the sum of the per-class budgets
    # (budget_futures/spread/option_daily_loss = 7,500). Those deliberately
    # sum to MORE than this global ceiling — see class_budget_blocked() —
    # so no ONE class can spend the whole day's allowance. Push this past
    # 7,500 and the sub-budgets become binding and the global ceiling can
    # never fire. It was briefly raised to 20,000 on 2026-08-02 on a bogus
    # comparison with portfolio_max_drawdown; that is an UNREALISED
    # force-close cap and this is a REALISED order-blocking limit. Not the
    # same scale, not orderable against each other. Reverted same day.
    "daily_loss_limit": 5000,    # ₹; risk agent blocks orders beyond this
    "daily_profit_target": 0,    # ₹; 0 = disabled. Once today's combined
                                 # P&L (trades + spreads) reaches this,
                                 # autopilot stops opening new positions
                                 # for the rest of the day — locks in a
                                 # good day instead of giving it back.
    # Transaction-level absolute-rupee SL/Target — an ADDITIONAL cap on
    # top of each position's own computed stoploss/target, expressed in
    # plain rupees per transaction rather than % of premium. 0 = disabled.
    "transaction_stop_loss_rupees": 0,
    "transaction_target_rupees": 0,
    # Rupee-based step-ratchet trailing (single-leg positions) — once
    # profit reaches `lock_trigger_rupees`, lock in `lock_profit_rupees`
    # as the floor; for every further `step_rupees` of profit gained
    # beyond that, raise the floor by `step_trail_rupees`. 0 = disabled
    # (falls back to the existing %-based trail_sl_* settings).
    "step_trail_enabled": False,
    "step_trail_lock_trigger_rupees": 2000,
    "step_trail_lock_profit_rupees": 1000,
    "step_trail_step_rupees": 1000,
    "step_trail_step_gain_rupees": 500,
    "cooldown_after_loss_min": 15,  # pause new signals for N minutes after any loss
    "stop_after_consecutive_losses": 2,  # stop autopilot after N losses in a row
    # 2026-07-27 — spreads never went through the directional pipeline's
    # own consecutive-loss halt at all (they bypass risk.evaluate()
    # entirely) — separate, per-(symbol,strategy) key rather than
    # reusing the one above, since a spread's wall-based edge is
    # symbol/strategy specific, not an account-wide signal-quality
    # signal the way the directional halt is.
    "spread_stop_after_consecutive_losses": 2,
    "regime_gate_enabled": True,   # block trades in choppy/rangebound regimes
    "require_tf_confluence": True, # require 1m/5m/15m to agree with signal direction
    # v59.0 (2026-08-02) — per-trade rupee cap on the OPTIONS path.
    #
    # RE-DERIVED on clean data. The first value (₹5,000) was calibrated
    # against a population that pooled spread legs with option buys, and
    # a tail figure computed by applying NIFTY's lot size to every
    # symbol — which inflated SENSEX risk 3.25x. On the clean 106-trade
    # single-leg population at each symbol's OWN lot size, per-lot risk
    # is median ₹2,519 and maxes at ₹4,059, so ₹5,000 never bound at all.
    #
    # ₹4,000 is not a new free parameter: it IS the existing per-trade
    # risk budget, risk_pct_per_trade (2%) x backtest_capital (200,000).
    # That budget is currently dead configuration, because
    # dynamic_sizing_enabled is False and size_option_buy() therefore
    # returns lots_per_trade without ever consulting it. This cap makes
    # the 2% budget actually bind.
    #
    # Deliberately NOT chosen by which historical trades lost. A ₹2,500
    # cap would have avoided ₹27,383 of losses in sample — and blocked
    # 57% of trades on n=106 with no demonstrated edge anywhere. Picking
    # a risk limit by back-fitted outcomes is the thing this engagement
    # exists to refuse.
    # DEFAULTS ships risk_pct_per_trade = 1.0, so the budget here is
    # 1% x 200,000 = 2,000 and the cap must match it. The running config
    # uses 2.0 and therefore 4,000 — an operator choice, not a
    # disagreement: the INVARIANT is cap == risk_pct x capital WITHIN
    # whichever config is in force, which sizing.risk_coherence() checks.
    # Lowered here rather than raising DEFAULTS' risk_pct deliberately: a
    # shipped default must not increase the risk a fresh install takes.
    "option_risk_per_trade_rupees": 2000,
    "lots_per_trade": 1,
    "max_concurrent_positions": 1,   # allow >1 to trade multiple indices at once
    "fee_per_lot": 40,
    "trail_sl_enabled": True,    # trail SL upward once trade is in profit
    "trail_sl_trigger_pct": 5,   # start trailing after +5% over entry
    "trail_sl_gap_pct": 10,      # keep SL 10% below the peak price
    # ATR-based stop/trail — an alternative to the fixed-% modes above.
    # Uses atr_pct (already computed by the regime engine: underlying
    # ATR as a % of spot) scaled onto the OPTION's own premium, since we
    # don't maintain a live per-strike ATR series. This is a practical
    # approximation, not a precise options-greeks translation — the
    # multiplier is there specifically so it can be tuned per how much
    # more volatile the premium moves vs. the underlying.
    "stop_mode": "fixed_pct",       # "fixed_pct" (default, entry*0.85 style)
                                    # or "atr" (entry - atr_pct%*multiplier)
    # 2026-08-02 — used by analyzer.option_stop_geometry() to turn index
    # ATR into an option stop. SEPARATE from atr_stop_multiplier, which
    # was applied to a dimensionally wrong expression (index ATR as a %
    # of SPOT, used as a fraction of PREMIUM) and therefore always hit
    # the lower clamp. 0.5 is chosen so the median NIFTY stop lands ~29%,
    # matching the 30% that dominated the journal — this consolidates the
    # rules WITHOUT shifting the typical risk level.
    # 1.7, derived 2026-08-03 from what regime.atr_pct ACTUALLY is.
    # It is _atr(c5, 14) — a FIVE-MINUTE ATR as a % of spot, median
    # 0.079% across the four indices (NIFTY 0.061, BANKNIFTY 0.082,
    # FINNIFTY 0.082, SENSEX 0.077), i.e. ~19.7 index points.
    # 0.5 was calibrated against DAILY ATR (~0.70%) and is wrong by ~3.4x
    # for this input: it produces an 8.8% stop that clamps to 9%, against
    # the 30% the fallback branch has been using. 1.7 reproduces ~30% at
    # median volatility, so wiring the ATR branch in changes WHICH
    # volatility a stop responds to, not the typical risk level.
    "option_stop_atr_mult": 1.7,
    "atr_stop_multiplier": 2.5,     # SL distance = entry * atr_pct% * this
    "trail_sl_mode": "fixed_pct",   # "fixed_pct" (trail_sl_gap_pct above)
                                    # or "atr" (peak - atr_pct%*multiplier)
    "atr_trail_multiplier": 1.5,    # trail gap = peak * atr_pct% * this
    "auto_strategies": [],       # strategy names auto-deployed when eligible
    "max_concurrent_spreads": 10, # cap on simultaneously open spreads (configurable in Settings)
    # 2026-07-27 — real gap found from a live data-driven review: the
    # ONLY existing limit on spread exposure was a COUNT
    # (max_concurrent_spreads) — with dynamic sizing on, the system
    # could keep opening spreads up to that count as long as margin
    # allowed, potentially committing the large majority of total
    # capital to spreads and leaving little room for directional
    # trades even on the rare occasions they DO clear the regime/
    # confluence gates. This caps the FRACTION of total capital that
    # can be tied up in spread margin at once — purely risk-REDUCING
    # (a new ceiling, never encourages more risk-taking), unlike
    # loosening the regime/confluence gates themselves, which is a
    # genuine risk-appetite decision this project isn't making
    # unilaterally.
    "max_spread_capital_pct": 60.0,
    "spread_reentry_cooldown_min": 15,
    "pa_min_trades_for_confidence": 15,  # min backtest trades before a version can go live
    "partial_day_min_coverage_pct": 60,  # v59.81 — a replayed day must carry at
                           # least this % of the MEDIAN day's bars for that
                           # symbol, or it is excluded and logged. A host sleep
                           # on 2026-08-13 left a 3.6h hole mid-session (243
                           # bars vs 1,078-1,391 on healthy days); replayed as a
                           # full day it would have counted as one independent
                           # day toward the promotion gate's 10-day minimum.
    "eod_max_price_age_sec": 900,  # v59.81 — if the last known price is older
                           # than this when the EOD square-off fires, the trade
                           # still closes (never leave a position open) but the
                           # fill is marked UNVERIFIED rather than booking a
                           # P&L that reads as real. The 2026-08-13 square-off
                           # ran on 6.5-hour-old quotes and recorded ₹12.
    "min_entry_runway_min": 30,   # v59.78 — minimum minutes to the forced
                           # square-off for a NEW entry, every instrument, live
                           # and replay. 2026-08-10's biggest loss entered at
                           # 15:13 against a 15:22 square-off: nine minutes of
                           # life, full round-trip cost, target unreachable by
                           # construction. 30 ≈ the median observed hold (12m)
                           # plus headroom — a trade needs at least typical
                           # time-to-target on the clock.
    "option_buy_require_regime_fit": False,  # v59.78 — directional option buys
                           # must match the regime (CE: trending-up/mixed, PE:
                           # trending-down/mixed), the gate spreads always had.
                           # Ships OFF: turning it on is a strategy decision,
                           # not a bug fix — see 2026-08-10 (PE and CE both
                           # bought around the same 24,600 pin in chop).
    "min_edge_cost_ratio": 2.0,   # v59.73 (Tier 2) — a trade is admissible only
                           # when its DESIGNED gross edge (its own target) is at
                           # least this multiple of the modelled round-trip
                           # cost. The restated record ran costs at 2.2× gross
                           # edge; below ~2× the counterparty is exchange
                           # infrastructure, and it always wins. Applied
                           # identically live and in the replays.
    "exit_min_cost_coverage": 1.0,  # v59.73 (Tier 2) — a profit-lock exit may
                           # only arm when the banked amount covers at least
                           # this multiple of its own round-trip cost.
    "closed_trades_memory_cap": 5000,  # v59.71 — in-memory closed_trades window
                           # (full history stays in trades.jsonl). Rescanned by
                           # five consumers per cycle; unbounded growth was a
                           # slow leak wearing an audit trail's clothes.
    "slippage_impact_alpha": 0.5,  # v59.69 — size impact exponent on the bid-ask
                           # component: halfspread(n) = halfspread_1 × n^alpha.
                           # 0 = linear (no impact, the old assumption), 0.5 =
                           # square-root (standard empirical form), 1.0 = walks
                           # the book proportionally. See size_aware_cost.py for
                           # the measured band this parameterises.
    "exit_retry_cooldown_sec": 30,  # v59.69 — after a live SELL fails/times out,
                           # refuse to re-place for this long (the first order
                           # may have filled; a 2s-cadence re-fire was a
                           # duplicate-order generator)
    "dhan_postback_secret": "",  # v59.79 — enables POST /api/dhan/postback/{secret}.
                           # Dhan's postback carries NO signature/HMAC (checked
                           # against their v2 docs), and it only lets you
                           # configure a URL — so the secret has to live in the
                           # PATH: the URL itself is the credential. Blank =
                           # endpoint disabled (503), which is the default.
                           # Only needed when this app is publicly reachable
                           # (ngrok/tunnel); the order-update websocket already
                           # delivers the same events with no exposure at all.
    "order_update_ws_enabled": True,  # v59.76 — Dhan order-update websocket
                           # (wss://api-order-update.dhan.co, client connects
                           # OUT — no postback URL needed). Live mode only; a
                           # belt on top of the polling confirm + reconciler.
    "broker_reconcile_interval_sec": 300,  # v59.69 — how often the LIVE book is
                           # compared against the broker's actual positions
                           # (paper mode: skipped, nothing to reconcile)
    "exit_quote_max_age_sec": 90,  # v59.69 — max quote age for EXIT decisions
                           # (stops/targets/trails). Entry gates had age checks;
                           # exits had none, and the feed's failure backoff
                           # reaches 300s. Older than this: hold, don't act.
    "gate_min_days": 10,   # v59.66 — min DISTINCT out-of-sample days before the
                           # promotion gate will score a strategy at all. The day
                           # is the independent observation (same-day trades share
                           # one regime); below this the verdict is "cannot
                           # evaluate", never "pass". Raising it tightens live
                           # promotion only — paper trading is unaffected.
    "pa_tuning_improvement_threshold": 0.15,  # new version must improve P&L by this fraction
    "pa_tuning_max_attempts": 4,      # consecutive non-improving attempts before pausing
    "pa_retune_cooldown_days": 7,     # days to wait after exhausting attempts
    "backtest_capital": 200000,      # assumed available capital for sizing/backtest context
    "margin_per_lot_spread": 85000,  # approx margin blocked per lot when SELLING a spread leg
                                      # (buying options only costs premium, already captured
                                      # via entry price × qty — this is for the sold leg)
    "dynamic_sizing_enabled": False,  # off by default — opt in explicitly
    "risk_pct_per_trade": 1.0,        # % of capital risked per trade when sizing is dynamic
    # MACD+Stoch Confluence strategy (rinkoo.docx, 2026-07-23) — daily/
    # weekly MTF confluence -> BUY_CE/BUY_PE. Requires Dhan as active broker.
    "mtf_confluence_enabled": True,
    "tradingview_webhook_secret": "",  # empty = webhook disabled (returns 503);
                                       # set a random string here and in your
                                       # TradingView alert's JSON payload
    "mtf_min_confidence": 70,          # below this, log but don't trade
    "mtf_max_trades_per_day": 1,       # per symbol
    "max_lots_per_trade": 10,         # hard cap regardless of the risk-budget math
    "portfolio_kill_switch_enabled": True,
    # 2026-08-02 — raised 5,000 -> 12,000. At the ₹4,000 per-trade cap a
    # ₹5,000 portfolio limit permitted 1.25 concurrent trades, i.e. it was
    # a single-trade stop wearing a portfolio label and it liquidated
    # winners to pay for losers (see the BANKNIFTY trade in ROADMAP:
    # +₹3,516 MFE, force-closed at -₹1,968). 12,000 permits 3 trades at
    # full permitted risk, which is what a whole-book limit should mean.
    # Only defensible because lots_per_trade is back at 1 — at 5 lots this
    # would have removed the last constraint on an oversized book.
    "portfolio_max_drawdown": 12000,
    # ---- S4 (v50): futures paper trading (Phase 1) ----
    # These MUST be registered here: config.save() silently drops any
    # key not present in DEFAULTS, so an unregistered setting can never
    # be persisted from the Settings page or the API.
    "margin_per_lot_future": 110000,   # SPAN+exposure approx per lot (index futures)
    "futures_sl_pct": 0.4,             # stop-loss, % of entry price (direction-aware)
    "futures_target_pct": 0.8,         # target, % of entry price (rr = 2.0, matching the house standard)
    "futures_trail_trigger_pct": 0.3,  # favourable move (%) before the trail activates
    "futures_trail_gap_pct": 0.2,      # trail distance (%) behind the best favourable price
    # 2026-07-28 — futures defense zone, mirroring spreads' own
    # "act before a full breach, not only at it" mechanism, adapted for
    # futures' linear (no-gamma) price/SL structure. Once an ADVERSE
    # move has consumed futures_defense_zone_pct of the ORIGINAL
    # entry-to-stop distance, the stop tightens to only allow
    # futures_defense_tighten_pct of the remaining room — a one-shot
    # tightening per position (never loosens), separate from the
    # existing favourable trailing mechanism above (which only engages
    # once already in profit; this engages while still losing, before
    # the original stop is reached).
    "futures_defense_enabled": True,
    "futures_defense_zone_pct": 40,     # once adverse move consumes this % of the
                                       # original entry-to-stop distance, tighten
    "futures_defense_tighten_pct": 50,  # tighten to this % of the remaining room
                                       # to the original stop
    # ---- Strategy 7 (v51): Structure-Gated EMA Cross ----
    # Same registration rule as above: unregistered keys are silently
    # dropped by config.save().
    "strategy7_enabled": True,
    "s7_ema_fast": 5,
    "s7_ema_slow": 13,
    "s7_mtf_confirm": 1,
    "s7_require_structure": True,
    "s7_require_ai_bias": True,
    "s7_min_ai_bias": 20,              # |Feature #2 bias score| threshold
    "s7_structural_stop_buffer_pct": 0.05,
    "s7_rr_target": 2.0,               # must stay >= risk gate's 1.95
    "s7_max_trades_per_day": 2,
    "s7_auto_deploy": False,           # off by default; paper-only in v51
    "s7_markers_enabled": True,
    "s7_show_rejected_markers": False,
    # ---- Strategy 8 (v58.28): EW-Reversal ----
    # Ported from the Avadhut Sathe "Get the Ultimate Edge" deck. Three
    # reversal detectors under ONE strategy id — see ew_reversal.py.
    #
    # Same registration rule as every block above: an unregistered key
    # is silently dropped by config.save(), AND silently dropped a
    # layer earlier by SettingsIn — so every key here also has a
    # matching field in app.py's SettingsIn (test_settings_model_sync
    # fails loudly if that ever drifts).
    #
    # strategy8_enabled defaults TRUE so the strategy is evaluated and
    # visible (eligibility card, Shadow Journal) from day one, while
    # s8_auto_deploy defaults FALSE so it can never actually fire —
    # the exact two-key posture Strategy 7 shipped with. A brand-new
    # detector set ported from a workshop deck has no backtest history
    # in this system yet; it earns auto-deploy after the journal shows
    # it firing on sane bars, not before.
    "strategy8_enabled": True,
    "s8_auto_deploy": False,           # off by default; paper-only
    "s8_ending_diagonal_enabled": True,
    "s8_hs_enabled": True,
    "s8_failed_hs_enabled": True,
    "s8_zigzag_deviation_pct": 0.5,    # matches the chart's own ZigZag
    "s8_require_macd_divergence": True,
    "s8_require_tide": True,           # failed-H&S only; skipped when 15m absent
    "s8_min_pattern_bars": 12,
    "s8_shoulder_tol_pct": 1.5,
    "s8_neckline_buffer_pct": 0.05,
    "s8_stop_buffer_pct": 0.05,
    "s8_rr_target": 2.0,               # must stay >= risk gate's 1.95
    "s8_max_trades_per_day": 2,
    "s8_markers_enabled": True,
    # v58.29 — the deck states plainly that H&S FAILS when the Tide is
    # against it. v58.28 enforced that only inside failed_hs, so a
    # plain H&S could fire a short into a rising Tide. Default ON
    # because it is what the source document actually specifies and S8
    # has never traded (s8_auto_deploy has been off since it shipped);
    # set to False to reproduce v58.28 behaviour exactly.
    "s8_require_tide_all_detectors": True,
    "s8_use_shared_tide": True,        # read the Tide from TAElliottAgent
    # ---- Strategy 9 (v58.29): TA with Elliott ----
    # The deck's slide 10 ("Marrying TA with Elliot") plus slides
    # 11-18 and 28: Bollinger band direction as the impulse-vs-
    # corrective classifier, GMMA compression/expansion, MACD zero-line
    # reversal, reverse (hidden) divergence, RSI divergence, ADX.
    # Runs in its OWN agent (TAElliottAgent, 180s) so it does not
    # inflate PriceActionAgent's 60s six-strategy loop.
    #
    # Same registration rule as every block above: an unregistered key
    # is dropped silently by config.save() AND a layer earlier by
    # SettingsIn — every key here has a matching SettingsIn field.
    "ta_elliott_enabled": True,        # master switch; state still publishes
    "ta_auto_deploy": False,           # off by default; paper-only
    "ta_min_confluence": 3,
    "ta_require_tide": True,
    "ta_bb_period": 20,
    "ta_bb_stdev": 2.0,
    # v58.41 — lowered from 0.0004, measured. Across 211 real
    # observations price tagged a Bollinger band 33 times and the slope
    # NEVER exceeded 0.0004 in the tag direction, so IMPULSE classified
    # 0.0% of the time. At NIFTY 24,200 that threshold demanded the
    # 20-bar mid move ~10 points per 5m bar. Re-check against the new
    # `abs_bb_slope` percentiles in the calibration export.
    "ta_bb_slope_eps": 0.00015,
    "ta_gmma_compression_pct": 25.0,
    # v58.41 — GMMA moved to 1m. On 5m it needs 65 bars = 325 minutes
    # of a 375-minute session, so it returned "not computable" on 79.1%
    # of real observations and `gmma_expansion` could not fire at all.
    # The 30-60 bank is a DAILY-chart calibration; on 1m the same
    # periods give a 60-minute lookback, available from ~10:15.
    "ta_gmma_timeframe": "1m",

    # v58.41 — the strike gate's message said "not OTM" while the
    # condition enforced at-or-IN-the-money. 8 rejections/day were
    # therefore unreadable. Policy made explicit; default preserves the
    # existing behaviour exactly. "atm_or_otm" flips it, "any" disables.
    "option_strike_policy": "atm_or_itm",
    # v58.44 — the RR floor the LLM prompt already states but nothing
    # enforced. Kept slightly above the RiskAgent's 1.95 gate so a
    # repaired signal clears it rather than landing exactly on it.
    "signal_min_rr": 2.0,
    # v58.47 — master switch for live index-level exit conditions
    # (momentum_confluence's MACD-histogram turn today). Off restores
    # the fixed-levels-only behaviour every other PA strategy uses.
    "dynamic_exits_enabled": True,

    # ---- Strategy 10: OI Buildup/Covering Composite (v58.65) ----
    # The operator's own methodology. Produces a COMPOSITE position
    # (future + credit spread + long option) from one option-chain
    # condition, and exits its legs INDEPENDENTLY -- neither of which
    # any existing strategy here could do. Observe-only on introduction.
    "oi_composite_enabled": True,
    "oi_composite_auto_deploy": False,
    "oi_composite_risk_pct": 2.0,          # of total capital, per composite
    "oi_composite_max_concurrent": 1,      # what the 2% arithmetic allows
    "oi_composite_rr_target": 3.0,         # the "1:3" reading of 1:3-1:5
    "oi_composite_cost_per_leg": 50.0,
    "oi_composite_cost_is_per_lot": 0,
    "oi_composite_otm_strikes_checked": 2,
    "oi_composite_spread_width_strikes": 3,
    "oi_composite_require_churn_filter": 1,
    "oi_composite_min_delta_for_long_leg": 0.45,
    "oi_composite_condor_enabled": 1,
    "oi_composite_max_trades_per_day": 3,
    # v58.41 — websocket subscription chunking. 2,080 individual frames
    # in a burst got the connection torn down without a close handshake
    # ("no close frame received or sent"), which is the source of the
    # socket.send() console errors.
    "ws_subscribe_chunk_size": 100,
    "ws_subscribe_delay_ms": 250,
    "ta_adx_dynamic_min": 20.0,
    "ta_rsi_period": 14,
    "ta_zigzag_deviation_pct": 0.5,
    "ta_stop_buffer_pct": 0.05,
    "ta_rr_target": 2.0,               # must stay >= risk gate's 1.95
    "ta_max_trades_per_day": 2,
    "ta_require_corrective_phase": False,  # strict reading: demand a positive
                                          # CORRECTIVE label, not merely
                                          # "not an impulse". Off by default -
                                          # measured at ~2% of bars, which made
                                          # the strategy unable to trade at all.
    "ta_tide_use_15m": False,
    "ta_calibration_logging": True,    # persist one observation per cycle so
                                      # the confluence signals can be
                                      # calibrated against REAL days
    "ta_calibration_retention_days": 10,
    # ---- Rupee profit floor (v58.35) ----
    # One ratchet in RUPEES for options, spreads and futures. Every
    # percentage-denominated protection failed to arm on 2026-07-29
    # (₹31,278 given back, 43.9% capture) while every rupee-denominated
    # exit captured its full peak. See agents.rupee_profit_floor().
    "rupee_profit_floor_enabled": True,
    "rupee_profit_floor_arm_rupees": 750,    # arm once peak P&L reaches this
    "rupee_profit_floor_keep_pct": 60,       # keep this % of the peak
    "rupee_profit_floor_min_rupees": 300,    # floor must be worth exiting for
    # 2026-08-01 — express an armed futures floor as a stop PRICE rather
    # than leaving it a per-cycle P&L comparison. As a P&L test it fires
    # wherever the cycle lands once pnl <= floor: four exits quoted the
    # floor they were protecting (₹2310, ₹1551, ₹825, ₹495) while booking
    # -₹3,000, -₹1,980, -₹1,500 and -₹2,340 gross. As a price it becomes
    # the stop, which is evaluated first in the exit chain.
    "rupee_profit_floor_as_stop": True,
    # ---- authentication (v58.74) ----
    # OFF by default and that is deliberate: enabling auth in a process
    # already holding live positions, before any account exists, locks
    # the operator out of their own running system. Create the admin at
    # /setup, enroll the authenticator, THEN turn this on.
    # ---- futures cost model (v59.0 Phase 0 §3.2) ----
    # fee_per_lot is a flat per-lot charge — an options-shaped assumption.
    # Futures STT is a PERCENTAGE OF NOTIONAL: one NIFTY lot at 24,800 is
    # ₹18.6 lakh of notional and ~₹372 of sell-side STT alone, against a
    # model charging ₹40 for the whole round trip. is_live_enabled() reads
    # backtest profitability, so the flat model is a live-promotion risk.
    # Clamped on READ in futures_costs.rate(), not only on write.
    # ---- basis residual (v59.0 Phase B §5) ----
    # The dashboard already shows raw basis, which is mostly cost of carry
    # and therefore tells you the calendar rather than the positioning.
    # The residual is the part carry does not explain. q is NOT allowed to
    # default to zero: NIFTY ex-dates cluster Feb-Aug and a zero
    # assumption biases the residual the same way every year, so the
    # estimate below is used and the payload is stamped approx=True.
    # Real per-index dividend yield (%) over the remaining contract life,
    # when a calendar is available: {"NIFTY": 1.4, ...}. Empty by default,
    # which makes basis_residual fall back to the estimate below AND stamp
    # approx=True. Registered here because config.save() drops unknown
    # keys silently — without this line the calendar hook was unreachable
    # and the estimate would have been used forever while looking optional.
    "index_dividend_calendar": {},
    # The residual as an optional VETO gate (§5). Default off everywhere:
    # this ships as an observation first. A per-strategy key wins over the
    # global one, so "all strategies" is covered without a key per
    # strategy forever. The gate may only veto — it can never be the
    # reason a trade happens, nor bypass an existing risk gate.
    "require_basis_agreement": False,
    "s11_require_basis_agreement": False,
    "s12_require_basis_agreement": False,
    "s13_require_basis_agreement": False,
    "s14_require_basis_agreement": False,
    "futures_require_basis_agreement": False,

    "fut_financing_rate_pct": 6.5,
    "fut_dividend_yield_pct": 1.2,
    "fut_residual_z_window": 200,
    "fut_brokerage_per_order": 20.0,
    "fut_stt_sell_pct": 0.0002,
    "fut_exchange_txn_pct": 0.0000173,
    "fut_sebi_turnover_pct": 0.000001,
    "fut_stamp_duty_pct": 0.00002,
    # v59.68 (third-eye Tier 0) — the OPTION cost rates. options_costs.py
    # claimed "config-driven with read-time clamping" but none of its
    # seven opt_* keys were registered here, so config.save() silently
    # dropped any tuned value and the module defaults always won — the
    # half-spread (the largest single component) was untunable in
    # practice. Defaults mirror options_costs.py's own; the read-time
    # clamps there still bound whatever is set here.
    "opt_brokerage_per_order": 20.0,
    "opt_stt_sell_pct": 0.001,        # sell-side premium
    "opt_exchange_txn_pct": 0.0005,
    "opt_sebi_turnover_pct": 0.000001,
    "opt_stamp_duty_pct": 0.00003,    # buy side
    "opt_gst_pct": 0.18,
    "opt_halfspread_points": 0.5,     # per leg per transaction; measured median 0.325
    "fut_gst_pct": 0.18,
    "fut_slippage_points": 1.0,
    "auth_enabled": False,
    "auth_require_mfa": True,      # a password alone is not a second factor
    "auth_session_hours": 12,
    "auth_max_failed": 5,          # attempts before a temporary lockout
    "auth_lockout_minutes": 15,
    # Secure cookies require HTTPS. This app is normally reached over
    # plain HTTP on a LAN (HOST=0.0.0.0), where setting Secure would stop
    # the cookie being sent at all and make login silently fail. Turn it
    # on only behind a TLS terminator.
    "auth_cookie_secure": False,
    "ai_exit_advisory_logging": True,        # log every advisory, acted on or not
    # ---- AI advisory cadence (v58.36) ----
    # Event-driven, not clock-driven. A flat 5-minute poll was too slow
    # (positions peaked and gave it back inside one window) but simply
    # lowering it breaks the ai_daily_call_cap: 60s x 4 positions is
    # ~1,500 calls against a cap of 400, and 10 spreads makes it 3,750.
    # So: a hard floor even in danger, a material-move trigger measured
    # against the position's OWN risk, and a periodic fallback.
    "ai_exit_advisory_danger_interval_sec": 20,   # hard floor, protects the cap
    "ai_exit_advisory_min_interval_sec": 45,      # floor for move-triggered calls
    "ai_exit_advisory_max_interval_sec": 300,     # periodic review when quiet
    "ai_exit_advisory_move_trigger_pct": 25,      # % of risk moved -> review
    "ai_exit_advisory_giveback_trigger_pct": 30,  # % of peak given back -> review

    # ---- Futures overhaul (v58.39) ----
    # Diagnosis from 40 live trades: 27.5% win, payoff 0.77, expectancy
    # -₹597. ZERO trades reached target, ONE reached its own stop, and
    # 11 were closed by the portfolio kill-switch for -₹21,215 (89% of
    # all futures losses). The per-trade stop could never bind because
    # a single stop (₹7,468) exceeded the whole daily loss limit.
    "futures_symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY"],   # SENSEX dropped
    "futures_stop_mode": "atr",          # "atr" | "pct"
    "futures_atr_period": 14,
    "futures_atr_stop_mult": 1.5,        # stop = 1.5 x ATR
    "futures_atr_target_mult": 2.75,     # target = 2.75 x ATR -> ~1.83 payoff
    "futures_risk_per_trade_rupees": 2500,   # HARD ceiling on one trade's loss
    "futures_min_adx": 22,               # trend-strength gate

    # ---- Separate risk budgets (v58.39) ----
    # Spreads (+₹15,235), option buys (+₹4,657) and futures (-₹23,863)
    # shared ONE daily_loss_limit, so the losing class spent the
    # winners' allowance. Sub-budgets deliberately sum ABOVE the global
    # limit: the global stays the hard ceiling, these only stop any one
    # class consuming all of it.
    "risk_budgets_enabled": True,
    "budget_futures_daily_loss": 2500,
    "budget_spread_daily_loss": 3000,
    "budget_option_daily_loss": 2000,

    # ---- Per-class profit floor (v58.39) ----
    # v58.35 applied one ratchet to everything. Correct for a
    # directional buy, wrong for a credit spread: a spread decaying
    # normally gives back 40% of an intraday mark peak routinely, and
    # exiting there converts theta collection into a scalp paying four
    # legs of fees. Spreads arm later and keep more.
    "rupee_profit_floor_arm_rupees_spread": 2000,
    "rupee_profit_floor_keep_pct_spread": 75,
    "rupee_profit_floor_min_rupees_spread": 800,
    "rupee_profit_floor_arm_rupees_futures": 750,
    "rupee_profit_floor_keep_pct_futures": 55,
    "rupee_profit_floor_min_rupees_futures": 300,
    "rupee_profit_floor_arm_rupees_option": 600,
    "rupee_profit_floor_keep_pct_option": 60,
    "rupee_profit_floor_min_rupees_option": 250,             # 15m Tide is a 195-min lookback in a
                                          # 375-min session; see ta_elliott
                                          # .tide_of() for the measurement.
    # ---- Futures delta hedge — SHADOW ONLY (v59.0 Phase D) ----
    # Nothing here places an order in live OR paper. fhedge_shadow.py
    # only records what a hedge would have done against the real S5/S6
    # spreads. 40 sessions of this before paper orders are discussed.
    "fhedge_shadow_enabled": True,
    "fhedge_trigger_buffer_pct": 0.10,
    "fhedge_max_lots": 2,
    # item 28 — below this parent size a hedge is directional, not risk
    # reduction. See fhedge_shadow.DEFAULTS for the delta arithmetic.
    "fhedge_min_parent_lots": 3,
    # ---- Snapshot retention (v53; tiered in v59.0 item 18) ----
    # chain_snapshot_retention_days is RETAINED but no longer consulted by
    # LearningAgent — it was a 5-day hard delete, and it is the reason no
    # historical premium exists to reprice the replays against. Kept as a
    # registered key so an explicit call can still hard-delete.
    "chain_snapshot_retention_days": 5,
    # Tiering: full detail, then 5-minute, then daily close. ~850 MB +
    # ~1.3 GB steady state; see history.prune_chain_snapshots().
    "chain_tier1_days": 90,
    "chain_tier2_days": 730,
    "chain_tier2_interval_sec": 300,
    # ---- PA strategies auto-deploy list (v55.1) ----
    # Was previously read ONLY via cfg.get("pa_enabled",
    # list(pa.PA_NAMES)) with no registered default and no way to
    # toggle a single strategy without touching config.json directly —
    # the Strategies-page table's Auto Deploy checkbox for orb/
    # vwap_pullback/ema_mtf was read-only for exactly this reason.
    #
    # 2026-07-27 — real gap found: this was a HARDCODED list, separate
    # from pa_strategies.PA_NAMES — and had already silently drifted
    # (missing sg_ema's addition for a while, then caught again when a
    # new strategy, momentum_confluence, was added and this list still
    # didn't include it). Derives from PA_NAMES dynamically now instead
    # of a manually-maintained list, so any future new PA strategy is
    # automatically included here with nothing else to remember.
    "pa_enabled": list(pa_strategies.PA_NAMES),
    # ---- v58 hygiene: two flags read via cfg.get(key, True) that were
    # never registered here, meaning config.save() would silently drop
    # any attempt to persist a non-default value for either — same
    # pattern already fixed for pa_enabled (v55.1). No Settings-page
    # UI currently exposes either, so this is a latent gap rather than
    # a live bug, closed now rather than left for a future session to
    # rediscover. Defaults match the inline fallback exactly.
    "ai_decision_engine_enabled": True,
    "learning_feedback_enabled": True,
    # ---- S4 Phase 2 (v52): futures entry-signal engine ----
    # Hybrid design (explicit decision): base direction from the SAME
    # regime+confluence gate every directional options strategy already
    # uses, confirmed by a futures-specific gate (current-month futures
    # OI buildup) that only ever BLOCKS on an actual conflict — missing
    # OI data skips the gate rather than blocking, same convention as
    # every other gate in this codebase.
    "futures_strategy_enabled": False,  # 2026-07-27 — changed from True to
    # False after real trading data showed every futures trade closing at
    # a loss or exact breakeven (all via forced kill-switch closure, none
    # via their own profit target). Futures OI-buildup/price data is still
    # collected and used as a supportive input for other strategies
    # (MTF Confluence) regardless of this flag — only the futures SIGNAL
    # ENGINE (auto-deploy + manual "Fire Now") is gated by it.
    "futures_auto_deploy": False,       # off by default
    "futures_min_regime_confidence": 60,
    "futures_require_oi_confirm": True,
    "futures_cooldown_min": 30,
    "futures_max_trades_per_day": 2,
    "futures_live_enabled": False,      # explicit second gate below paper_mode
                                       # positions+spreads that force-closes everything
                                       # (separate from daily_loss_limit, which only
                                       # gates new entries against REALIZED P&L —
                                       # this catches a correlated shock mid-event,
                                       # the gap our regression testing surfaced)
    "portfolio_halt_cooldown_min": 60,  # after a kill-switch trip, block new
                                        # entries for this long before resuming
    "time_stop_minutes": 0,  # exit any position/spread still open after this
                             # many minutes, regardless of P&L — 0 disables it.
                             # Addresses the observation that some trades were
                             # held indefinitely waiting for a target that
                             # never came; a time stop forces a decision.
    "spread_defense_enabled": True,
    # 2026-07-27 (item 9) — Liquidity-sweep/FVG confluence on top of
    # the existing OI-wall selection. A genuine new entry requirement,
    # not a bug fix, so it's opt-in (off by default) rather than
    # silently changing behavior for anyone already running these
    # strategies.
    "spread_require_liquidity_confluence": False,
    "spread_liquidity_proximity_pct": 0.3,
    "spread_profit_target_pct": 18,   # close spread at this % of credit.
                                       # Was 30 — live data (69 trades to
                                       # 2026-07-23) showed spread P&L peaks
                                       # cluster at 15-25% of credit, so a 30%
                                       # target was rarely reachable and most
    # 2026-07-27 — dynamic, IV-based profit targets, per explicit
    # request: the flat target above is conservative and keeps win
    # rate high, but leaves upside on the table on days IV genuinely
    # supports capturing more. When enabled, these four replace the
    # flat target above at spread ENTRY time (locked in for that
    # trade's lifetime, same as loss_limit already is) based on the
    # IV percentile (or absolute IV level if no percentile history
    # exists yet) at the moment of entry. Disabled by default —
    # a real, tested behavior change to how spreads exit, opt-in
    # rather than silently changing existing behavior.
    "dynamic_spread_targets_enabled": False,
    "spread_target_low_iv_pct": 20.0,
    "spread_target_normal_iv_pct": 30.0,
    "spread_target_elevated_iv_pct": 40.0,
    "spread_target_elevated_iv_stable_pct": 50.0,
                                       # trades never exited cleanly. The 8
                                       # trades that DID hit target captured a
                                       # median ~15% of credit and averaged
                                       # +₹390 each (100% win rate). 18% is set
                                       # just under that observed median so
                                       # target-hits become the normal exit.
    "spread_profit_lock_trigger_pct": 80,  # once P&L reaches this % of the
                                            # profit target, start locking gains.
                                            # Was 50 — combined with the old 30%
                                            # target that armed the ratchet at
                                            # just 15% of credit, i.e. at or
                                            # below the typical peak, so it fired
                                            # on essentially every trade and
                                            # exited on the first tick of
                                            # pullback. Result: 26 "profit lock"
                                            # exits netted ₹62 TOTAL (₹2/trade,
                                            # 35% win rate) while the 8 trades
                                            # allowed to reach target made ₹3119.
                                            # The ratchet must be a late safety
                                            # net, not the primary exit.
    "spread_profit_lock_pct": 75,     # keep this % of the peak P&L once
                                       # triggered (was 60 — gave back too much)
    "spread_profit_lock_min_rupees": 250,  # ratchet will NOT exit for less than
                                            # this absolute ₹ profit. 17 of 26
                                            # ratchet exits peaked below ₹4/share
                                            # — noise-level moves where fees
                                            # (₹40/lot × lots × 4 legs) ate the
                                            # entire gain. Below this floor the
                                            # trade is left to its normal target
                                            # / loss-limit / breach rules.
                                       # captured (was a fixed 60% that
                                       # never fired intraday — see note
                                       # at the spread-open call site)
    "spread_loss_limit_multiple": 1.0,  # close spread at this multiple
                                         # of credit lost (capped at
                                         # max_loss either way)
    "spread_ai_auto_exit_enabled": False,  # AI HOLD/EXIT advisory is passive by
                                            # default (alert only) — enable to let
                                            # a confident EXIT call actually close
                                            # the spread, not just notify about it
    "spread_ai_exit_confidence_threshold": 75,
    # 2026-07-28 — per explicit request, the SAME AI HOLD/EXIT
    # advisory pattern spreads already had, extended to single-leg
    # option positions ("open trade") and futures positions — both
    # had no equivalent advisory at all until now. Same conservative
    # design: passive/alert-only by default, each with its own
    # separate auto-exit opt-in rather than inheriting spreads' switch.
    "option_ai_auto_exit_enabled": False,
    "option_ai_exit_confidence_threshold": 75,
    "futures_ai_auto_exit_enabled": False,
    "futures_ai_exit_confidence_threshold": 75,
    "spread_defense_zone_pct": 30,  # once spot is within this % of the
                                    # spread's width from the short strike
                                    # (but hasn't breached it yet), tighten
                                    # the loss limit rather than waiting for
                                    # a full breach — addresses the
                                    # observation that spreads need defense
                                    # rules BEFORE the short strike is hit,
                                    # not only a hard exit once it is
    "spread_defense_tighten_pct": 50,  # tighten loss_limit to this % of
                                       # its current value when defense fires  # wait after closing before re-entering same setup
    "broker": "dhan",            # dhan | zerodha | kotak — active data+order broker
    "zerodha_api_key": "",
    "zerodha_access_token": "",  # regenerate daily via Kite login flow
    "kotak_consumer_key": "",
    "kotak_access_token": "",
    "kotak_sid": "",
    "kotak_mobile": "",
    "kotak_ucc": "",
    "kotak_session_token": "",
    "kotak_base_url": "",
    "kotak_auth_token": "",           # ₹ brokerage+charges per lot per transaction
                                 # (entry and exit each count as one transaction)
    # verify lot sizes with your broker; exchanges revise them periodically
    # v59.0 item 32 (2026-08-01) — corrected to the Dhan scrip master.
    # Was NIFTY 75 / FINNIFTY 65 while the live contracts were 65 / 60.
    # Exchange lot sizes are revised periodically to hold contract value
    # in a band, so this map goes stale silently; futures_costs.
    # reconcile_lot_sizes() now surfaces the drift on a daily schedule.
    "lot_sizes": {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "SENSEX": 20},
    # ---- AI engine + cost controls ----
    "ai_engine": "local",        # local (Ollama) | online (Anthropic) | auto | off
    "ollama_model": "qwen2.5:3b", # DEFAULT: lightweight ~2GB. Safer options:
                                  # qwen2.5:1.5b (~1GB, works on 8GB Macs),
                                  # llama3.2:3b (~2GB). AVOID llama3.1 (8B)
                                  # on <16GB Macs — it freezes the machine.
    "ollama_num_thread": 4,      # max CPU cores Ollama can use (out of your total)
    "ollama_num_ctx": 2048,      # context window (smaller = less RAM)
    "ollama_keep_alive": "2m",   # unload model after 2 min idle to free RAM
    "ollama_timeout": 60,        # seconds; fail fast if machine thrashes
    "ai_enabled": True,          # kept for back-compat; off == ai_engine "off"
    "ai_active_only": True,      # only call AI for the symbol you're viewing
    "ai_min_interval": 180,      # min seconds between AI calls per symbol (cache TTL)
    "ai_daily_call_cap": 400,    # hard stop on LLM calls per day across everything
    "ai_signal_on_change_only": True,  # skip AI if chain/bias barely moved
    "news_block_minutes": 20,    # how long a news risk event blocks trades
    # 2026-07-27 — AI-based semantic news classification (item 10,
    # round 2): keyword matching alone can't tell "war mentioned as the
    # actual bearish subject" from "war mentioned in passing while the
    # headline is really a bullish stock recommendation." Enabled by
    # default with a conservative daily budget separate from the
    # trading-signal AI cap, since news volume and signal volume are
    # unrelated quantities.
    "news_ai_classification_enabled": True,
    "news_ai_classification_daily_cap": 150,
    "news_realert_cooldown_minutes": 60,  # don't re-alert on the same
                                           # ongoing risk event more often
                                           # than this, regardless of how
                                           # the LLM rewords it each cycle
    # ---- News/Macro Agent (global markets + macro events) ----
    "twelve_data_api_key": "",   # Twelve Data — equity indices (US/Asia)
    # ---- Macro data providers (2026-08-02 refactor) ----
    # Canonical key -> per-provider ticker. IN CONFIG, not hardcoded in the
    # fetch logic, so a provider change is a settings edit. Downstream
    # consumers only ever see the canonical key.
    #
    # `freshness_sec` is per symbol on purpose: an e-mini trades around the
    # clock and is stale after minutes, while a CASH index between 09:15
    # and 15:30 IST is EXPECTED to be hours old — it returns the previous
    # US close. Flagging that every cycle would train people to ignore the
    # flag, so cash gets a long threshold and is labelled instead.
    #
    # Twelve Data has entries ONLY for FX/metals: its free tier does not
    # serve indices, which is what produced the SPX/DJI 404s. Alpha
    # Vantage entries are (from, to) for its FX function or a bare
    # function name for its commodity endpoints.
    "macro_symbols": {
        "SPX_FUT":    {"yf": "ES=F",      "stooq": "es.f",   "freshness_sec": 1200},
        "NDX_FUT":    {"yf": "NQ=F",      "stooq": "nq.f",   "freshness_sec": 1200},
        "DJI_FUT":    {"yf": "YM=F",      "stooq": "ym.f",   "freshness_sec": 1200},
        "RUT_FUT":    {"yf": "RTY=F",                        "freshness_sec": 1200},
        "NIKKEI":     {"yf": "^N225",     "stooq": "^nkx",   "freshness_sec": 21600},
        "HSI":        {"yf": "^HSI",      "stooq": "^hsi",   "freshness_sec": 21600},
        "GOLD":       {"yf": "GC=F",      "stooq": "xauusd",
                       "td": "XAU/USD",   "av": ["XAU", "USD"], "freshness_sec": 900},
        "SILVER":     {"yf": "SI=F",      "stooq": "xagusd",
                       "td": "XAG/USD",   "av": ["XAG", "USD"], "freshness_sec": 900},
        "CRUDE_WTI":  {"yf": "CL=F",      "stooq": "cl.f",   "av": "WTI",
                       "freshness_sec": 900},
        "CRUDE_BRENT": {"yf": "BZ=F",     "stooq": "cb.f",   "av": "BRENT",
                        "freshness_sec": 900},
        "USDINR":     {"yf": "USDINR=X",  "stooq": "usdinr",
                       "td": "USD/INR",   "av": ["USD", "INR"],
                       "fx": ["USD", "INR"], "freshness_sec": 900},
        "DXY":        {"yf": "DX-Y.NYB",                     "freshness_sec": 1800},
        "NIFTY":      {"yf": "^NSEI",     "stooq": "^nsei",  "freshness_sec": 900},
        "BANKNIFTY":  {"yf": "^NSEBANK",                     "freshness_sec": 900},
        "INDIAVIX":   {"yf": "^INDIAVIX",                    "freshness_sec": 1800},
        # Cash indices: kept for post-close context ONLY. Stale during the
        # IST session by construction — see freshness_sec.
        "SPX_CASH":   {"yf": "^GSPC",     "stooq": "^spx",   "freshness_sec": 86400},
        "DJI_CASH":   {"yf": "^DJI",      "stooq": "^dji",   "freshness_sec": 86400},
    },
    # Hard local ceiling, checked BEFORE the request. AV's free tier is 25
    # a day; 20 leaves headroom for a manual probe.
    "alpha_vantage_daily_budget": 20,
    # Hourly bars: a DAILY bar is stamped at the bar's start, which makes
    # every quote read stale. See YFinanceProvider.
    # Per-symbol provider detail at DEBUG; the one-line cycle summary is
    # always at INFO. Off by default — this is a high-volume log path.
    "macro_debug_logging": False,
    # Repeating futures refresh during 09:15-15:30 IST. MUST stay below
    # the futures freshness threshold in macro_symbols (900s) or the
    # sentiment input flickers between a value and None as quotes age out.
    # Active providers, IN ORDER. Stooq is deliberately absent: it stopped
    # serving CSV (its /q/l/ endpoint 404s, /q/d/l/ answers with a
    # JavaScript anti-bot challenge), so enabling it costs a wasted
    # request per symbol per cycle for a provider that cannot succeed.
    # Add "stooq" back here if it ever starts serving again.
    "macro_providers_enabled": ["yf", "ecb", "td", "av"],
    "macro_intrasession_enabled": True,
    "macro_intrasession_refresh_sec": 300,
    # 5-MINUTE bars, not hourly. A bar is stamped at its START, so the
    # freshest age an interval can produce is about the interval itself —
    # an interval COARSER than a symbol's freshness_sec makes that symbol
    # read STALE permanently. Measured live in the 2026-08-03 session:
    #   1h  -> median futures age 51.3m, 0/4 fresh
    #   15m -> 21.3m, 0/4 fresh
    #   5m  -> 11.4m, 4/4 fresh
    # At 1h the e-minis were stale ALL SESSION, so replacing the cash
    # indices with futures delivered nothing. Checked by
    # macro_providers.interval_coherence().
    "macro_yf_interval": "5m",
    "macro_yf_period": "5d",
    "macro_cache_ttl_open": 600,
    "macro_cache_ttl_closed": 3600,
    "alpha_vantage_api_key": "", # Alpha Vantage — commodities/FX (tight free-tier budget)
    "newsapi_api_key": "",       # NewsAPI.org — macro/geopolitical/constituent news
    # ---- Option Chain Intelligence Engine (Feature #4) ----
    "chain_snapshot_interval_sec": 60,  # how often per-strike option-chain
                                        # snapshots are persisted for
                                        # change-vs-previous-snapshot and
                                        # change-vs-session-open calculations;
                                        # spec allows 30/60/300/900 (30s/1m/5m/15m)
    # ---- Live Chart history lookback (2026-07-27) ----
    # Real gap found from a live report: the chart's own history query
    # hard-cut at "today only" even though the DB genuinely has
    # multi-day history (the per-tick candle builder runs continuously
    # server-side, independent of any browser connection, and nothing
    # ever prunes it). Scaled per interval so payload size stays
    # comparable across intervals — a 1-minute candle count over N days
    # is ~15x a 15-minute count over the same N days.
    "chart_history_days_1m": 5,
    "chart_history_days_5m": 20,
    "chart_history_days_15m": 60,
}

SECRET_KEYS = ("dhan_client_id", "dhan_access_token", "anthropic_api_key",
               "zerodha_api_key", "zerodha_access_token",
               "kotak_consumer_key", "kotak_access_token",
               "kotak_sid", "kotak_auth_token",
               "kotak_session_token", "kotak_mobile",
               "twelve_data_api_key", "alpha_vantage_api_key", "newsapi_api_key",
               "tradingview_webhook_secret", "telegram_bot_token",
               "dhan_postback_secret")


def load() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(PATH):
            try:
                cfg.update(json.load(open(PATH)))
            except Exception:
                pass
    # env fallback for secrets
    cfg["dhan_client_id"] = cfg["dhan_client_id"] or os.environ.get("DHAN_CLIENT_ID", "")
    cfg["dhan_access_token"] = cfg["dhan_access_token"] or os.environ.get("DHAN_ACCESS_TOKEN", "")
    cfg["anthropic_api_key"] = cfg["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


_LOG_FILE = os.path.join(os.path.dirname(PATH), "activity.log")


def _warn_dropped_keys(dropped):
    """Surface a config.save() key-drop the same way every OTHER
    failure in this codebase is surfaced: loudly, into the same
    activity.log operators already watch — not a silent no-op.

    2026-07-26 (v53) — added after this exact silent-drop pattern bit
    THREE separate features in one session (futures Phase 1's margin/
    trailing keys, Strategy 7's 13 keys, futures Phase 2's engine keys)
    before anyone noticed, purely because each one happened to get
    caught by a human reading a diff rather than by the system itself.
    Writes directly to activity.log rather than importing agents.py's
    Bus.log() — agents.py imports config, so importing back would be
    circular; this module already knows its own store directory.
    """
    if not dropped:
        return
    try:
        import datetime
        ts = store.ist_now().strftime("%Y-%m-%d %H:%M:%S")   # v59.71 — IST
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{ts}] [config] \u26a0 save() DROPPED unregistered "
                    f"key(s) — not persisted, will not survive a restart: "
                    f"{sorted(dropped)}. Register in config.DEFAULTS if "
                    f"this setting should actually be saved.\n")
    except Exception:
        pass


# Values below which a setting stops describing reality. Currently one
# entry, and it earned its place: fee_per_lot = 0 means "trading costs
# nothing", which is never true, and it silently overstated 184 trades
# across six sessions before anyone noticed.
FLOORS = {"fee_per_lot": 1}


def _warn_floored(floored):
    """Say loudly that a saved value was raised to its floor."""
    for k, was, lo in floored or []:
        msg = (f"config: {k}={was} is below the floor {lo} and was saved as "
               f"{lo}. A zero or near-zero fee makes every P&L figure, "
               f"every Quality breakdown and every backtest that "
               f"is_live_enabled() reads overstate profit.")
        try:
            import agents
            agents.pilot.bus.log("config", "⚠ " + msg)
        except Exception:
            print("[config] " + msg)


def save(updates: dict) -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(PATH):
            try:
                cfg.update(json.load(open(PATH)))
            except Exception:
                pass
        dropped, floored = set(), []
        for k, v in updates.items():
            if k not in DEFAULTS:
                dropped.add(k)
                continue
            if v is not None:
                # 2026-08-06 — FLOORS. A value that makes trading look
                # FREE is never a legitimate operator choice, and a
                # warning was not enough: `warn_zero_fees` already
                # existed, already said "Every P&L figure today is
                # overstated", and a full week (07-22..07-29, 184
                # trades) still recorded zero cost. Warnings are read
                # after the fact; a floor is not skippable.
                lo = FLOORS.get(k)
                if lo is not None:
                    try:
                        if float(v) < lo:
                            floored.append((k, v, lo))
                            v = lo
                    except (TypeError, ValueError):
                        pass
                cfg[k] = v
        json.dump(cfg, open(PATH, "w"), indent=2)
    _warn_dropped_keys(dropped)   # outside _lock — file I/O, not config state
    _warn_floored(floored)
    return cfg


def public_view(cfg: dict) -> dict:
    """Settings safe to send to the browser (secrets masked)."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        v = cfg.get(k, "")
        out[k + "_set"] = bool(v)
        out[k + "_masked"] = (v[:6] + "…" + v[-4:]) if len(v) > 12 else ("set" if v else "")
    return out
