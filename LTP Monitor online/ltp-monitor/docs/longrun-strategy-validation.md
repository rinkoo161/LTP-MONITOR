# Long-run strategy validation — every testable strategy, full free history

**Run:** 2026-08-18, `tools/longrun_strategy_validation.py`.
**Data:** NIFTY 2017-04→2026-08 (2,319 sessions); BANKNIFTY / FINNIFTY /
SENSEX 2021-08→2026-08 (~1,248 each). Isolated store built from the
research backfill; production `history.db` never opened.
**What ran:** the LIVE replay functions with the LIVE tuned parameters
(`get_params`, bounds-clamped active versions). 28 cells, **46,623
replay trades**. Raw output: `longrun-results-2026-08-18.json`.

**One-line verdict: nine years of data confirm the standing conclusion —
no strategy shows a consistent edge — and produce one genuinely
actionable negative: momentum_confluence loses in every market, every
period, at scale.**

---

## What this does and does not measure

- PA replays trade a **spot proxy** with the replay fee model. Signal
  quality, not broker P&L.
- Chain-dependent gates (v59.86 reachability, etc.) **fail open before
  2026-07-29** — early years run MORE permissively than live.
- Spreads, momentum_buy and S10 are **excluded, not approximated**: they
  need the option-chain archive, which covers 17 days.
- The tuned params were fitted on recent data, so 2017–2023 is genuine
  out-of-sample for them — backwards. It is a fair test, and most fail it.
- NIFTY/SENSEX correlate 0.98 and BANKNIFTY/FINNIFTY 0.95: four columns
  are roughly **two** independent observations, not four.

## NIFTY, net ₹k by year (the only 9-year sample)

| strategy | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | +yrs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| orb | −16 | +55 | −25 | **+106** | −21 | −13 | −22 | −0 | +63 | −52 | 3/10 |
| vwap_pullback | −12 | +14 | +60 | −25 | +52 | +46 | −4 | +84 | −13 | +72 | 6/10 |
| ema_mtf | −7 | −4 | +10 | −7 | −16 | +13 | −3 | +18 | −8 | +1 | 4/10 |
| sg_ema | −15 | −2 | +29 | +64 | +33 | −7 | +24 | +34 | −15 | −1 | 5/10 |
| momentum_confluence | −26 | −50 | −26 | −59 | −106 | −153 | −40 | −25 | +7 | +10 | 2/10 |
| ew_reversal | +0 | −5 | +7 | +50 | +15 | +7 | −5 | −7 | −1 | +12 | 5/10 |
| ta_elliott | −3 | −15 | +12 | +70 | −33 | +12 | −8 | +38 | −7 | +64 | 5/10 |

## Shared window 2021-08+, four indices (net ₹k)

| strategy | NIFTY | BANKNIFTY | FINNIFTY | SENSEX |
|---|---|---|---|---|
| orb | −45 | −106 | −135 | −38 |
| vwap_pullback | +237 | −59 | −166 | +122 |
| ema_mtf | +4 | −85 | −58 | −9 |
| sg_ema | +69 | −104 | +13 | +7 |
| **momentum_confluence** | **−307** | **−428** | **−361** | **−15** |
| ew_reversal | +21 | +88 | −6 | −12 |
| ta_elliott | +66 | −105 | −66 | +57 |

---

## Findings

**F1 — momentum_confluence is a consistent, high-volume loser.
[ACTIONABLE]** Negative in 8 of 10 NIFTY years and on all four indices
in the shared window (−₹1.11M combined, 17,579 trades). This is not
regime luck: it loses in trend years, chop years, and crash years, on
both correlated pairs. It is also the highest-volume signal source live
(it dominated the 231 rejections on 2026-08-17, so the gates are already
absorbing most of its output). **Proposal, per rule 8 — operator
decision, not applied:** disable `momentum_confluence`, or set it
observe-only. Nothing in nine years suggests the gates are suppressing a
winner.

**F2 — no strategy earns a positive verdict.** The best consistency is
vwap_pullback at 6/10 years — but its cross-symbol pattern (+NIFTY,
+SENSEX, −BANKNIFTY, −FINNIFTY) splits exactly along the correlation
pairs: one supportive observation, one contrary. orb's 9-year total
(+75k) is two years (2018, 2020) carrying eight; 3/10 positive years is
a coin flip with survivor glow. ta_elliott and sg_ema alternate sign by
year and by index. ew_reversal is the least bad (positive on the two
larger samples, 137+113 trades) but at ~15 trades/year per symbol the
sample is too thin to promote — it stays observe-only on its merits.

**F3 — the tuned versions do not transfer.** These are the parameters
the auto-tuner/optimizer settled on against recent data. Run against
2017–2023 they mostly lose, which is what overfitting to a short window
looks like, and is consistent with the promotion gate's standing 0-of-11
verdict. The tuner's output should continue to be treated as proposals
against ≤weeks of data, never as validated.

**F4 — the year splits, not the totals, are the result.** Any strategy
here can show a profitable *total* by choosing the window (orb 2020:
+106k). Ten-year year-by-year sign sequences are the honest display, and
none of them looks like an edge plus noise; they look like noise plus
regime.

## Recommended next steps (all operator decisions)

1. **Disable or observe-only `momentum_confluence`** (F1). One config
   key; reversible; the single highest-expected-value change this data
   supports.
2. Keep every other strategy in paper/observe with unchanged params —
   nothing here justifies promotion, and nothing except F1 justifies
   removal on this evidence.
3. When the option-chain archive has accumulated (≥60 sessions), run the
   same grid for the spread strategies — the only family with a real
   credit-capture mechanism, and currently the only one actually trading.

## Reproducing

```bash
# build the isolated store (see tool docstring), then:
LTP_MONITOR_HOME=<store> python3 tools/longrun_strategy_validation.py
```
Checkpointed per cell; resumable; ~17 minutes total.
