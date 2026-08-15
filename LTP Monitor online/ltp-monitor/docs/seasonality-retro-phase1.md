# Phase 1 findings — SEAS-1 and SEAS-2 on the full free history

**Status:** complete for all data obtainable without payment.
**Tools:** `tools/backfill_index_history.py`, `tools/seasonality_retro.py`.
**Runs:** 2026-08-15, first on the 530-day local archive, then on
2,312 days of NIFTY after backfill.

**Headline:** SEAS-1 as written is **rejected, significantly, in the
opposite direction** — the last 15 minutes are *quieter* than the
typical block. SEAS-2 remains null. But the volatility profile shows the
memo's *mechanism* is real and its *window* was wrong by one block:
the pre-close event is at **15:00–15:14**, and that replicates
out-of-sample at p<1e-6.

---

## 1. Phase 0, answered

The memo asked how far back minute data goes and assumed a paid source
might be needed. Probing Dhan directly:

| Index | Earliest 1-minute data | Trading days now held |
|---|---|---|
| NIFTY | **2017-04-03** | **2,312** |
| BANKNIFTY | 2021-08-04 | 1,209 |
| FINNIFTY | 2021-08-04 | 1,209 |
| SENSEX | 2021-08-04 | 1,215 |

2015 and 2016 return nothing, so "since 2015" is unreachable — but
**~9.4 years of NIFTY is free**, and the earlier report's suggestion
that a paid source was needed was wrong. It answered Phase 0 by reading
the local database and never asked the broker.

**No paid source is required for Category A.** Category B remains
blocked: `chain_snapshots` holds 17 days and Dhan's chain endpoint
serves a live snapshot, not a history.

### Data integrity

The backfill writes to `~/.ltp-monitor/research_history.db`, **not**
`history.db`. Adding nine years under the same security_ids would
silently change every replay path and `backtest_s10.py`; that is a
decision about the production measurement substrate and belongs to the
operator. A separate file is reversible with `rm`.

Cross-checked against production on the overlap: **100.000% exact close
agreement across ~199,000 shared bars per index, max difference 0.0000**.
Two independently-fetched copies agreeing bar-for-bar is the strongest
available evidence that neither is corrupt.

---

## 2. SEAS-1: rejected, and reversed

Pre-registered family = {SEAS-1, SEAS-2a} × 4 indices, BH at q=0.10.

| Index | n | SEAS-1 | p | q | control C1 |
|---|---|---|---|---|---|
| NIFTY | 2312 | **45.8%** | 0.0001 | **0.0006** | 50.7% (p=0.49) ✓ |
| SENSEX | 1215 | **45.7%** | 0.0028 | **0.0113** | 49.8% (p=0.91) ✓ |
| BANKNIFTY | 1209 | 47.4% | 0.0745 | 0.199 | 45.6% (p=0.002) ✗ |
| FINNIFTY | 1209 | 48.7% | 0.388 | 0.554 | 46.1% (p=0.007) ✗ |

Two survive FDR — **both below 50%**. The hypothesis predicted *larger*
absolute moves in the last 15 minutes; the data says smaller. On the
530-day archive this was invisible (50.8%, p=0.76): the sample was
simply too small.

Controls are clean for the two survivors, which is what makes them
credible. For BANKNIFTY and FINNIFTY the control is itself significant,
so **those two indices' SEAS-1 numbers should not be read at all** —
when the placebo fails, the instrument is untrustworthy, not the
hypothesis confirmed.

The paired McNemar (last block vs random block, same day) agrees for
NIFTY: p=0.0015.

---

## 3. Why: the profile, and SEAS-1b

Median |15-minute return|, NIFTY, 2,312 days:

```
09:15   16.1 bps  #################################  <- the open
12:15    5.1 bps  ###############                    <- midday trough
15:00   10.2 bps  ###############################    <- second peak
15:15    5.9 bps  ##################                 <- what SEAS-1 tests
```

There *is* a large pre-close volatility event. It sits in **15:00–15:14**,
one block before the window SEAS-1 examines. Testing "the last 15
minutes" misses it entirely and instead samples a quiet block, which is
why the result came out reversed.

Checked before believing it: every block including the last holds a
median of **15 bars** (min 14), so this is not a truncated-final-block
artefact.

**SEAS-1b** — pre-registered before running: *block 23 |return| exceeds
the median of the other 24 at a rate above 50%.*

| Index | n | rate | p | provenance |
|---|---|---|---|---|
| BANKNIFTY | 1209 | 60.5% | <1e-6 | **out-of-sample** |
| FINNIFTY | 1209 | 62.6% | <1e-6 | **out-of-sample** |
| NIFTY | 2312 | 66.3% | <1e-6 | contaminated (hypothesis came from here) |
| SENSEX | 1215 | 61.7% | <1e-6 | contaminated |

The hypothesis was generated from the NIFTY and SENSEX profiles, so
those two cannot confirm it. BANKNIFTY's and FINNIFTY's profiles had not
been examined when SEAS-1b was written, making them a genuine — if
imperfect, since their SEAS-1 numbers had been seen — out-of-sample
test. **Both confirm it decisively.**

> **Correction (2026-08-15, added after measuring cross-index
> correlation — see `seasonality-seas3-seas4.md` §3).** Calling this
> "two out-of-sample replications" overstates it. Daily open→close
> returns correlate r=0.84 (NIFTY/BANKNIFTY), r=0.87 (NIFTY/FINNIFTY)
> and r=0.98 (NIFTY/SENSEX). These are not independent markets, and an
> intraday volatility-shape effect is a property of the market they
> share. The profiles genuinely had not been looked at, so the window
> was not fitted to them — but four correlated indices agreeing is
> closer to one observation than four. SEAS-1b still stands on its
> effect size (60–66% against a 50% null, visible directly in the raw
> profile); the corroboration is worth much less than the table
> suggests.

SEAS-1b is deliberately **excluded from the BH family**. It was not in
the original pre-registration, and admitting it afterwards would let a
post-hoc hypothesis borrow a pre-registered one's credibility.

A candidate structural mechanism, stated as a candidate: NSE derives the
closing price from a VWAP over the final half hour, so 15:00 starts a
window with different incentives. This script does not test that, and
the correlation would look identical if the cause were something else.

---

## 4. SEAS-2: null, and the artefact confirms itself

| Index | SEAS-2a (disjoint) | SEAS-2b (as memo words it) |
|---|---|---|
| NIFTY | 51.3% (p=0.23) | 63.4% (p<1e-4) |
| BANKNIFTY | 48.9% (p=0.45) | 62.8% (p<1e-4) |
| FINNIFTY | 50.7% (p=0.65) | 65.2% (p<1e-4) |
| SENSEX | 51.7% (p=0.25) | 65.0% (p<1e-4) |

**SEAS-2 is null.** Nothing survives BH.

SEAS-2b — the memo's literal wording, where ORB is a *subset* of the
outcome window — again reads 63–65% at p<0.0001 on all four indices, now
on 4× the data. It is arithmetic, and it is stable enough to look
exactly like a discovery. Implementing the memo as written would produce
a confident false positive with a four-index replication story.

---

## 5. Stability

NIFTY SEAS-1 by year: 54.1 / 46.5 / 41.0 / 43.8 / 38.7 / 45.7 / 42.0 /
**52.4** / 46.8 / **51.6** (2017→2026).

Predominantly below 50% from 2018–2023, but back at ~52% in 2024 and
~52% in 2026. **The effect may be decaying**, or the last two years may
be noise. Per-year cells are underpowered and excluded from the family;
this is a caution, not a finding.

---

## 6. What this does and does not license

**Does not:**
- SEAS-1b is a statement about **volatility, not direction**. It says
  the 15:00 block moves more. It does not say which way.
- **No costs are modelled anywhere in this work.** Against the system's
  measured bid-ask, a volatility-timing filter has to clear a bar this
  analysis never approaches.
- Nothing here is a strategy, a filter, or a config change. Phase 3
  attachment stays gated behind `derivatives-third-eye`.

**Does:**
- SEAS-1 and SEAS-2 are answered and should not be re-tested on this
  data.
- A concrete, replicated, mechanism-plausible time window exists for any
  future work that wants one.

### Recommended next

1. **Directional and cost-aware follow-up on the 15:00 window** —
   pre-registered separately. The question is not "is 15:00 volatile"
   (answered) but "is anything about it tradeable after costs", which
   this work says nothing about.
2. **SEAS-3 (expiry-day shape)** is now much more attractive: with 2,312
   NIFTY days there is real power, and its mechanism (pinning, dealer
   gamma) is the most specific of the four.
3. **Category B still blocked** on option-chain history, which is a
   genuine sourcing problem no free endpoint solves.

---

## 7. Reproducing

```bash
python3 tools/backfill_index_history.py --dry-run     # ~121 paced calls
python3 tools/backfill_index_history.py               # ~3 min
python3 tools/seasonality_retro.py --db ~/.ltp-monitor/research_history.db
python3 tools/seasonality_retro.py --db ~/.ltp-monitor/research_history.db --profile
```

The backfill is idempotent and resumable; production `history.db` is
never written to.
