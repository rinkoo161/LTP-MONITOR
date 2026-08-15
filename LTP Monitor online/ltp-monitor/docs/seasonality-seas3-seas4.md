# SEAS-3 and SEAS-4 — the remaining Category A hypotheses

**Tool:** `tools/seasonality_dow.py` (SEAS-4). SEAS-3 has no tool,
deliberately.
**Run:** 2026-08-15, on the backfilled archive (`research_history.db`).

**Summary: SEAS-4 is a clean null. SEAS-3 cannot be tested at all —
the data required does not exist and cannot be safely invented.**

---

## 1. SEAS-3 — BLOCKED, and not attempted

> *Weekly expiry days show different intraday volatility shape than
> non-expiry days.*

This needs a historical weekly-expiry calendar back to 2017. It does not
exist here:

| Source | What it holds |
|---|---|
| `instruments` table | 30 (symbol, expiry) pairs, **2026-07-21 onward only** |
| Dhan `/optionchain/expirylist` | 18 NIFTY expiries, earliest **2026-08-18** — all FUTURE |

Verified 2026-08-15. No past expiry date is obtainable from either.

**The tempting shortcut is the dangerous one.** Weekly expiry could be
"derived" as every Thursday. That would be wrong: NSE has changed index
weekly-expiry weekdays more than once over this window, so a
rule-of-thumb calendar would mislabel a large and *systematically
clustered* fraction of days — and mislabelled days in a volatility-shape
comparison do not add noise, they move the estimate. It is the same
failure this codebase already records as "reproducing the *shape* of a
data structure instead of its *meaning*", which has silently zeroed out
entire strategies before.

So SEAS-3 is left untested rather than tested badly. **What would
unblock it:** a genuine historical expiry calendar — NSE's F&O bhavcopy
archive carries expiry dates per contract, or the operator can supply
one. It is a small file, and it is the only missing input.

---

## 2. SEAS-4 — tested, null

> *Day-of-week effects in direction or volatility.*

Method is the memo's own: *"mean return / vol by weekday, tested for
significance vs. random day assignment"* — an omnibus **permutation
test** (20,000 shuffles, seeded) on the spread between the five weekday
means. No distributional assumption, which matters because daily index
returns are fat-tailed and a normal-theory ANOVA would overstate
significance on exactly this data.

Family = {direction, volatility} × 4 indices = 8 tests, BH at q=0.10.

| Index | direction p | volatility p |
|---|---|---|
| NIFTY | 0.730 | 0.512 |
| BANKNIFTY | 0.341 | 0.768 |
| FINNIFTY | 0.353 | 0.804 |
| SENSEX | 0.156 | 0.576 |

**Nothing survives. The smallest q in the family is 0.804.** Controls
(scrambled weekday labels) returned p = 0.63 / 0.56 / 0.74 — the
permutation machinery is behaving.

The permutation test was itself validated before use: p=0.76 on pure
noise, p=0.0005 on a planted Monday effect.

### The descriptive pattern that is *not* a finding

Mean open→close by weekday, in basis points:

| | Mon | Tue | Wed | Thu | Fri |
|---|---|---|---|---|---|
| NIFTY | +6.6 | −4.5 | +1.8 | −4.2 | −1.4 |
| BANKNIFTY | +9.9 | −5.0 | +1.1 | −3.9 | −1.9 |
| FINNIFTY | +9.0 | −5.3 | +0.1 | −3.4 | −1.9 |
| SENSEX | +6.7 | −7.6 | −0.7 | −7.8 | −3.3 |

Monday positive and Tuesday/Thursday negative in **all four**. It looks
like a replicated pattern. It is not: the omnibus says p = 0.16–0.73,
and the four indices are not four samples (see below). Reporting the
Monday cell alone would be the exact multiple-comparison error the
omnibus exists to prevent.

### Weekend sessions

The first run crashed with `IndexError` on a weekday of 5. NSE does
trade some weekends: Union Budget Saturdays (2020-02-01, 2025-02-01), a
Budget **Sunday** (2026-02-01), Muhurat (2021-11-13), and a
disaster-recovery live session (2024-01-20).

They are excluded and listed in the output — a Budget-day session is a
sample of Budget day, not of "what Saturdays are like". The crash was
the good outcome: had the weekday table simply had seven entries, those
five would have formed two junk cells and nothing would have said so.

---

## 3. A correction that applies to the earlier SEAS-1b result

Measured daily open→close return correlation between the indices:

| pair | r |
|---|---|
| NIFTY vs SENSEX | **+0.978** |
| BANKNIFTY vs FINNIFTY | +0.952 |
| NIFTY vs FINNIFTY | +0.867 |
| NIFTY vs BANKNIFTY | +0.837 |
| FINNIFTY vs SENSEX | +0.860 |
| BANKNIFTY vs SENSEX | +0.829 |

**These are not four independent markets.** NIFTY and SENSEX are very
nearly the same series.

`docs/seasonality-retro-phase1.md` describes SEAS-1b as confirmed
"out-of-sample" on BANKNIFTY and FINNIFTY. That remains true in the
sense that matters most — their volatility profiles had not been
examined when the hypothesis was written, so the window was not fitted
to them. **But it is weaker evidence than "two independent
replications" implies**, because an intraday volatility-shape effect is
a property of the market those indices share, and at r≈0.85 they carry
much less independent information than their sample sizes suggest.

The same caveat applies to every "confirmed on all four indices"
statement in this work, including SEAS-2b's artefact — four correlated
indices agreeing is closer to one observation than four.

It does not overturn SEAS-1b: the effect is very large (60–66% against a
50% null, p<1e-6) and the pre-close volatility peak is visible directly
in the raw profile. It does mean the *strength* of the out-of-sample
claim was overstated, and any future work should treat cross-index
agreement here as near-worthless corroboration.

---

## 4. Category A status

| | Status |
|---|---|
| SEAS-1 | Answered — **rejected, reversed** (last 15 min are quieter) |
| SEAS-1b | Post-hoc, large, replicated — pre-close event at **15:00–15:14** |
| SEAS-2 | Answered — **null** |
| SEAS-3 | **Blocked** — no historical expiry calendar |
| SEAS-4 | Answered — **null** |

Category A is complete except SEAS-3, and SEAS-3 is blocked on a data
file rather than on analysis.

Nothing in Category A has produced a tradeable claim. The one live
thread is SEAS-1b, which is a statement about **volatility, not
direction**, with **no costs modelled anywhere**.

**Recommended next:** obtain a historical expiry calendar and close out
SEAS-3, which has the most specific mechanism of the four (pinning,
dealer gamma) and now has 2,312 NIFTY sessions of power behind it.
Category B remains blocked on option-chain history.

---

## 5. Reproducing

```bash
python3 tools/seasonality_dow.py --db ~/.ltp-monitor/research_history.db
python3 tools/seasonality_dow.py --db ~/.ltp-monitor/research_history.db --permutations 100000
```

Read-only; writes nothing; shares every statistic with
`seasonality_retro.py` by import rather than by copy.
