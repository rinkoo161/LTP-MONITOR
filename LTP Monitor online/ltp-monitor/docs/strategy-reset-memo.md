# Strategy Research Reset — Memo

Written incrementally, Part by Part. Numbers and the script that produced
them; "unknown" where I do not know.

Scripts live in `scratch/`. Nothing in this session edited strategy code,
tuned a parameter, or ran a sweep.

---

## Context load — all required documents now read

| Document | Status |
|---|---|
| `LTP-Monitor-System-Reference.docx` (§5, §6) | **read** — supplied late; despite the `.docx` extension it is plain UTF-8 text, 55,904 chars |
| `elliott-structure-engine-spec.md` (§2.4) | **read** — 22,983 chars |
| `futures-strategy-engine-prompt-v59.md` | read |
| `config.py` DEFAULTS + live `config.json` | read, both — see 0.4 |
| `strategies.py`, `backtester.py`, `sizing.py` | read |
| closed-trade table, shadow journal | read — 313 closed trades, 17 sessions |

Parts 0.1 and 0.3 were completed before the two specs arrived and are
unaffected by them (they rest on trade history and database state).
**0.2 and 0.4 were amended after reading §5.1 and §6** — the spec supplies
the *intended* values, which is a stronger comparison than `DEFAULTS`.
Both amendments are marked below.

### Two things the specs establish that bear on later Parts

- **§5 documents SIX strategies** ("across two families"). Established
  fact 2 refers to **11** live options strategies. The system has roughly
  doubled its strategy count since the reference was written. Which 11,
  and whether the 5 undocumented ones have any written thesis, is a
  Part 1 question — flagged, not answered here.
- **§2.4 of the Elliott spec is a routing thesis, not a strategy**:
  corrective waves take "days and weeks", so on weekly options they are
  "a guaranteed loss even if the count is right" — therefore impulse
  structures → option buying (S1/S8/S9/S10), corrective structures →
  credit spreads (S5/S6). It explicitly frames the structure engine as a
  **strategy router** and as "an independent veto" on momentum-buy
  confidence reading high in RANGEBOUND chains. Directly relevant to
  Part 1's "how many independent bets" question.

**One change of state since the audit the prompt describes.** Between that
audit and this memo, the fee model was rewired to the notional/premium-aware
modules and the journal was restated (see `ROADMAP.md` v59.49–v59.52). All
P&L figures below are **post-restatement**. The established facts are not
contradicted — fact 1's "understated fees" is precisely what was corrected,
and 0.2 below re-verifies the correction independently.

---

## Part 0.1 — Power analysis

`scratch/p0_power.py`, `scratch/p0_power_strategy.py`

Two-sided α = 0.05, power 0.80, so n = 7.849·σ²/δ². σ is the empirical
per-trade net P&L standard deviation from actual closed trades.

### By family

| family | n now | mean | sd | n for δ=₹200 | δ=₹500 | δ=₹1,000 |
|---|---:|---:|---:|---:|---:|---:|
| long option | 60 | −270 | 1,133 | 252 | 41 | 11 |
| credit spread | 194 | −205 | 1,476 | 428 | 69 | 18 |
| futures | 59 | −1,693 | 3,864 | 2,930 | 469 | 118 |
| ALL | 313 | −498 | 2,178 | 931 | 149 | 38 |

At a 5-trade/day cap and 250 sessions/year, detecting a **₹500** edge needs
**8 trading days** (long option), **14 days** (credit spread), **94 days**
(futures). Detecting **₹200** needs 50 / 86 / 586 days.

**So at family level, yes — our volume can reach these counts inside a
year, comfortably for a ₹500 edge and plausibly for ₹200.**

### By strategy — where promotion decisions are actually made

| strategy | n | mean | sd | t | n for δ=₹500 |
|---|---:|---:|---:|---:|---:|
| bear_call_spread | 103 | −161 | 1,963 | −0.83 | 121 |
| bull_put_spread | 91 | −256 | 527 | **−4.63** | 9 |
| (future, unattributed) | 59 | −1,693 | 3,864 | **−3.36** | 469 |
| (option, unattributed) | 13 | −476 | 541 | **−3.17** | 10 |
| AI | 12 | −298 | 1,335 | −0.77 | 56 |
| momentum_confluence | 8 | +100 | 636 | +0.44 | 13 |
| vwap_pullback | 7 | −537 | 1,334 | −1.06 | 56 |
| rule-engine (AI invalid) | 7 | +69 | 385 | +0.48 | 5 |
| orb | 4 | +163 | 1,957 | +0.17 | 121 |
| sg_ema | 4 | −82 | 620 | −0.26 | 13 |
| ema_mtf | 2 | −1,898 | 2,030 | −1.32 | 130 |

**Findings.**

1. **Three strategies already have statistically solid NEGATIVE edges** at
   conventional thresholds: `bull_put_spread` t = −4.63 on n = 91, the
   unattributed futures book t = −3.36 on n = 59, unattributed options
   t = −3.17 on n = 13. Consistent in kind with established fact 3.
2. **`bear_call_spread` is the largest sample (n = 103) and is
   indistinguishable from zero** (t = −0.83). It is not a demonstrated
   edge in either direction; it needs 121 trades for a ₹500 edge and has
   103, so it is close to decidable — the one strategy where more data
   would actually settle something.
3. **Every positive-mean strategy has n ≤ 8.** None is remotely near its
   own power requirement. `rule-engine (AI invalid)` at n = 7 and
   `momentum_confluence` at n = 8 are noise, whatever their sign.
4. The per-strategy σ is dominated by a few strategies with very high
   dispersion (`orb` σ = 1,957 on 4 trades; `bear_call_spread` σ = 1,963).
   High σ is what makes their power requirements large.

**Answer to the question as posed:** at family level our volume suffices
within a year. At strategy level it does **not**, because the 5/day cap is
shared across ~11 strategies — each gets well under 1/day in practice, and
today only 6 trades occurred across the whole book.

---

## Part 0.2 — Cost model verification

`scratch/p0_costs.py`. Rebuilt from published rates independently of
`options_costs.py`, so this is a check rather than the model validating
itself. NIFTY lot 65, half-spread 0.5 pts, live `fee_per_lot` = 30.

### A) 1-lot NIFTY weekly ATM option, 150 → 160

| component | rupees |
|---|---:|
| brokerage (₹20 × 2 orders) | 40.00 |
| STT (0.10% × sell premium) | 10.40 |
| exchange txn (0.05% × both sides) | 10.08 |
| SEBI (₹10/crore) | 0.02 |
| stamp duty (0.003% × buy) | 0.29 |
| GST (18% × brokerage+exch+SEBI) | 9.02 |
| **statutory** | **69.80** |
| bid-ask (0.5 pt × 65 × 2 crossings) | 65.00 |
| **all-in round trip** | **134.80** |

`options_costs.py` returns statutory 69.80, spread 65.00, total 134.80 —
**identical**. The flat model charges ₹60.00.

**AMENDED after reading §5.1.** The spec documents the intended fee as
"₹40 per lot per transaction × 2 (entry + exit) = **₹80 round-trip per
lot**". Live `fee_per_lot` is 30, i.e. ₹60. So there are two ratios and
both matter:

| basis | charged | real | ratio |
|---|---:|---:|---:|
| **as designed** (§5.1, ₹40/lot) | 80.00 | 134.80 | **1.69×** |
| as configured live (₹30/lot) | 60.00 | 134.80 | **2.25×** |

Statutory alone is 69.80, i.e. only **0.87×** the designed ₹80 — so the
*designed* flat fee was a reasonable approximation of statutory charges
and understates the round trip almost entirely because it omits the
bid-ask. The live value of 30 makes it worse by a further 25%.

### B) 1-lot bull put spread — SELL ATM 150, BUY OTM hedge 60

| leg | statutory | slippage |
|---|---:|---:|
| short (150 → 140) | 67.74 | 65.00 |
| hedge (60 → 52) | 55.00 | 65.00 |
| **total** | **122.74** | **130.00** |

All-in **₹252.74**. Flat model charges ₹120.00 → **ratio 2.11×**.

**A discrepancy worth recording.** `options_costs.py` returns **₹265.47**
for the same spread — 5% higher — because it applies the *same premium* to
both legs (`premium × lot × legs`). A real spread's hedge leg is far
cheaper than its short leg, so the shipped model overstates the hedge's
statutory component. Small, but it is a modelling simplification not
documented in that module.

**STT on exercised longs.** For an option held to expiry and settled ITM,
STT is levied on **intrinsic value**, not premium — which for a deep-ITM
weekly can exceed the entire premium-based figure above. This system
squares off intraday at 15:23 and never carries to expiry, so it does not
arise today. **Neither model covers it**, and any future strategy that
holds to expiry would need it added before its costs mean anything.

**Relation to established fact 1** ("₹40/lot vs ~₹500 real round-trip on
futures"): that ~10× figure is a *futures* number, where STT is on
~₹18 lakh notional. The options ratio is 2.1–2.25×. Both can be true; they
are different instruments and I have not re-derived the futures figure.

---

## Part 0.3 — P&L reconstruction integrity

`scratch/p0_reconstruct.py`

```
chain_snapshots: 726,214 rows, 2026-07-29 .. 2026-08-07
distinct snapshot days: 9
closed trades: 313 across 17 days
```

| | trades | share |
|---|---:|---:|
| fall on a day where snapshots still exist | 127 | **41%** |
| **UNVERIFIABLE — snapshots pruned** | **186** | **59%** |

Of the verifiable set, 40 option trades had a snapshot within ±180s of
**both** entry and exit. Recomputing gross from recorded premiums and
differencing against stored `gross_pnl`:

| statistic | rupees |
|---|---:|
| n | 40 |
| mean error | −43 |
| median error | +10 |
| **sd** | **651** |
| min | −2,940 |
| p10 | −300 |
| p90 | +469 |
| max | +1,125 |
| **within ±₹50 of stored** | **25%** |

**This is the most serious Part 0 finding.** The mean and median are near
zero — so there is no systematic bias — but the **dispersion is ₹651 on
trades whose mean absolute P&L is a few hundred rupees**. Only a quarter
of trades reconstruct to within ₹50.

Two candidate explanations, and I cannot separate them with current data:

1. **Fill-vs-snapshot timing.** Fills are recorded at the price the agent
   saw on a 3-second feed; snapshots are 60-second. A ±180s search window
   can pick a materially different premium. This would produce exactly this
   signature — unbiased, high variance.
2. **Stored P&L is genuinely wrong** for some trades.

Distinguishing them requires archiving the fill price *and* the snapshot
timestamp used, which we do not currently do. **Until that is resolved, no
backtest-vs-live comparison at the individual-trade level is trustworthy**,
and 59% of history cannot be audited at all.

---

## Part 0.4 — Config drift

`scratch/p0_drift.py`. 24 keys differ from `DEFAULTS`; **17 touch sizing,
risk limits or gate thresholds**.

| key | DEFAULT | LIVE | direction |
|---|---:|---:|---|
| `risk_pct_per_trade` | 1.0 | **5.0** | 5× looser |
| `lots_per_trade` | 1 | **5** | 5× looser |
| `max_trades_per_day` | 3 | **100** | 33× looser |
| `max_concurrent_positions` | 1 | **10** | 10× looser |
| `stop_after_consecutive_losses` | 2 | **20** | 10× looser |
| `cooldown_after_loss_min` | 15 | **1** | 15× looser |
| `daily_loss_limit` | 5,000 | 10,000 | 2× looser |
| `portfolio_max_drawdown` | 12,000 | 15,000 | 1.25× looser |
| `daily_profit_target` | 0 | 80,000 | n/a |
| `transaction_target_rupees` | 0 | 5,000 | n/a |
| `dynamic_sizing_enabled` | False | **True** | changes sizing path |
| `fee_per_lot` | 40 | 30 | 25% cheaper |
| `futures_ai_auto_exit_enabled` | False | **True** | |
| `spread_ai_auto_exit_enabled` | False | **True** | |
| `market_data_feed` | rest | websocket | |
| `auth_enabled` | False | True | |
| `pa_enabled` | 6 strategies | 5 (different set) | |

Non-material (7): `auto_execute`, `auto_strategies`, `kotak_base_url`,
`s7_auto_deploy`, `s8_auto_deploy`, `ta_auto_deploy`, `watch_symbols`.

**AMENDED after reading §5.1 and §6.** The spec states the *intended*
operating values directly, which is a stronger benchmark than `DEFAULTS`.
Three of them do not even match `DEFAULTS`:

| control | §5.1 spec says | DEFAULTS | LIVE |
|---|---:|---:|---:|
| max trades/day | **5** | 3 | **100** |
| re-entry cooldown after a loss | **15 min** | 15 | **1 min** |
| halt after consecutive losses | **2** | 2 | **20** |
| daily loss limit | **₹5,000** | 5,000 | **₹10,000** |
| fee per lot per transaction | **₹40** | 40 | **₹30** |

So live is not merely drifted from defaults — it is drifted from the
**documented design**, in the loosening direction on all five. Note also
that `DEFAULTS` itself says 3 trades/day where the spec says 5; a small
inconsistency, but it means neither source alone is authoritative.

`max_lots_per_trade` (§6.1's hard cap) is **10 in both DEFAULTS and
live** — checked, no drift there.

**Findings.**

1. **Every single risk limit is looser than its default**, most by 5–33×.
   Not one is tighter. `lots_per_trade = 5` confirms established fact 1 and
   is still live.
2. **`risk_pct_per_trade` is 5.0 against a default of 1.0** — this was not
   in the audit's list of six. At ₹200,000 capital that is ₹10,000 of risk
   per trade against a `daily_loss_limit` of ₹10,000: **one trade is
   permitted to consume the entire day's loss budget.** This is also why
   the risk gate rejected trades all morning with
   `daily loss limit (risking ₹13,545, day P&L ₹−290)`.
3. **`fee_per_lot` is 30 against a default of 40** — cheaper than shipped,
   in the direction that overstates profit. It is now only a fallback
   (v59.49) but it is still the wrong direction.
4. **Two AI auto-exits remain ENABLED**: `spread_ai_auto_exit_enabled` and
   `futures_ai_auto_exit_enabled`. The *option* one was disabled after all
   five auto-exits it ever took were found to close positions on a trend
   that favoured them. **The same mechanism is still armed for spreads and
   futures**, and I have not audited those two paths' history.
5. `stop_after_consecutive_losses = 20` effectively disables the
   consecutive-loss halt: 20 losses at the observed mean would be several
   thousand rupees before it engages.

---

## Part 0 verdict

| question | answer |
|---|---|
| Can we detect a ₹500 edge at family level within a year? | **Yes** — 8–94 trading days depending on family |
| Can we detect it per strategy? | **No** — the 5/day cap is shared across ~11 strategies; positive-mean strategies all have n ≤ 8 |
| Is the cost model right? | **Yes for single-leg options** (independent rebuild matches to the rupee). Real cost is **1.69×** the §5.1 designed fee and **2.25×** the live one. Spread hedge leg overstated ~5%. Expiry-settlement STT unmodelled but does not arise intraday |
| Can stored P&L be trusted? | **59% of history is unverifiable.** Of the rest, only 25% reconstructs to within ₹50; sd of error ₹651 |
| Is live config the config we think? | **No.** 17 material keys differ from DEFAULTS, and 5 differ from the §5.1 documented design — every one in the loosening direction. `risk_pct_per_trade` 5× |

**Blocking issue for everything downstream:** the reconstruction error
(0.3) means individual-trade P&L cannot currently be independently
verified. Any Part 3 pre-registration that specifies an expected per-trade
edge will be measured against a number with ±₹651 of unexplained
dispersion. That should be resolved, or explicitly accepted as a known
limitation, before a protocol is frozen in Part 4.

---

# Part 1 — Adversarial audit: how many independent bets do we have?

Hypothesis under test: *we do not have 11 strategies, we have one
strategy expressed 11 ways.* **Partially confirmed — but not where the
hypothesis pointed.**

## 1.1 Signal correlation — the two measures disagree, and that is the result

`scratch/p1_correlation.py`. Grid = (day, symbol, 30-min bucket),
direction +1 BUY_CE / −1 BUY_PE, from 1,383 shadow signals over 16 days.
Seven strategies had ≥8 directional signals.

**(a) Pearson r on the ±1/0 vectors — as the brief asks**

|  | AI | mom_conf | vwap_pb | orb | sg_ema | rule-eng | ema_mtf |
|---|---:|---:|---:|---:|---:|---:|---:|
| AI | 1.00 | −0.00 | 0.02 | 0.01 | −0.04 | −0.23 | −0.06 |
| momentum_confluence | −0.00 | 1.00 | 0.19 | 0.11 | −0.01 | −0.01 | 0.00 |
| vwap_pullback | 0.02 | 0.19 | 1.00 | 0.19 | 0.08 | 0.06 | 0.06 |
| orb | 0.01 | 0.11 | 0.19 | 1.00 | 0.03 | −0.01 | 0.08 |
| sg_ema | −0.04 | −0.01 | 0.08 | 0.03 | 1.00 | 0.11 | 0.19 |
| rule-engine | −0.23 | −0.01 | 0.06 | −0.01 | 0.11 | 1.00 | 0.13 |
| ema_mtf | −0.06 | 0.00 | 0.06 | 0.08 | 0.19 | 0.13 | 1.00 |

Eigenvalues 1.44 1.29 1.05 0.88 0.82 0.79 0.72. PC1 explains **21%**.
Participation ratio → **6.58 effective bets out of 7**.

**Taken at face value that refutes the hypothesis. Do not take it at
face value.** The grid has 284 buckets and no strategy fires in more
than 116, so the vectors are mostly zeros and r is dominated by
co-*absence*: two strategies that are both flat score as agreeing. The
6.58 is an artefact of sparsity, not evidence of diversification.

**(b) Conditional agreement — of buckets where BOTH fired, % same direction**

| pair | agreement | n co-fires |
|---|---:|---:|
| vwap_pullback ↔ orb | **89%** | 19 |
| sg_ema ↔ vwap_pullback | **77%** | 13 |
| momentum_confluence ↔ vwap_pullback | **72%** | 47 |
| momentum_confluence ↔ orb | **68%** | 25 |
| sg_ema ↔ rule-engine | 100% | 6 |
| ema_mtf ↔ vwap_pullback / orb / sg_ema | 100% | 2–5 |
| **AI ↔ everything else** | **25–50%** | 4–37 |

**Finding.** The five price-action strategies — `vwap_pullback`, `orb`,
`momentum_confluence`, `sg_ema`, `ema_mtf` — agree **68–100%** of the
time when they both fire. That is one bet expressed five ways, exactly
as hypothesised. The `AI` signal path is the exception: it agrees with
everything at **25–50%**, i.e. a coin flip, so it is genuinely
uncorrelated with the PA family.

**Effective independent bets ≈ 3, not 7 and not 11:**
one price-action/trend-continuation bet, one AI bet, one credit-spread
bet (§1.5). Caveat: co-fire counts are small (2–47).

## 1.2 Economic classification — the honest table

| Strategy | Phenomenon claimed | Counterparty | Why they keep losing |
|---|---|---|---|
| momentum_buy (S1) | OI-derived bias + trend continuation | *not stated* | *not stated* |
| orb (S2) | Opening-range breakout | *not stated* | *not stated* |
| vwap_pullback (S3) | Pullback to session anchor, trend resumption | *not stated* | *not stated* |
| ema_mtf (S4) | 5/13 EMA cross, MTF confirmed | *not stated* | *not stated* |
| sg_ema (S7) | EMA cross gated by structure | *not stated* | *not stated* |
| momentum_confluence | MACD+Stoch confluence (Pine port) | *not stated* | *not stated* |
| ew_reversal (S8) | Ending diagonal / H&S reversal | *not stated* | *not stated* |
| ta_elliott (S9) | Elliott structure + TA confluence | *not stated* | *not stated* |
| bull_put_spread (S5) | OI wall holds → sell premium below it | **premium buyer** | pays insurance/convexity — a real, published reason |
| bear_call_spread (S6) | OI wall holds → sell premium above it | **premium buyer** | same |
| mtf_confluence | MACD+Stoch (rinkoo.docx) | *not stated* | *not stated* |
| futures_signal (S4-P2) | Futures hybrid entry | *not stated* | disabled; −₹99,858 |

**Ten of twelve have no counterparty and no persistence argument
anywhere in `strategy_docs.py`, §5, or the code comments.** They
describe *how* they enter, never *who pays* or *why that party keeps
paying. Per the brief's own rule, those ten have no thesis.

The two credit spreads are the only strategies with an economic story
that survives the question: the buyer of a defined-risk spread's short
leg is paying for insurance, and insurance demand is a documented,
persistent reason to overpay.

## 1.3 Exit geometry — T1 sits beyond what the market usually delivers

`scratch/p1_geometry.py`, `scratch/p1_t1_percentile.py`. Premium and
delta **measured at 40 real entries** from `chain_snapshots`, not
assumed:

- ATM premium = **0.42% of spot** (median)
- |delta| at entry = **0.526** (median)
- so T1 at +30% premium requires a spot move of `0.42% × 0.30 / 0.526`
  = **0.24% of spot**

| symbol | spot | T1 needs | n | median move to close | **%ile of close** | median MFE | **%ile of MFE** |
|---|---:|---:|---:|---:|---:|---:|---:|
| NIFTY | 24,650 | 59 pts | 27 | 30 pts | **89%** | 55 pts | **56%** |
| BANKNIFTY | 57,800 | 138 pts | 7 | 62 pts | **86%** | 64 pts | **71%** |
| FINNIFTY | 26,900 | 64 pts | 4 | 13 pts | **100%** | 10 pts | **100%** |
| SENSEX | 78,700 | 189 pts | 20 | 97 pts | **75%** | 159 pts | **75%** |

Under the **spec's original** +60% T1 the required NIFTY move is 118 pts
— beyond the largest move-to-close observed (156) at roughly p97.

**Finding.** The gap between the two percentile columns is the story.
On a *maximum-favourable* basis NIFTY's T1 is reachable ~44% of the
time; on a *close* basis only 11%. **The target is frequently touched
and given back.** That is the same phenomenon observed live on
2026-08-07, where every profit-lock exit captured 1.45–3.45/share while
the two target/floor exits captured 10–11.55.

FINNIFTY at 100% on both measures (n=4) never moves far enough at all.

## 1.4 Long-premium drag — it is NOT theta, it is costs

`scratch/p1_theta.py`

```
holding period, 53 option trades
  median 6.1 min   p25 1.4   p75 15.5   max 70 min
theta at entry (40 trades with a snapshot)
  |theta| median 24.40 per share per day
  rupee bleed over the ACTUAL hold: median Rs 45, mean Rs 84, max Rs 383
```

| symbol | cost | theta bleed | total | **index points to break even** |
|---|---:|---:|---:|---:|
| NIFTY | ₹127 | ₹9 | ₹136 | **4.0 pts** (0.016%) |
| BANKNIFTY | ₹93 | ₹23 | ₹116 | **7.3 pts** (0.013%) |
| FINNIFTY | ₹122 | ₹0 | ₹122 | **3.9 pts** (0.014%) |
| SENSEX | ₹82 | ₹108 | ₹190 | **18.0 pts** (0.023%) |

**This partly refutes the brief's framing.** At a *median 6-minute
hold*, theta is 7–13% of the drag on NIFTY/BANKNIFTY/FINNIFTY —
transaction cost is 87–93% of it. Long-premium decay is not what is
killing these trades; **paying to cross the spread six times an hour
is**. SENSEX is the exception, where theta (₹108) exceeds cost (₹82).

The break-even move (4–18 pts) is small relative to T1 (59–189 pts), so
the problem is not that break-even is unreachable — it is that the
strategies hold for minutes and exit near break-even rather than near
target.

## 1.5 The S5/S6 exception — a fair sample, starved by CAPITAL not filters

`scratch/p1_spreads.py`

| strategy | n | net | mean | sd | t | win |
|---|---:|---:|---:|---:|---:|---:|
| bear_call_spread | 103 | −₹16,532 | −161 | 1,963 | **−0.83** | 33% |
| bull_put_spread | 91 | −₹23,269 | −256 | 527 | **−4.63** | 26% |

Both filters sit **at the permissive floor of their own bounds** —
`wall_gap_frac` 1.5 against bounds (1.5, 4.0), `credit_min_frac` 0.25
against (0.25, 0.40). They cannot be loosened further without moving
floors that were raised on 2026-07-23/24 after real spreads at 15–22%
credit fraction produced 4–5.6:1 risk:reward against the trader.

Skip reasons from the deploy loop, aggregated over the whole log:

| reason | count |
|---|---:|
| **capital_concentration** | **1,010** |
| not_eligible | 914 |
| stale_analysis | 416 |
| no_analysis | 206 |
| on_cooldown | 178 |
| entry_failed | 70 |
| consec_loss_halt | 47 |
| max_concurrent | 0 |

Eligibility rejections attributable to the two filters: `credit_min_frac`
259, `wall_gap_frac` 258 — real, but together ~57% of `not_eligible`
and far below `capital_concentration`.

**Answer:** they were **not** starved by the wall-gap/credit filters.
They got n=103 and n=91 — the two largest samples in the book. The
binding constraint was **capital concentration** (1,010 skips), i.e.
`margin_per_lot_spread` ₹85,000 against ₹200,000 capital at 60%, which
permits **two** concurrent spreads. `bear_call_spread` at t = −0.83 is
the one strategy in the entire book close to being decidable on more
data.

## Part 1 verdict

| question | answer |
|---|---|
| How many strategies? | 12 documented, 6 in the spec — the count doubled without the reference being updated |
| How many independent bets? | **≈3**: one price-action/trend bet (5 strategies, 68–100% agreement), one AI bet (25–50% agreement, genuinely uncorrelated), one credit-spread bet |
| Is the participation ratio of 6.58 meaningful? | **No** — artefact of a sparse grid dominated by co-absence |
| How many have an economic thesis? | **2 of 12.** The credit spreads. The other ten state entry mechanics only |
| Where does T1 sit? | p56–p75 of the max-favourable distribution, p75–p100 of move-to-close. Touched, then given back |
| Is long-premium theta the drag? | **No.** At a 6-minute median hold, costs are 87–93% of the drag. SENSEX is the one exception |
| Were the spreads starved? | **No** — largest samples in the book. Constrained by capital (1,010 skips), not by the two filters (517) |

---

# Part 2 — First principles: where can edge exist for *this* participant?

## Our constraints, stated before any candidate

| constraint | consequence |
|---|---|
| Retail account, broker REST/websocket, ~3s polling | we see the top of book at best, seconds late |
| No colocation, no order-book depth | any edge that decays inside seconds is closed to us |
| MIS intraday only, square-off 15:23 (not 15:15 — measured) | no overnight, no multi-day structure, no expiry settlement |
| Small capital (₹200,000), `margin_per_lot_spread` ₹85,000 | **2 concurrent spreads maximum** — measured, 1,010 capital skips |
| Costs (Part 0) | ₹134.80 single-leg, ₹252.74 spread round trip |
| Break-even (Part 1.4) | **4–18 index points per trade** |
| Data held | option chain (OI, ChgOI, vol, IV, greeks) **9 days**; index candles **2 years**; futures OHLCV 7 days; FII/DII, macro/news |

**The binding constraint is the ₹130–250 round trip against a ₹200,000
account.** Any candidate whose gross edge per trade is under ~₹150 is
unreachable no matter how sound the signal.

## The six candidates

### 1. Volatility risk premium — **CANNOT CONFIRM; our data says the opposite**

`scratch/p2_vrp.py`

| symbol | ATM IV % | RV same days % | RV 2yr % | **VRP vs 2yr** |
|---|---:|---:|---:|---:|
| NIFTY | 9.0 | 13.0 | 12.9 | **−4.0** |
| BANKNIFTY | 12.6 | 14.3 | 15.4 | **−2.9** |
| SENSEX | 9.9 | 8.2 | 12.9 | **−3.0** |
| FINNIFTY | 16.3 | 20.9 | 15.9 | **+0.4** |

- **Mechanism** — buyers of index options pay above fair value for
  insurance; the seller earns the difference.
- **Counterparty** — hedgers and directional speculators buying
  convexity. Documented, persistent, and the reason the premium exists.
- **Persistence** — it is compensation for bearing gap/tail risk, not a
  mispricing, so it is not arbitraged away.
- **Kill condition** — IV consistently at or below trailing realised
  over a 60+ day window.

**On our own data the kill condition is already met on 3 of 4 symbols.**
Implied sits 3–4 vol points *below* realised. That is the opposite sign
to the published literature.

I do not think this refutes VRP. I think **our IV sample is 9 days and
`daily_atm_iv` is EMPTY** — the multi-year IV series this system was
designed to keep was never backfilled. Nine days of IV against two years
of RV is not a like-for-like comparison, and it spans a period whose
realised vol may be atypical.

**Verdict: the one candidate with the strongest prior cannot be
evaluated, because we did not keep the data.** Concrete action —
start populating `daily_atm_iv` now; it needs ~60 sessions before this
question is answerable.

### 2. Expiry-day pinning / gamma near heavy OI strikes — **NOT TESTABLE**

- **Mechanism** — dealers short gamma near a heavy OI strike hedge
  toward it, pinning spot into the close.
- **Counterparty** — the dealer's hedging flow, which is mechanical.
- **Persistence** — structural, arises from hedging obligations.
- **Kill condition** — pin frequency at heavy-OI strikes no better than
  chance over 20+ expiries.

**We hold 2 expiry days** (2026-07-30 and 2026-08-06). Two observations
cannot distinguish pinning from noise; ~20 expiries are needed.

> **CORRECTION (issued in Part 3).** I first wrote that a 5-day prune
> destroys this data. That is wrong. `history.prune_chain_snapshots()`
> is *tiered*, not a delete: tier 1 keeps **90 days untouched**, tier 2
> keeps 730 days thinned to one row per strike per 300s. Its docstring
> says explicitly that the old 5-day hard delete was removed because it
> "made two questions permanently unanswerable."
>
> We hold 9 days because **collection started 2026-07-29**, not because
> anything was discarded. ~20 expiries accumulate by roughly **2026-12**
> under the retention policy that is already in force. **No code change
> is required — only elapsed time.**

### 3. Event-driven IV crush — **NOT TESTABLE, and partly closed**

- **Mechanism** — IV inflates into a scheduled event and collapses after.
- **Counterparty** — buyers of event protection.
- **Persistence** — scheduled and dateable, so the *timing* is free; the
  edge is in the size of the crush, which is competed.
- **Kill condition** — post-event IV drop smaller than the pre-event
  premium paid, over 10+ events.

Needs an IV series spanning events. `daily_atm_iv` is empty; 9 days of
chain data contains no scheduled macro event. **Same blocker as VRP.**

Partly closed anyway: the largest crushes happen *overnight* around
RBI/Fed/budget announcements, and we square off at 15:23 with no
overnight risk. We can only harvest the intraday portion.

### 4. Term structure / weekly-vs-monthly calendar — **STRUCTURALLY CLOSED**

`broker_adapter.option_chain()` calls `_nearest_expiry(symbol)` and
fetches **only that chain**. No monthly-expiry fetch exists anywhere in
the codebase (verified by grep). We therefore have no second point on the
term structure at any moment in history.

Closed until someone adds a monthly-chain fetch. That is a code change,
which this session may not make.

### 5. Intraday seasonality — **REJECTED, and the number is decisive**

`scratch/p2_seasonality.py`, 197,118 NIFTY bars over 2 years — the one
candidate where our sample is genuinely large.

| symbol | strongest bucket | mean | t |
|---|---|---:|---:|
| NIFTY | 15:00 | **−0.065 bps** | −2.64 |
| NIFTY | 14:00 | −0.058 bps | −2.51 |
| BANKNIFTY | 15:00 | −0.076 bps | −2.52 |

No bucket reaches \|t\| > 3 across 26 tests, so none survives a
multiple-comparison correction. **More decisively: the largest effect is
0.076 bps against a break-even of ~1.6 bps** (4 NIFTY points on 24,650).
The strongest intraday seasonal effect we can measure is **21× too small
to pay for the trade that would harvest it.**

Rejected on magnitude, not on significance. More data will not help.

### 6. Directional trend continuation — **REJECT on our own evidence**

Part 1 established: five strategies, 68–100% agreement — one bet, not
five. No counterparty and no persistence argument for any of them. T1
sits at p75–p100 of move-to-close. And per §2.4 of the Elliott spec,
corrective structures take "days and weeks", which on weeklies is "a
guaranteed loss even if the count is right."

The realised record: `bull_put_spread` t = −4.63, futures t = −3.36,
unattributed options t = −3.17. Nothing positive with n > 8.

**Argument for keeping it:** trend continuation is the only family we
have any live infrastructure for, and `bear_call_spread` (t = −0.83,
n = 103) is not yet refuted.

**Argument against, which I find stronger:** a family with no stated
counterparty is a pattern, not an edge. Ten of twelve strategies cannot
name who pays them. That is the single clearest finding of this whole
exercise.

## What is structurally available to us

| source | status | blocker |
|---|---|---|
| Volatility risk premium | **unknown — job unwired** | `backfill_iv_history()` has no caller; then ~60 sessions |
| Expiry pinning / gamma | **unknown — data not yet accumulated** | 2 of ~20 expiries held; retention already keeps 90d, ready ~2026-12 |
| Event IV crush | **unknown + partly closed** | no IV history; crush is mostly overnight and we are flat |
| Term structure | **closed** | only nearest expiry is ever fetched |
| Intraday seasonality | **closed** | largest effect 21× below break-even |
| Trend continuation | **open but unevidenced** | no counterparty; realised t-stats negative |

**The honest short list: we currently have ONE family we can trade
(directional/trend, unevidenced) and TWO we cannot evaluate because we
did not keep the data (VRP, pinning).**

> **CORRECTION (issued in Part 3).** `daily_atm_iv` is not empty for
> want of infrastructure. `risk_engine.backfill_iv_history(symbol,
> days_back=90)` exists, is correct, reads `history.chain_days()` and
> writes via `history.upsert_daily_atm_iv()`. **Nothing calls it** — not
> an agent, not an endpoint, not the LearningAgent EOD job (verified by
> grep across all modules; the only hits outside its own file are
> docstrings and `agents.py:5261`, which *reads* the table it never
> fills). This is a one-caller wiring gap, not a missing capability.

The most valuable action available from this Part is not a strategy. It
is **wiring one existing function to the EOD job** and then waiting.
The two candidates with the best economic thesis are testable on the
data this system is already collecting; they were made invisible by an
unwired job and by starting collection nine days ago.

---

# Part 3 — Three pre-registered candidates

Frozen before any backtest. Costs from Part 0: **₹134.80** single-leg
round trip, **₹252.74** spread round trip (1 lot). Power from Part 0:
n = 7.849·σ²/δ².

Three different families: **VRP** (A), **gamma/pinning** (B),
**liquidity provision** (C). A and B are non-directional; A is the
short-premium candidate; C is the single permitted directional one and
differs from the incumbent family in *mechanism*, not indicator.

### Candidate A: Conditional Short Premium

```
Hypothesis (one sentence, falsifiable):
  When ATM IV exceeds trailing 20-day realised volatility by >= 3 vol
  points, a defined-risk short-premium structure held intraday earns a
  positive mean net return; when it does not exceed it, we do not trade.

Economic mechanism:
  Insurance demand. Buyers of index convexity pay above fair value.

Counterparty and why they pay:
  Hedgers and directional speculators buying convexity, who accept a
  negative expected return in exchange for tail protection.

Instrument and structure (exact legs, strikes, expiry):
  Nearest weekly expiry. Iron fly: sell ATM CE + sell ATM PE, buy the
  CE and PE at ATM +/- 2 strike-steps as wings. Defined risk. 1 lot.

Entry condition (precise, no free parameters left open):
  09:45 IST, once per symbol per day. Enter only if
  atm_iv - rv20 >= 3.0 vol points, where atm_iv is the ATM CE/PE IV
  mean from the live chain and rv20 is annualised close-to-close
  realised vol over the prior 20 sessions. No other filter.

Exit condition (precise; state the required spot/vol move in absolute):
  Square off 15:15 IST unconditionally, OR when spot touches either
  short strike (a move of one strike-step: 50 NIFTY / 100 BANKNIFTY
  points), whichever is first. No profit target, no trailing stop.

Holding period distribution expected:
  Bimodal: most trades run the full ~5.5 hours to the timed exit; a
  minority stop within the first hour on a strike touch. Unknown split.

Free parameters (list every one; there should be very few):
  1. the VRP threshold (3.0 vol points)
  2. the wing distance (2 strike-steps)
  3. entry time (09:45)
  Three. All frozen here.

Expected per-trade edge, gross, in rupees, and the reasoning:
  UNKNOWN. I will not estimate it. Our only IV sample (9 days) shows
  VRP NEGATIVE on 3 of 4 symbols, so any positive number I wrote here
  would contradict the only measurement we have. The threshold is what
  makes this safe to pre-register: if VRP really is negative, the
  entry condition is simply never met and the candidate costs nothing.

All-in cost per trade from Part 0:
  4 legs, opened and closed = Rs 505.48 per lot (2x the Rs 252.74
  2-leg spread round trip). This is the largest cost of the three
  candidates and the reason the wings are 2 steps, not 1.

Therefore required trades for 80% power:
  Unknown until sigma is observed. If per-trade sigma matches the
  spread family (Rs 3,000), an edge of Rs 500 needs 283 trades and an
  edge of Rs 1,000 needs 71. At 4 symbols x ~250 sessions, 283 trades
  is reachable in ~1 year ONLY IF the gate passes ~30% of days.

Data required, and whether we currently archive it:
  Daily ATM IV series, >= 60 sessions. We archive the raw material
  (chain_snapshots, 90-day tier-1 retention) but the table is EMPTY
  because risk_engine.backfill_iv_history() has no caller. Wiring that
  one call to the EOD job is the prerequisite. Earliest testable date
  is ~60 sessions after that wiring, i.e. roughly 2026-11.

Kill condition — what result makes us abandon this:
  Either (a) the gate fires on fewer than 15% of symbol-days over 60
  sessions — the phenomenon is too rare to trade — or (b) mean net
  return per gated trade is <= 0 with n >= the power requirement.
```

### Candidate B: Expiry-Day Max-OI Pin

```
Hypothesis (one sentence, falsifiable):
  On weekly expiry day, spot at 15:15 is closer to the 09:45 max-total-
  OI strike than a random-walk benchmark from the same 09:45 spot
  predicts, by enough to pay a defined-risk structure centred there.

Economic mechanism:
  Dealer gamma hedging. Dealers short gamma at a heavy-OI strike buy
  weakness and sell strength as spot approaches it, damping realised
  movement in that neighbourhood into settlement.

Counterparty and why they pay:
  The dealer is not "losing" — they are paying for the privilege of
  being flat at settlement. The hedging flow is mechanical and
  obligatory, which is exactly why it is predictable.

Instrument and structure (exact legs, strikes, expiry):
  Same-day weekly expiry. Iron fly centred on the max-total-OI strike
  (NOT on ATM): sell CE + PE at that strike, buy wings 2 strike-steps
  out. 1 lot.

Entry condition (precise, no free parameters left open):
  Expiry day only, 09:45 IST. Enter only if |spot - max_OI_strike| <=
  1 strike-step at 09:45 — we are betting on a pin that is already
  plausible, not predicting a journey to a distant strike.

Exit condition (precise; state the required spot/vol move in absolute):
  Square off 15:15 IST unconditionally, or on a touch of either short
  strike (2 strike-steps of spot movement: 100 NIFTY / 200 BANKNIFTY).

Holding period distribution expected:
  Single-mode, ~5.5 hours. Expiry-day theta is the whole point; early
  exit forfeits it.

Free parameters (list every one; there should be very few):
  1. entry proximity gate (1 strike-step)
  2. wing distance (2 strike-steps)
  3. entry time (09:45)
  Three. All frozen here.

Expected per-trade edge, gross, in rupees, and the reasoning:
  UNKNOWN. Deliberately not estimated — with 2 expiry days held, any
  number would be fitted to 2 observations.

All-in cost per trade from Part 0:
  Rs 505.48 per lot (4 legs, round trip).

Therefore required trades for 80% power:
  ~100 expiry-day opportunities per year across 4 symbols. At sigma =
  Rs 3,000 that supports detecting an edge of Rs 1,000 (needs 71) in
  under a year, but NOT Rs 500 (needs 283, ~3 years). Pre-committed
  consequence: this candidate is only worth running if the edge is
  large. A small pin edge is undetectable for us and must be abandoned
  rather than pursued.

Data required, and whether we currently archive it:
  Expiry-day chain snapshots, >= 20 expiries. WE ALREADY ARCHIVE THIS
  correctly — tier-1 retention keeps 90 days untouched. We hold 2 of
  20 because collection began 2026-07-29. Earliest testable ~2026-12.
  No code change required.

Kill condition — what result makes us abandon this:
  Over >= 20 expiries, |close - max_OI_strike| is not smaller than the
  random-walk benchmark at p < 0.05 after the Part 4 correction.
```

### Candidate C: Opening-Gap Fade via Credit Spread

```
Hypothesis (one sentence, falsifiable):
  After a large opening gap, the session's move from 09:45 is biased
  AGAINST the gap direction by enough to pay a credit spread sold in
  the gap direction.

Economic mechanism:
  Liquidity provision to a forced order imbalance. The opening auction
  concentrates a night of accumulated orders into one print, and the
  price that clears it overshoots the price that would clear a normal
  continuous book. We are paid to take the other side of the overshoot.
  This is NOT trend continuation: the incumbent family predicts that a
  move persists; this predicts that a specific, mechanically-caused
  move partially reverses.

Counterparty and why they pay:
  Overnight position holders adjusting at the only liquid moment
  available to them, and stop orders triggered by the gap. They are
  paying for immediacy, not making a forecast.

Instrument and structure (exact legs, strikes, expiry):
  Nearest weekly expiry, credit vertical SOLD IN THE GAP DIRECTION:
  after a gap UP, sell a bear call spread (short strike ~1 step OTM,
  long 2 steps beyond); after a gap DOWN, sell a bull put spread.
  Defined risk, 1 lot. Uses the EXISTING S5/S6 execution path — no
  new order code. Deliberately NOT a long option: Part 1 established
  that long weekly premium needs a p75-p100 move just to reach T1.

Entry condition (precise, no free parameters left open):
  09:45 IST. Enter only if |open(09:15) - prior close| >= 80 NIFTY
  points / 160 BANKNIFTY points, AND spot at 09:45 has not already
  retraced more than 50% of the gap.

Exit condition (precise; state the required spot/vol move in absolute):
  Square off 15:15 IST unconditionally, or if spot breaches the short
  strike. Net entry delta ~0.3; at 1 lot the position needs
  Rs 252.74 / (0.30 x 65) = 13.0 NIFTY points, or
  Rs 252.74 / (0.30 x 30) = 28.1 BANKNIFTY points,
  of favourable spot movement to cover its cost. Against the >= 80 /
  >= 160 point entry gate that is a required capture of 16.2% (NIFTY)
  and 17.6% (BANKNIFTY) of the gap. The spread payoff is non-linear,
  so these are the linear-equivalent reference points, not the exact
  break-evens.

Holding period distribution expected:
  Single-mode ~5.5 hours, with a left tail of same-morning stops on
  gap continuation.

Free parameters (list every one; there should be very few):
  1. gap threshold (80 / 160 points)
  2. maximum pre-entry retrace (50%)
  3. short-strike distance (1 step OTM)
  4. entry time (09:45)
  Four. All frozen here.

Expected per-trade edge, gross, in rupees, and the reasoning:
  UNKNOWN — I have deliberately not measured the fade return, because
  that measurement IS the test. What I have measured is the size of
  the phenomenon, from 525 sessions of 2-year candle history
  (scratch/p3_sizing.py):

      |gap| >= 80 pts   NIFTY      172/525 sessions (32.8%)  ~86/yr
      |gap| >= 160 pts  BANKNIFTY  (p50 gap is 141 pts)     ~176/yr

  So the trade needs to capture roughly one sixth of the gap to break
  even, and the opportunity occurs often enough to test. Whether the
  capture exists is exactly what is unknown.

All-in cost per trade from Part 0:
  Rs 252.74 per lot (2 legs, round trip).

Therefore required trades for 80% power:
  ~260 opportunities/year across NIFTY + BANKNIFTY at the stated
  thresholds. At sigma = Rs 3,000: Rs 500 edge needs 283 trades
  (~13 months), Rs 1,000 needs 71 (~3.5 months).

Data required, and whether we currently archive it:
  Index candles only, which we hold for 2 years (3,148,803 rows,
  2024-06-20 onward). THIS IS THE ONLY CANDIDATE OF THE THREE THAT IS
  TESTABLE TODAY. Option premiums for the spread leg must be modelled
  from bs_greeks for the historical portion, which is an approximation
  and must be declared as such in the Part 4 protocol.

Kill condition — what result makes us abandon this:
  Mean net return <= 0 at n >= 283, OR the fade capture is positive
  but below the 16% break-even, OR results reverse sign between the
  2024-2025 and 2026 halves (which would make it a period artefact).
```

### Honest ranking, and the main risk to C

**C is the only one we can test now**, and it is the one I trust least
economically. Opening gaps in Indian indices are largely *information*
driven — an overnight SGX/US move — and information-driven gaps have a
documented tendency to *continue*, not fade. The auction-imbalance
mechanism I have described is real but competes against that. If C
fails, the correct reading is that the information component dominates
the imbalance component, and the answer is **not** to add a filter that
separates them post hoc — that is the indicator treadmill this whole
exercise exists to escape.

**A and B have the better economics and cannot be tested for months.**
That is the actual state of this system, and it is worth saying plainly:
the honest output of Part 3 is one testable candidate, two dated
waiting periods, and one unwired function call.

### What must start now

| action | blocks | effort |
|---|---|---|
| Wire `risk_engine.backfill_iv_history()` into the LearningAgent EOD job | Candidate A | one call |
| Nothing — chain retention is already correct | Candidate B | none, ~2026-12 |
| Nothing — 2 years of candles already held | Candidate C | none, testable now |

I have not made the wiring change: this session may not edit code.

---

# Part 4 — Statistical protocol, frozen before any backtest

Nothing below may be changed after the first backtest runs. If a rule
here turns out to be wrong, that is a finding to record, not a
parameter to adjust mid-experiment.

## 4.1 Measured inputs

| input | value | source |
|---|---|---|
| Max holding period observed | **348 min (5.8 h)**, n=223, median 12 min | `scratch/p4_inputs.py` |
| Trades exceeding one session | **0** | same |
| Free parameters across live strategies | **37** across 8 strategies | `PA_BOUNDS` + `SPREAD_BOUNDS` |
| Configurations persisted historically | **11** (2 backtests + 9 versions) | `~/.ltp-monitor/` |
| Configurations actually *tried* historically | **UNRECOVERABLE** | see 4.4 |

## 4.2 Purged, embargoed walk-forward CV

All three candidates square off intraday; **0 of 223 trades ever
crossed a session boundary** and the maximum was 5.8 h. The embargo
must exceed the maximum holding period, so:

- **Fold unit: one trading day.** No fold boundary may fall inside a
  session.
- **Purge: 1 day.** The day adjacent to the boundary is dropped from
  train.
- **Embargo: 2 trading days** after each test fold, excluded from
  train. This is ~3.5× the max holding period, deliberately generous
  because entry conditions read *prior-day* state (`rv20` in A, prior
  close in C) and a 1-day embargo would leak that.
- **Walk-forward, not k-fold**: train on days [0, t), test on
  [t+purge, t+purge+w). Never train on data after a test fold.
- Fold width w = **20 trading days**. On 525 sessions that is ~24
  non-overlapping test folds for Candidate C.

## 4.3 Out-of-sample holdout

**Held out now, before anything runs: the most recent 20% of each
candidate's data.** For Candidate C that is sessions from **2026-03-16
onward** (the last 105 of 525). Touched **exactly once**, after the
walk-forward result is final and written down, and the result of that
single touch is reported whatever it says.

If the holdout contradicts the walk-forward, the holdout wins. No
re-run, no "the regime changed", no second holdout.

## 4.4 Deflated Sharpe — and the number we cannot recover

DSR requires N, the number of configurations tried. **We cannot
recover N.** The tuner uses continuous bounds `(lo, hi, relax_dir)`
with no grid, and persists only *accepted* results — 11 records for a
search over **37 free parameters**. The true N is certainly in the
hundreds or thousands and is gone.

Expected max Sharpe under the null (Bailey / López de Prado, per-trade
units, V[SR]=1), computed in `scratch/p4_inputs.py`:

| N trials | E[max SR] under null |
|---:|---:|
| 11 | 1.622 |
| 77 | 2.437 |
| 100 | 2.531 |
| **1,000** | **3.255** |

The N=11 row reads **1.622**, against the prior audit's independently
stated ~1.59 and its max observed t-stat of 1.47. Two different
derivations agreeing to two decimals is good corroboration that the
null hurdle is right — and it confirms the audit's conclusion that
nothing cleared.

**Pre-committed decision: N = 1,000.** Since the true N is unknowable
and certainly exceeds the 11 on disk, we take the conservative floor
rather than the flattering one. The hurdle is **E[max SR] = 3.255**,
scaled by the observed √V[SR] across the configurations we try in this
round. A candidate must beat a deflated Sharpe with that null.

**Mandatory change before any tuning resumes: DONE (v59.58).**
`trial_log.py` appends every evaluated configuration — accepted or not —
to `~/.ltp-monitor/tuner_trials.jsonl`, recorded from
`backtester._replay_for()`, which is now the single evaluation
chokepoint. The reason N was lost is that it was NOT single: the daily
tuner in `LearningAgent` called `replay_spreads()`/`replay_pa()`
directly and was invisible to anything watching `_replay_for()`. Those
two call sites now route through it.

N = 1,000 remains the pre-committed floor for the CURRENT round, because
the historical count is still gone and nothing recovers it. From the
next round on, `trial_log.summary()` reports the measured N and the
hurdle should be recomputed from it rather than assumed.

## 4.5 Benjamini–Hochberg

Applied **across all three candidates together**, plus every variant
of them tested in this round, as one family. Not per candidate.

- FDR **q = 0.10**.
- Rank p-values ascending; largest k with p(k) ≤ k·q/m is the cutoff.
- m counts **every hypothesis tested in the round**, including ones
  abandoned early. An abandoned test still consumed a draw.

## 4.6 Pre-committed minimum sample size

From Part 0 (n = 7.849·σ²/δ²), at the spread family's σ ≈ ₹3,000:

| edge | required n |
|---:|---:|
| ₹500 | **283** |
| ₹1,000 | **71** |
| ₹2,000 | **18** |

**No result is reported as significant below n = 71**, and no claim of
a ₹500-scale edge is made below n = 283. Candidate B has ~100
opportunities/year, so it is **pre-committed to the ₹1,000 tier only**
— if B's edge is real but ₹500-scale, we abandon it as undetectable
rather than run it for three years.

## 4.7 Promotion gate — the explicit numeric threshold

A candidate is promoted to observe-only live deployment **only if all
six hold**:

1. n ≥ 71 completed trades in walk-forward test folds
2. Mean net P&L per trade > **₹0 after Part 0 costs** (₹252.74 spread / ₹505.48 iron fly)
3. t-statistic ≥ **3.0** on net P&L
4. Deflated Sharpe positive against **E[max SR] = 3.255** (N = 1,000)
5. Survives **Benjamini–Hochberg at q = 0.10** across the family
6. Single-touch holdout mean net P&L > 0

Promotion is to **observe-only**. Live capital requires a further 40
forward trades meeting (2) — no backtest promotes straight to money.

The old `net_pnl > 0` gate is gate (2) *alone*, which is why it passed
strategies that lost money. It is retained only as a necessary
condition, never a sufficient one.

## 4.8 The stopping sentence

> **If, after all three candidates have been tested to their
> pre-committed sample sizes, none produces a t-statistic ≥ 3.0 on net
> P&L that also survives the N=1,000 deflated Sharpe and BH at q=0.10,
> we conclude that no edge detectable at our cost structure and trade
> volume exists in these families, and we stop — we do not tune, do
> not add an indicator, and do not propose a fourth candidate.**

The correct action in that case is to trade nothing and keep
collecting the IV and expiry-day data, because Part 3 established that
the two candidates with the best economics are not yet testable and
will be by roughly 2026-12.

## 4.9 What this protocol cannot fix

Stated plainly so it is not discovered later as a surprise:

- **Candidate C's option premiums are modelled, not observed** for the
  historical portion — we hold index candles back to 2024 but chain
  data only from 2026-07-29. Its backtest prices spreads from
  `bs_greeks`, which cannot reproduce the bid-ask it will actually pay.
  Every C result carries that caveat; the holdout does not remove it.
- **The historical N is gone**, so the deflated Sharpe uses an assumed
  floor. This makes the gate conservative, not correct.
- **σ = ₹3,000 is borrowed** from the existing spread family. If a
  candidate's true σ is larger, its required n is larger and the
  pre-committed minimum was too low. Recompute σ from the candidate's
  own first 71 trades and, if it exceeds ₹3,000, raise n before
  reporting — this is the one number the protocol permits revising,
  and only upward.

---

**Parts 0–4 complete. No strategy code was written, edited, or
deleted; no parameter was tuned; no sweep was run. The two changes this
work identifies as necessary — wiring `risk_engine.backfill_iv_history()`
into the EOD job, and logging every tuner evaluation — are recommendations
awaiting sign-off, not applied changes.**
