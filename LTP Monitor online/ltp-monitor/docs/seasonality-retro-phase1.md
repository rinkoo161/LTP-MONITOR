# Phase 1 findings — SEAS-1 and SEAS-2 on the existing archive

**Status:** complete for the data that exists. Feeds the gated research
memo per `research-memo-addendum-seasonality-flow.md`.
**Tool:** `tools/seasonality_retro.py` (pre-registration frozen in its
docstring before any result was looked at).
**Run:** 2026-08-15.
**Result in one line: no evidence for either hypothesis, and the memo's
literal SEAS-2 formulation would have produced a false positive on all
four indices.**

---

## 1. Phase 0, partially answered as a side effect

The memo's Phase 0 asks "what data do we have back to 2015, and from
where". It is **not** answered here — but one half of it now is, and the
answer bounds everything below.

| | |
|---|---|
| Instruments | NIFTY (sid 13), BANKNIFTY (25), FINNIFTY (27), SENSEX (51) |
| Granularity | 1-minute bars, 375/day median, 09:15–15:29 |
| **Range** | **2024-06-20 → 2026-08-14** |
| Trading days | 531 (528–530 admitted per index) |

**The archive reaches 2024, not 2015.** Every hypothesis scoped "since
2015" is therefore unanswerable from local data today, and the paid-source
question in Phase 0 step 2 is live rather than hypothetical. What exists
is ~2.2 years, which is enough for SEAS-1 and SEAS-2 as scoped and not
enough for anything seasonal at annual frequency.

Note also `daily_ohlc` holds only 18 days and the `*_SPOT_1m` series
start 2026-07-24. The 2024+ history lives **only** in the numeric
security-id rows. Anything reading the `_SPOT_1m` series for history
will silently get three weeks.

---

## 2. What was tested

Per the memo's section 5, SEAS-1 and SEAS-2 **only**. No FLOW-*, no
SEAS-3/4, no filter, no strategy, no config key.

Unit of observation is **one trading day**, so day-clustering is handled
by construction rather than by a correction bolted on afterwards. Day
admission required ≥360 of 375 bars and all 25 blocks non-empty; exactly
one day per index was dropped (`short_session`), and drops are printed
by reason.

Two design decisions changed the answer, and both are worth keeping:

**SEAS-1 excludes the tested block from its own reference median.**
Comparing a block against a median that includes it gives a null of
12/25 = **0.48**, not 0.50. At n=531 that 2-point bias alone manufactures
a "significant" result out of nothing.

**SEAS-2 is reported twice**, because the memo's own wording ("ORB
direction vs the day's close-to-close direction") specifies overlapping
windows — the ORB is a *subset* of the full day. That is measured as
SEAS-2b and flagged; SEAS-2a (ORB → rest-of-day, disjoint) is the
primary test and the only tradeable version.

---

## 3. Results

Pre-registered family: {SEAS-1, SEAS-2a} × 4 indices = 8 tests,
Benjamini-Hochberg at q=0.10.

| Index | SEAS-1 | SEAS-2a | SEAS-2b *(contaminated)* |
|---|---|---|---|
| NIFTY | 50.8% (p=0.76) | 51.4% (p=0.54) | 65.4% (p<0.0001) |
| BANKNIFTY | 47.1% (p=0.19) | 51.4% (p=0.54) | 66.5% (p<0.0001) |
| FINNIFTY | 48.8% (p=0.60) | 51.6% (p=0.49) | 67.1% (p<0.0001) |
| SENSEX | 46.0% (p=0.07) | 53.4% (p=0.13) | 67.8% (p<0.0001) |

n ≈ 528–530 days per cell. Null is 50% in every column.

**Not one of the 8 pre-registered tests survives BH at q=0.10.** The
smallest q-value in the family is **0.51**. The smallest raw p is 0.074
(SENSEX SEAS-1) — and it points *below* 50%, i.e. the last 15 minutes
being *less* volatile than the session median, the opposite of the
hypothesis.

### The one large, highly significant effect is an artefact

SEAS-2b reads 65–68% at p<0.0001 on **all four indices** — exactly what
a mechanically-overlapping window produces under a pure random walk,
since ORB and full-day share their first 15 minutes. Had this been
reported as the memo's wording literally specifies, it would have looked
like a strong, cross-index-replicated discovery. It is arithmetic.

### The controls beat the hypotheses

| Index | SEAS-1 p | control C1 p |
|---|---|---|
| BANKNIFTY | 0.192 | **0.019** |
| FINNIFTY | 0.602 | **0.019** |

C1 substitutes a random non-final block for the last one and must show
nothing. It scored *more* significant than the real hypothesis on two
indices; C2 (random sign) reached p=0.012 on BANKNIFTY against the real
SEAS-2a's p=0.543.

Under the memo's own instruction — *if a control scores like the real
thing, the real thing is the control* — this is decisive.

### Post-hoc, and reported as such

C1 landing further from 50% than SEAS-1 can only happen if 15-minute
blocks are **not exchangeable** within a day, which they plainly are not
(intraday volatility is U-shaped). So the pre-registered 0.5 null is not
quite the right null for SEAS-1.

The comparison that does not rely on it is last-block vs random-block
**on the same day** — paired, hence McNemar:

| Index | discordant | p |
|---|---|---|
| NIFTY | 141/286 | 0.859 |
| BANKNIFTY | 151/290 | 0.518 |
| FINNIFTY | 153/285 | 0.236 |
| SENSEX | 127/279 | 0.151 |

Nothing. This test was added **after** seeing the control's behaviour,
is labelled post-hoc in the tool, and is deliberately kept outside the
BH family — promoting it after the fact is precisely what
pre-registration exists to prevent. It is reported because it is the
*more* sensitive test and it also finds nothing, which strengthens the
negative rather than rescuing a positive.

### Stability

Per-year and first/second-half splits are printed by the tool and are
descriptive only (cells are underpowered). They do not agree with each
other in sign — NIFTY SEAS-1 runs 57.6% / 47.0% / 51.0% across 2024 /
2025 / 2026. A real effect does not do that.

---

## 4. What this means for the memo

**SEAS-1 and SEAS-2 are answered NEGATIVE on 2024-06→2026-08.** They
should not proceed to Phase 3, and no filter should be attached.

This is recorded as a negative result rather than dropped. Re-testing
either hypothesis on new data without knowing it already failed here is
how a spurious positive eventually gets adopted — the same reason the C3
wall-predictiveness result is kept in `scratch/wall_predictiveness.py`.

Three qualifications, stated because the negative is only as strong as
its scope:

1. **~2.2 years, one regime.** A genuine seasonality effect could exist
   and be invisible here. The correct response is the Phase 0 data
   question, not a re-run of this script.
2. **Not tested: SEAS-3 (expiry-day vol shape) or SEAS-4 (day-of-week).**
   Out of section 5's scope. SEAS-3 in particular is untouched and is the
   one with the most specific mechanism (pinning, dealer gamma).
3. **Costs are not modelled at all.** Irrelevant given the result, but it
   would matter the moment any cell survived.

### Recommended next step

Phase 0's data-sourcing question, which now has a concrete number
attached: local history starts **2024-06-20**. Whether to buy history
back to 2015 is an operator decision with a cost attached, and it gates
Category B entirely.

**Not recommended:** widening the hypothesis set against this same
2.2-year window. Eight tests already returned nothing with a best
q-value of 0.51; adding SEAS-3 and SEAS-4 to the same data mostly buys
more opportunities for a false positive.

---

## 5. Reproducing

```bash
python3 tools/seasonality_retro.py               # full report
python3 tools/seasonality_retro.py --json        # machine-readable
python3 tools/seasonality_retro.py --min-days 60 # loosen the refusal floor
```

Read-only against `~/.ltp-monitor/history.db`; writes nothing. Cells
below `--min-days` (default 100) report "insufficient sample" and no
percentage, per CLAUDE.md rule 7.
