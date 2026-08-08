# Tier 1 — Statistical Validity

This tier answers: **does the evidence actually support the claim?** Most trading systems
fail here not from bad math but from counting the wrong things — treating correlated trades
as independent, and testing many strategies while applying a single-test threshold.

## Contents
- [Count independent bets, not trades](#count-independent-bets-not-trades)
- [The multiple-testing problem](#the-multiple-testing-problem)
- [Deflated Sharpe](#deflated-sharpe)
- [Purged and embargoed cross-validation](#purged-and-embargoed-cross-validation)
- [Lookahead and leakage](#lookahead-and-leakage)
- [Sample size reality check](#sample-size-reality-check)
- [Pre-registration](#pre-registration)
- [Checklist](#checklist)

---

## Count independent bets, not trades

A trade count overstates evidence whenever trades share a driver. Sources of correlation:

- **Same-day trades on the same symbol** — driven by one day's regime, not N independent draws.
- **Correlated symbols** — NIFTY and BANKNIFTY move together; a signal firing on both is
  closer to one bet than two.
- **Overlapping holding periods** — positions open simultaneously share the same market path.
- **Same signal, multiple strikes** — one directional view expressed across a chain.

Ask the author to state the effective number of independent bets and justify it. A reasonable
conservative approach for intraday index strategies is to treat one symbol-day as one
observation unless the trades within it are demonstrably driven by different factors.

The practical consequence is severe: 40 trades might represent 8-12 independent observations,
which is far too few to distinguish any realistic edge from noise.

## The multiple-testing problem

This is the single most common invalidator of "we found a profitable strategy."

If you test N strategies and pick the best, the best one's performance is inflated by
selection. The relevant comparison is not "is this t-statistic above 2?" but "is it above
what the *maximum of N draws from pure noise* would produce?"

Rough guide for the expected maximum |t| from N independent zero-edge strategies:

| N strategies tested | Expected max \|t\| under the null |
|---|---|
| 1 | ~0.8 |
| 5 | ~1.4 |
| 10 | ~1.6 |
| 20 | ~1.9 |
| 50 | ~2.2 |
| 100 | ~2.5 |

So a family of ~11 strategies whose best t-statistic is ~1.47 has produced **less** than
noise would be expected to produce. That is not weak evidence of edge; it is evidence
consistent with no edge at all.

Checks:
- How many strategies, variants, and parameter sets were tried in total? Include abandoned
  ones — they count. Include parameter sweeps: a sweep over 1,000 combinations is 1,000 tests.
- Was any correction applied across the family? Benjamini-Hochberg (controls false discovery
  rate) is the usual choice and is less conservative than Bonferroni.
- **Is the correction applied across the whole family, or only across the survivors?**
  Correcting only over strategies that already passed a filter reintroduces the bias.

## Deflated Sharpe

The Sharpe ratio from a backtest is inflated by the search that produced it. The Deflated
Sharpe Ratio (Bailey & López de Prado) adjusts the observed Sharpe for:

- the number of independent trials,
- non-normality of returns (skew and kurtosis — options returns are strongly non-normal),
- sample length.

Checks:
- Is DSR computed, or only raw Sharpe? Raw Sharpe on a selected strategy is not evidence.
- Are the skew and kurtosis inputs computed from actual trade returns? Option-buying
  strategies typically have positive skew and high kurtosis; premium-selling strategies have
  negative skew — both violate the normality that plain Sharpe assumes, in opposite directions.
- Is the trial count honest? Understating N inflates DSR directly.

## Purged and embargoed cross-validation

Standard k-fold cross-validation leaks in time-series data. Two mechanisms:

- **Label overlap:** if a trade's outcome is determined over a holding window, a training
  sample whose window overlaps a test sample shares information with it. The fix is
  **purging** — remove training samples whose label windows overlap the test set.
- **Serial correlation across the boundary:** features near the test-set boundary carry
  information from just outside it. The fix is an **embargo** — drop a buffer of samples
  after the test set before resuming training data.

Checks:
- Is CV used at all, or just a single train/test split? A single split is one draw and
  invites re-splitting until the result looks good.
- If CV is used, is it purged and embargoed? Plain `KFold` or `TimeSeriesSplit` without
  purging is insufficient for overlapping labels.
- Is the embargo period at least as long as the maximum holding period?
- Is normalisation (z-scoring, scaling) fitted on training folds only, or on the whole
  dataset before splitting? The latter is a classic leak.

## Lookahead and leakage

Specific things to grep for in backtest code:

- **Signal bar close used as fill price.** If the signal is computed from a bar's close, the
  fill cannot be at that same close — you didn't know the close until the bar ended. Fill at
  the next bar's open, and check that the code does this.
- **Indicators computed on the full series then sliced.** Rolling computations over the whole
  array before the backtest loop leak future values into early bars.
- **`shift()` direction errors.** Off-by-one in the wrong direction is a lookahead that
  produces spectacular backtests.
- **Option chain snapshot timing.** If the chain snapshot used to pick a strike was taken
  after the signal timestamp, the strike choice used future information.
- **Survivorship in the instrument set.** Backtesting only contracts that had liquidity
  throughout excludes exactly the ones that would have caused problems.
- **Restated or adjusted data.** Index levels and corporate-action-adjusted prices may not
  match what was observable in real time.
- **Stop and target both hit within one bar.** Which does the backtest assume filled first?
  Optimistic resolution (target first) inflates results substantially. Requires intrabar data
  or a conservative assumption (stop first).

## Sample size reality check

Before accepting any performance claim, compute how many independent bets would be needed to
detect the claimed edge.

Rough rule: to detect an edge with per-trade Sharpe `s` at conventional power, you need on the
order of `(2.8 / s)²` independent trades. For a realistic intraday per-trade Sharpe of 0.05,
that is roughly 3,000 independent bets. At 0.10, roughly 800.

This is why forty trades cannot demonstrate anything, and why a strategy that "worked for a
month" carries almost no information. State this explicitly in the review with the actual
numbers — it is far more persuasive than a general caution.

## Pre-registration

The strongest available defence against overfitting in a small research operation is to write
down the hypothesis, the entry/exit rules, the parameters, and the success criteria **before**
running the backtest.

Checks:
- Does a pre-registration document exist for this strategy? Is it timestamped before the
  results?
- Do the tested parameters match the pre-registered ones, or did they drift during testing?
- Were the success criteria changed after seeing results? Any post-hoc adjustment of the gate
  moves the strategy back to the "exploratory" pile.

If no pre-registration exists, say so plainly and treat all results as exploratory — meaning
they generate hypotheses for future out-of-sample testing, and cannot support a promotion
decision on their own.

## Checklist

- [ ] Effective independent bet count is stated and justified, not just trade count.
- [ ] Total number of strategies/variants/parameter sets tested is known and honest.
- [ ] Multiple-testing correction (e.g. Benjamini-Hochberg) applied across the full family.
- [ ] Best observed t-statistic exceeds the max-of-N noise threshold for that family size.
- [ ] Deflated Sharpe computed with honest trial count and actual skew/kurtosis.
- [ ] Cross-validation is purged and embargoed, with embargo ≥ max holding period.
- [ ] No lookahead: fills lag signals, indicators computed causally, normalisation fit on
      train folds only.
- [ ] Intrabar stop/target ambiguity resolved conservatively.
- [ ] Required sample size for the claimed edge computed and compared against actual.
- [ ] Hypothesis and success criteria pre-registered before backtesting.
