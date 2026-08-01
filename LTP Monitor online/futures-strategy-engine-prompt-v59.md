# Futures Strategy Engine — Claude Code Build Prompt (v58.75 → v59.0)

**Repo:** `LTP Monitor online/ltp-monitor` @ `v58.75`
**New page:** Futures Research (`view-fstrat`) — **distinct from** the existing Futures Trading execution page
**Delivery mode:** phase by phase, stop for review after each

---

## 0. Kickoff block (paste this into Claude Code)

```
Read futures-strategy-engine-prompt-v59.md in the repo root.

We are building the Futures Research engine, 58.75 -> 59.0.

Read section 2 FIRST. The futures signal engine is currently DISABLED because it
lost money, and section 2 is the evidence. This project is a post-mortem before it
is a build. If Phase 0 concludes the existing engine has no fixable defect and no
new strategy clears the Phase A gates, the correct deliverable is a written finding
that futures stay disabled. That is a successful outcome, not a failed one.

Rules:
1. Work phase by phase. Complete a phase, run its tests, show me results, STOP.
   Do not begin the next phase until I say go.
2. Phase 0 and Phase A produce NO live-trading code path.
3. Every acceptance criterion is a hard gate. If a strategy fails, report the
   failure. Do not widen the sweep grid to rescue it.
4. futures_strategy_enabled stays False for the entire engagement. Nothing in
   this spec authorises re-enabling it.
5. Follow the standing rules in section 1 without being reminded.

Start with Phase 0.
```

---

## 1. Verified repo state (do not re-derive, but do re-verify if stale)

Confirmed by direct inspection of `main` on 2026-08-01:

| Fact | Detail |
|---|---|
| Version | `VERSION` = `v58.75` |
| Dashboard | `static/dashboard.html`, 343,005 bytes — the >30,000 build gate passes trivially; it is not a meaningful check at this size |
| Views | `showView()` switches over `["dash","futures","pnl","strat","inst","bt","journal","agents","macro","quality"]` with `view-{k}` / `rail-{k}` |
| Existing futures page | `view-futures` → `loadFuturesPage()` — **execution UI** (Spot vs Futures cards, Buy/Sell/Exit). Polls every 5s. **Do not modify or absorb it.** |
| Futures endpoints | Only three: `POST /api/futures/enter`, `/exit`, `/manual_deploy` |
| Futures engine | `agents.py` — `_futures_signal_engine()` (3731), `_futures_signal_eval()` (3800), `_monitor_futures()` (3535), `_futures_ai_check()` (3668) |
| Shadow journal | **Already exists** — `log_futures_shadow()` (agents.py:2635), writes JSONL to `SHADOW_PATH` tagged `kind="futures"`. Added 2026-07-29. **Reuse it. Do not build a second one.** |
| Backtester | `backtester.py` has `replay_spreads`, `replay_momentum`, `replay_pa`, `replay_ew_reversal`, `replay_ta_elliott`. **There is no `replay_futures`.** |
| Walk-forward | **Does not exist anywhere in the codebase.** `sweep_params()` is in-sample only, 3 candidates per param. |
| Prefix-invariance / lookahead test | **Does not exist.** |
| Cost model | `fee_per_lot`, default ₹40 flat. `warn_if_costs_disabled()` fires only when it is zero. |

### Standing project rules (violating any is a build failure)

- **Config registration** — every new key in **both** `config.DEFAULTS` **and** `SettingsIn`. `config.save()` silently drops unregistered keys.
- **Read-time clamping** — `get_params()` clamps to bounds on read, not only on write. On a 15×-levered future this matters more than it ever did on options.
- **Fail loud** — `NameError`/`AttributeError`/`KeyError`/`TypeError` propagate and surface red. Only transient network errors are swallowed. **Note:** `log_futures_shadow()` currently ends in `except Exception: pass`, which will silently lose journal entries — the journal is the evidence base for this entire project, so fix that in Phase 0.
- **No hardcoded contract specs** — lot size, tick size, expiry resolve from the Dhan scrip master; a failed lookup raises rather than defaults. Follow the existing multi-candidate exchange-code pattern from the SENSEX futures work.
- **Session boundaries** — all aggregation via `most_recent_session()`.
- **Chart markers** — through `lwRedrawMarkers()`, sorted ascending, anchored to per-symbol last-candle timestamps.
- **Tests touching persisted files** — snapshot and restore; redirect `agents.TRADES_FILE`.
- **Version bump** — `VERSION`, `APP_VERSION` in `app.py`, dashboard badge, zip filename, together.
- **pip** — `--break-system-packages`.

### Expiry calendar — audit before writing any date logic

Since Sept 2025: **NIFTY** weekly Tuesday, monthly last Tuesday. **BANKNIFTY / FINNIFTY / MIDCPNIFTY** — no weekly options at all, monthly only, last Tuesday. **SENSEX** weekly Thursday, monthly last Thursday (BSE). Holiday → previous trading day.

`futures_symbols` is currently `["NIFTY","BANKNIFTY","FINNIFTY"]` with SENSEX dropped. Report what the current expiry resolution produces for each of the three against the above.

---

## 2. The finding that reframes this project

`config.py` line 418:

> `"futures_strategy_enabled": False,` — changed from True to False on **2026-07-27** after real trading data showed **every futures trade closing at a loss or exact breakeven — all via forced kill-switch closure, none via their own profit target.**

`agents.py` line 2643, the shadow-journal docstring:

> futures is "the one instrument class that is demonstrably losing money (**40 trades, 27.5% win, −₹23,863**)".

**This is the single most important input to the build.** It means:

1. Futures in this system are not an unexplored opportunity. They are a **measured failure with a specific, unusual signature**: not "sometimes wins, sometimes loses," but *nothing ever reached target and everything was force-closed*. A strategy that loses is a bad edge. A strategy where **zero** of 40 trades hit its own target is an **exit-geometry or interference defect**, and those are two completely different problems.
2. Building three new strategies on top of an execution layer with an unexplained defect would reproduce the defect three more times. **Phase 0 is therefore a post-mortem, not scaffolding.**
3. A defensible outcome of this engagement is: "the defect was X, it is fixed, and here is the evidence" — or "no new strategy cleared the gates; futures stay disabled." Both are successes. Shipping an enabled engine without explaining the 40 trades is the only real failure mode.

### Prime hypotheses for Phase 0 to test

| # | Hypothesis | How to test |
|---|---|---|
| H1 | **Target unreachable.** `futures_atr_target_mult` = 2.75 × ATR. If intraday index futures rarely travel 2.75 ATR from a mid-session entry before square-off, the target is geometrically unreachable and every trade must exit some other way. | Distribution of MFE in ATR units across the 40 trades vs. the 2.75 threshold |
| H2 | **Defence zone pre-empts.** `futures_defense_enabled` = True, `futures_defense_zone_pct` = 40, `futures_defense_tighten_pct` = 50. A tightened stop after a 40% adverse excursion may be converting would-be winners into breakevens. | Count trades that hit the defence trigger, then compare their subsequent MFE against the original target |
| H3 | **Rupee cap distorts the stop.** `futures_risk_per_trade_rupees` = 2500 is a hard ceiling. If it binds, the effective stop is tighter than 1.5 ATR and the realised payoff is nothing like the intended 1.83. | Compare implied stop distance from the cap vs. 1.5 × ATR per trade |
| H4 | **Kill-switch is doing the closing.** All 40 closed via forced kill-switch. Either the portfolio kill-switch is firing constantly, or "kill-switch closure" is also the label for EOD square-off. | Establish what exit reason was actually recorded, and whether the label is being overloaded |
| H5 | **Costs.** `fee_per_lot` = ₹40 flat. See §3.2 — this understates real futures cost by roughly an order of magnitude. | Recompute the 40 trades' P&L under a correct cost model |

H4 is the one to resolve first, because if "kill-switch" is an overloaded label for ordinary EOD square-off, the headline finding is much less alarming than it reads — and the correct fix is exit-reason granularity, not strategy work.

---

## 3. Phase 0 — Post-mortem and cost correction

### 3.1 Trade post-mortem

Reconstruct all futures trades from `trades.jsonl` and the shadow journal. Produce:

- Per-trade table: entry, exit, direction, ATR at entry, intended stop, intended target, **realised MAE and MFE in both points and ATR units**, exit reason, hold duration, gross P&L, cost, net P&L.
- MFE distribution against the 2.75 ATR target line — **this single chart likely answers H1 outright.**
- Exit-reason histogram, with kill-switch separated from EOD square-off, target, stop, defence-tighten, and AI auto-exit.
- Count of trades where the ₹2,500 cap bound the stop tighter than 1.5 ATR.
- Rejected-signal analysis from the shadow journal: which gate blocked most, and what those blocked signals would have done.

**Deliverable: a written finding naming which hypothesis is supported, with the numbers.** Do not proceed to Phase A until this exists.

### 3.2 Cost model correction

`fee_per_lot = 40` is an options-shaped assumption. Futures STT is a percentage of **notional**, not a flat fee:

> 1 NIFTY lot at 24,800: STT on the sell side alone ≈ **₹372**. Full round trip ≈ **₹500 ≈ 7 index points**. The current model charges ₹40. **It understates futures cost by roughly 10×.**

Every futures backtest number produced under the existing model is therefore inflated — and `is_live_enabled()` reads backtest profitability, so this is a live-promotion risk, exactly the class of bug `warn_if_costs_disabled()` was written to catch.

Add a per-symbol, notional-aware futures cost function. All rates config-driven, registered in both places, clamped:

```
cost_round_trip(symbol, entry, exit, lots) =
      fut_brokerage_per_order * 2
    + fut_stt_sell_pct      * (exit * lot_size * lots)
    + fut_exchange_txn_pct  * (entry_notional + exit_notional)
    + fut_sebi_turnover_pct * (entry_notional + exit_notional)
    + fut_stamp_duty_pct    * entry_notional
    + fut_gst_pct           * (brokerage + exchange_txn + sebi)
    + fut_slippage_points   * lot_size * lots * 2
```

| Key | Default | Bounds |
|---|---|---|
| `fut_brokerage_per_order` | 20 | 0–100 |
| `fut_stt_sell_pct` | 0.0002 | 0–0.001 |
| `fut_exchange_txn_pct` | 0.0000173 | 0–0.0001 |
| `fut_sebi_turnover_pct` | 0.000001 | 0–0.00001 |
| `fut_stamp_duty_pct` | 0.00002 | 0–0.0001 |
| `fut_gst_pct` | 0.18 | 0–0.30 |
| `fut_slippage_points` | 1.0 | 0–10 |

Extend `warn_if_costs_disabled()` to also fire when a futures replay is about to run with the flat `fee_per_lot` model instead of the notional model.

**Sanity check to print:** 1 NIFTY lot at 24,800 ≈ ₹500 round trip ≈ 7 index points. If it isn't, the rates or the scrip-master lot size are wrong.

### 3.3 Fixes

- `log_futures_shadow()` — replace `except Exception: pass` with fail-loud handling. Losing journal entries silently defeats the purpose of the journal.
- Exit-reason granularity — if H4 confirms the label is overloaded, split it.

### 3.4 Phase 0 tests

`test_futures_costs.py` (NIFTY ≈ 7 pts; all symbols finite and positive; **missing lot size raises, does not default**), `test_futures_config_v59.py` (round-trip through `config.save()`; out-of-bounds clamped on read), `test_futures_shadow_failloud.py`, plus the expiry audit report.

**STOP. Report the post-mortem finding. Wait for approval.**

---

## 4. Phase A — Research harness and strategy evaluation (no live code)

### 4.1 Build what is missing

Two pieces of infrastructure do not exist and are prerequisites:

**`replay_futures(symbol, name, params, days, log)`** in `backtester.py`, following the existing `replay_*` signature convention so `_replay_for()` and `sweep_params()` can reach it. Uses the notional cost model from §3.2, not `fee_per_lot`.

**A walk-forward harness** — new, since nothing in the codebase does out-of-sample evaluation. 2 years of reconstructed data, 6-month IS / 2-month OOS rolled forward → 8–9 folds. Parameters chosen **inside each IS window only**; each OOS window evaluated once and never revisited. `sweep_params` runs inside the IS window, keeping its existing 3-candidates-per-param discipline.

**Prefix-invariance test** — mandatory, and the single most important test in this phase. Running the replay on data truncated at bar *t* must produce byte-identical signals up to *t*. Test at 20+ truncation points. Any signal that appears and later changes is lookahead.

### 4.2 Strategies under test

Costs from §3.2 applied inside the trade loop on every trade. Never report gross-only.

**S11 — Intraday Momentum.** Opening-window return above a threshold → same-direction position late session → exit before close. The strongest evidence base on the list and the smallest parameter count, which is why it goes first.

| Param | Default | Bounds |
|---|---|---|
| `fim_open_window_min` | 30 | 15–60 |
| `fim_min_abs_open_ret_pct` | 0.15 | 0.05–0.50 |
| `fim_entry_time` | "14:45" | 14:00–15:00 |
| `fim_exit_time` | "15:25" | 15:10–15:28 |
| `fim_sl_pct` | 0.35 | 0.15–0.80 |

Report results **with and without the stop.** The classic formulation has none; we add one for leverage. The difference tells us what the stop costs — and given H1/H2, how this system's exit geometry interacts with a strategy is now the thing we most need to measure.

**S12 — VWAP Mean Reversion.** True VWAP is available on futures because they carry real volume — the known index-spot limitation does not apply. Enter against a stretch from session VWAP, target VWAP, hard stop beyond.

| Param | Default | Bounds |
|---|---|---|
| `fvr_band_sigma` | 2.0 | 1.5–3.0 |
| `fvr_sl_pct` | 0.30 | 0.15–0.60 |
| `fvr_max_trades_per_day` | 3 | 1–6 |
| `fvr_daily_loss_cap_pct` | 1.5 | 0.5–3.0 |
| `fvr_regime_required` | "rangebound" | — |

Enforced in code, not convention: never average down; the daily cap kills the strategy for the session; the regime gate is a precondition, not a score contributor. Expect a high win rate with payoff below 1.0 — viable only if the tail is genuinely controlled, which is what the skew gate checks.

**S13 — ORB (futures port of existing S2).** Reuse S2's parameters. Add `forb_min_breakout_vol_mult` (1.5, bounds 1.0–3.0) = breakout-bar volume as a multiple of the session's running average. **Keep the OR-range minimum filter** — ORB without a volatility gate is a coin flip, since only ~30% of sessions are genuine trend days.

**S14 — Re-test of the existing engine under corrected costs and corrected exits.** Whatever Phase 0 concludes, run the current `_futures_signal_eval` logic through the new harness with the notional cost model and any exit fix applied. This tells us whether the existing engine was a bad edge or a broken exit — and it is cheap, because the logic already exists.

### 4.3 Acceptance gates

| Metric | Gate |
|---|---|
| Trade count | ≥ 200 to be scored; ≥ 380 to be statistically meaningful. **Below 200 → `INSUFFICIENT`, not `FAIL`** |
| Net expectancy (points/lot, after §3.2 costs) | **> 0** |
| t-statistic of mean net trade return | **≥ 2.0** |
| Walk-forward degradation (OOS ÷ IS expectancy) | **≥ 0.5** |
| **Target-reach rate** — share of trades exiting at their own target | **≥ 15%.** A strategy where nothing reaches target is the defect we are here to fix; this gate makes it impossible to ship one again |
| Skew of net-trade distribution | reported; **< −1.0 → mandatory daily cap + half sizing** |
| Cost drag (cost ÷ gross edge) | reported; **> 30% → flagged fragile** |
| Correlation of daily P&L to each existing strategy | **< 0.6** to count as diversifying |
| Max drawdown | reported, in points and % of margin |

**Why 380:** distinguishing a true 55% win rate from 50% at 95% confidence needs roughly 380 trades — about 18 months for a once-a-day strategy. The existing engine's 40 trades are far below this, which is worth stating plainly: **40 trades cannot establish that a strategy is bad either.** The 27.5% win rate is alarming; the −₹23,863 is real money; but the statistical case rests on the *exit signature*, not the sample size. Say so in the report.

**Sweep grid stays tiny.** Two parameters, three values each, maximum. A wider grid does not find more edge, it finds more overfit.

### 4.4 Phase A tests

`test_futures_replay_no_lookahead.py` (prefix invariance, 20+ points), `test_futures_replay_costs.py` (synthetic series, hand-checkable net points), `test_futures_walkforward.py` (folds don't overlap; OOS never used for selection), plus synthetic sanity: a perfectly trending series must make S11 profitable and a perfectly mean-reverting one must make S12 profitable. If those fail, the harness is broken, not the strategy.

**STOP. Report per-strategy verdicts. Wait for approval.**

---

## 5. Phase B — Basis residual

Turns the raw basis already displayed into an institutional-positioning signal — directly serving the project's stated objective.

```
fair_basis = spot × (r − q) × (days_to_expiry / 365)
residual   = actual_basis − fair_basis
residual_z = (residual − rolling_mean) / rolling_std
```

- `fut_financing_rate_pct` (6.5, bounds 3–12)
- `q` = dividend yield over remaining contract life. **Not a constant.** Build from the index dividend calendar where available; otherwise `fut_dividend_yield_pct` (1.2, bounds 0–4) with `approx=true` in the payload. NIFTY ex-dates cluster Feb–Aug and will otherwise bias the residual. Never substitute zero silently.
- `fut_residual_z_window` (200 bars, bounds 50–1000)

Persist to a new `basis_residual` table every RegimeAgent cycle — follow the existing Dhan pacing pattern, do not add an independent poll loop.

Reading: sustained positive z → aggressive long futures positioning; sharp compression during an up-move → longs unwinding into strength; sustained negative z → short build-up or hedging demand.

Expose as an optional gate to **all** strategies via `*_require_basis_agreement` (default off). The gate may only **veto**, never bypass an existing risk gate.

Tests: hand-computed fair basis from known inputs; z-score correct against a synthetic series; dividend-unavailable path sets `approx` and does not zero-substitute; cold start (fewer bars than window) neither divides by zero nor emits a fake z of 0.

**STOP.**

---

## 6. Phase C — Paper wiring

Only Phase A `PASS` strategies are wired. `INSUFFICIENT` runs shadow-only to accumulate sample. `FAIL` is not wired at all and its Deploy button renders disabled.

Reuse the existing S4 Phase 2 order path, margin-aware sizing, MAE/MFE tracking, and the portfolio kill-switch. Do not build a parallel risk system. `futures_min_regime_confidence` applies as today.

Shadow journal via the existing `log_futures_shadow()`. **Minimum 40 trading sessions** of shadow data before live is discussed — and note the symmetry: the previous engine was disabled on 40 *trades*. Forty sessions of shadow data across three symbols is a materially larger evidence base than the decision to disable was made on.

Overnight: `*_allow_overnight` defaults `False`. At 15× leverage a 4% gap is not a bad day. Once the News/Macro agent lands, positions reduce ahead of known macro events.

`futures_strategy_enabled` and `futures_live_enabled` both stay `False`.

**STOP.**

---

## 7. Phase D — Futures as delta hedge for S5/S6

The highest risk-adjusted use of futures here, and easy to overlook because it is not a return source. When spot breaches a bull-put or bear-call short strike, the position is short gamma; one future is faster and cheaper than legging out of a spread whose bid-ask just widened.

- Trigger: breach by `fhedge_trigger_buffer_pct` (0.10, bounds 0.0–0.5)
- Size: spread net delta from `bs_greeks.py`, whole lots, capped by `fhedge_max_lots` (2, bounds 1–5)
- Unwind: strike reclaimed by the same buffer, spread closed, or EOD
- **A hedge must never become a directional position** — if the parent spread closes, the hedge closes in the same cycle. Assert this in a test.
- Hedge P&L attributes to the parent spread record, not a separate strategy

**STOP.**

---

## 8. The Futures Research page

**New view key `fstrat`**, added to the `showView()` array alongside the existing ten, with `view-fstrat` and `rail-fstrat`, dispatching to `loadFuturesResearch()`. The existing `view-futures` execution page is untouched.

Follow existing UI conventions exactly: compact non-repeating tables with **rowspan-merged** shared columns; real green/red toggles, not checkboxes; left-aligned content enforced in CSS; feed-health chip WS teal / REST amber / stale red, never inferred from a frozen price; fail-loud surface for `NameError`/`AttributeError`.

**Panel 1 — Post-mortem (top, permanent).** The Phase 0 finding as a standing artifact: exit-reason histogram, MFE-vs-target distribution, and the headline numbers. This page should open with *why futures were switched off*, so nobody re-enables them without meeting that evidence first.

**Panel 2 — Futures state strip.** Index | Spot | Fut | Basis | Fair basis | Residual | Residual z | Volume | OI chg | Feed | Regime. Residual z colour-coded at ±1.5. `approx=true` shows an amber dot with a tooltip — never hide a data limitation.

**Panel 3 — Strategy verdict table** (rowspan). Symbol | Regime | Strategy | Verdict chip (`PASS`/`FAIL`/`INSUFFICIENT`) | Eligible | Confidence | **Target-reach rate** | Reason | Params | Enabled | Deploy. `FAIL` disables Deploy.

**Panel 4 — Basis residual chart.** Own LWC pane, ±1.5 lines, crosshair-synced per the official tutorial pattern.

**Panel 5 — Research results.** Run selector → trades, win rate, payoff, net expectancy (pts), t-stat, **target-reach rate**, skew, max DD, OOS/IS ratio, cost drag %, correlation to existing strategies. Fold-by-fold OOS expectancy as a bar strip.

**Panel 6 — Shadow journal.** Reads the existing JSONL filtered to `kind="futures"`. Date | Time | Symbol | Strategy | Direction | Verdict | Failed gates | Entry | SL | T1 | Hypothetical P&L | Confidence | Residual z. Filterable; default last 5 sessions.

**Panel 7 — Hedge monitor.** Spread | Short strike | Spot | Breach | Net delta | Hedge lots required | Active | P&L attribution.

**Panel 8 — Cost model readout.** Per symbol: lot size with its scrip-master source, round-trip cost in ₹ and points, and current cost drag as a share of each strategy's measured gross edge. This panel exists because a stale lot size or tax rate silently corrupts every expectancy number on the page without ever throwing — and that is precisely what `fee_per_lot = 40` has been doing.

New endpoints: `GET /api/futures/research/state`, `/basis/{symbol}`, `/backtest/runs`, `/backtest/run/{id}`, `POST /api/futures/backtest/run`, `GET /api/futures/postmortem`, `GET /api/futures/hedge`, `POST /api/futures/hedge/toggle`.

### Build gates

```bash
wc -c static/dashboard.html      # currently 343,005 — passes trivially, not a real check
node -c <extracted JS block>
python run_tests.py
# TestClient smoke on every new endpoint
```

Then bump `VERSION`, `APP_VERSION`, dashboard badge together; zip to `/mnt/user-data/outputs/ltp-monitor-v59.0.zip`.

---

## 9. Risks and anti-patterns

| Risk | Mitigation |
|---|---|
| **Building on an unexplained defect** | Phase 0 post-mortem gates everything |
| Lookahead inflating results | Prefix invariance at 20+ truncation points |
| **Flat `fee_per_lot` inflating futures backtests ~10×** | Notional cost model; `warn_if_costs_disabled` extended |
| Overfitting via grid width | 2 params × 3 values; walk-forward only; failure is a valid result |
| Small-sample false positives | `INSUFFICIENT` below 200 trades; no deploy on `INSUFFICIENT` |
| **Shipping another strategy nothing reaches target on** | Target-reach-rate gate ≥ 15% |
| Mean reversion blowing up on a trend day | Regime precondition, hard stop, no averaging down, daily cap in code |
| Overnight gap at 15× leverage | `allow_overnight` defaults False |
| Hedge drifting directional | Spread close forces hedge close same cycle, asserted |
| Silent journal loss | Fix `except Exception: pass` in Phase 0 |
| Wrong expiry after the 2025 SEBI change | Phase 0 expiry audit |

---

## 10. Phase summary

| Phase | Scope | Trades? | Gate |
|---|---|---|---|
| **0** | Post-mortem of the 40 trades, cost correction, journal fix, expiry audit | No | A written finding naming the supported hypothesis |
| **A** | `replay_futures` + walk-forward + no-lookahead test; evaluate S11/S12/S13/S14 | No | ≥1 `PASS`; prefix-invariance green |
| **B** | Basis residual + gate | No | Hand-computed fair basis matches |
| **C** | Paper wiring of survivors | Paper only | 40 sessions shadow before live is discussed |
| **D** | Delta hedge for S5/S6 | Paper only | Hedge-close assertion green |

`futures_strategy_enabled` and `futures_live_enabled` remain `False` throughout. Nothing here authorises re-enabling them.
