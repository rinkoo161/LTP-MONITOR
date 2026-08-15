# Category A — closed. All five hypotheses answered.

**Run:** 2026-08-15. **Tools:** `fetch_expiry_calendar.py`,
`seasonality_expiry.py`, plus the earlier `seasonality_retro.py` /
`seasonality_dow.py`.

| | Result |
|---|---|
| SEAS-1 | **Rejected, reversed** — last 15 min are *quieter* |
| SEAS-1b | Real (volatility), 15:00–15:14 — but see SEAS-1c |
| SEAS-1c | **Direction does not pay.** Break-even 63.6%, measured 52.7% |
| SEAS-2 | **Null** |
| SEAS-3 | **Null** — now actually tested |
| SEAS-4 | **Null** |

**Nothing in Category A is tradeable.** The one live thread is closed by
arithmetic, not by opinion.

---

## 1. The expiry calendar, and why the shortcut would have lied

`fetch_expiry_calendar.py` reconstructs the calendar from NSE's own
bhavcopies — 150 sampled files, both formats, ~4 minutes:

| Index | Expiries | From |
|---|---|---|
| NIFTY | 441 | 2017-01-25 |
| BANKNIFTY | 450 | 2017-01-05 |
| FINNIFTY | 228 | 2021-02-04 |
| MIDCPNIFTY | 172 | 2022-02-15 |

The weekday it actually landed on:

```
NIFTY      2017-2024  Thu        2025  Thu:34 Tue:17     2026  Tue:35
BANKNIFTY  2017-2023  Thu        2024  Wed:37            2026  Tue:11
```

**A "weekly expiry is Thursday" rule would have mislabelled most of
2024–2026, and essentially all of BANKNIFTY 2024.** Because the error is
contiguous in time rather than scattered, it would have shifted the
estimate rather than blurring it — and produced a confident finding.
This is the single best justification for the earlier refusal to derive
the calendar by rule.

It also confirms the scoping constraint: NIFTY shows 12–13 expiries/year
in 2017–18 against 46+ from 2019, i.e. **NIFTY weekly options did not
exist before 2019**, so SEAS-3 is only well-posed from then.

---

## 2. SEAS-3 — tested, null

The hypothesis is about **shape, not level**, so each day's 25-block
profile is divided by that day's own mean before comparison. Otherwise a
simple "expiry days are more volatile" level difference would dominate
and answer a different question. Statistic is the L1 distance between
mean normalised profiles; the test is a permutation on the expiry labels
(the exact reference distribution, since under the null which days are
expiry days is arbitrary).

| Index | expiry / other days | L1 | p | q | control |
|---|---|---|---|---|---|
| BANKNIFTY | 193 / 1012 | 1.573 | 0.099 | 0.244 | 0.075 ⚠ |
| FINNIFTY | 192 / 1013 | 1.513 | 0.163 | 0.244 | 0.342 |
| NIFTY | 388 / 1489 | 1.008 | 0.282 | 0.282 | 0.779 |

**Nothing survives BH; smallest q = 0.244.**

BANKNIFTY's control came back at p=0.075 — not significant, but closer
to the line than a control should be, so its p=0.099 deserves *less*
credit than the number suggests, not more.

Level, reported separately as descriptive: expiry days are +5.5%
(BANKNIFTY) and +6.4% (FINNIFTY) more volatile overall, and −1.0% for
NIFTY. Even that is inconsistent in sign across indices.

**SENSEX is absent** — it is a BSE index and the fetcher reads NSE
bhavcopies. Closing that gap needs BSE's equivalent archive; it is a
known, stated omission rather than a silent one.

---

## 3. SEAS-1c — the cost model that closes SEAS-1b

SEAS-1b established the 15:00–15:14 block is ~2× the midday block. That
is volatility, not direction, so the question is whether direction in
that window is predictable enough to pay.

**Pre-registered**: does the sign of the 15:00 block predict the sign of
the 15:15 block? Train 2017–2021, test 2022–2026, NIFTY.

| | n | continuation | p |
|---|---|---|---|
| TRAIN 2017–2021 | 1162 | 47.2% | 0.065 |
| TEST 2022–2026 | 1132 | 47.3% | 0.070 |

Strikingly stable — a mild **reversal** tendency, i.e. a 52.7% call for
betting against continuation. Neither half is significant alone.

Now the arithmetic:

```
tradeable move   = the 15:15 block, median 6.0 bps   (NOT the 15:00
                   block's 10.2 bps — that has already happened)
edge             = 2 x 0.527 - 1 = 5.4% of |move|
E[gross], 1 lot  = Rs 25.7   (delta 0.50, lot 65, spot 24,400)
round-trip cost  = Rs 129
E[net]           = -Rs 103 per trade

break-even hit rate = 63.6%
measured            = 52.7%
```

**The edge is roughly five times too small to pay costs**, and scaling
lots does not help — cost scales with it.

### A correction to my own first attempt

My initial cost check said this cleared comfortably (₹566–1,618 gross
against ₹121–289 cost). **It was wrong twice over**: it used the 15:00
block's 10.2 bps as the gross — a move that has already happened and
cannot be traded — and it assumed the direction was known rather than a
52.7% call. Correcting both flips the conclusion from "clears" to "loses
₹103 a trade".

Both errors pointed the same way, which is the direction that makes a
strategy look viable. That is worth noting as a pattern, not just as one
mistake.

---

## 4. What Category A cost and what it bought

Five hypotheses, ~2,300 NIFTY sessions, free data, no paid source. Four
nulls, one rejection-with-reversal, and one real volatility effect that
does not survive costs.

The genuinely useful outputs are the negative results and three
measurement artefacts worth remembering:

1. **SEAS-2b** — the memo's literal wording overlaps its own predictor
   and reads 63–68% at p<0.0001 on all four indices. Implementing it as
   written would have shipped a false positive with a replication story.
2. **The indices are not independent** (r=0.84–0.98). Cross-index
   agreement here is near-worthless corroboration.
3. **The expiry weekday changed twice** in the sample window. Any
   analysis using a derived calendar is measuring its own assumption.

**Category B remains blocked** on historical option-chain data, which no
free endpoint provides — the chain archive holds 17 days and Dhan serves
a live snapshot only. That is a genuine sourcing decision with a cost
attached, and it is unchanged by any of this work.

---

## 5. Reproducing

```bash
python3 tools/fetch_expiry_calendar.py           # ~4 min, 150 files
python3 tools/seasonality_expiry.py              # SEAS-3
python3 tools/seasonality_dow.py  --db ~/.ltp-monitor/research_history.db
python3 tools/seasonality_retro.py --db ~/.ltp-monitor/research_history.db --profile
```

All read-only against `research_history.db`; production `history.db` is
never written to.
