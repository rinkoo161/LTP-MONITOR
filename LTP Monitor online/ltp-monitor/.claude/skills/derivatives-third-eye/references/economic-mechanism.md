# Tier 2 — Economic Mechanism

This tier answers: **is there a reason this should work, stated before the backtest?**

A strategy without a mechanism is a pattern found in noise. Patterns found in noise backtest
beautifully and fail forward, and no amount of statistical rigour distinguishes them from real
edge as reliably as asking who is on the other side and why they are willing to lose.

## The core question

**Who takes the other side of this trade, and why do they accept a negative expected return?**

If there is no answer, the strategy has no edge — it has a coincidence. Legitimate answers
fall into a small number of categories:

- **Risk transfer.** The counterparty is paying to offload risk they don't want. Someone
  hedging a portfolio buys puts above fair value; the seller earns a premium for bearing
  tail risk. This is real and durable, and it comes with genuine tail exposure — if the
  backtest shows the premium without the tail, the tail is in the data you don't have.
- **Constraint.** The counterparty is forced to trade by a mandate, margin call, index
  rebalance, or expiry mechanic, regardless of price. Durable while the constraint exists.
- **Information or processing asymmetry.** You see or compute something they don't. In liquid
  index derivatives this is the least plausible category for a retail system, and it decays
  fastest.
- **Liquidity provision.** You're paid the spread for providing immediacy. Requires being
  faster than the competition, which is a capital and infrastructure question, not a strategy
  question.

If the proposed mechanism is "momentum persists" or "the market is inefficient," that is not a
mechanism. Push for the specific participant and the specific reason.

## Structural feasibility before anything else

Before evaluating whether an edge is real, check whether it *could* be captured.

**Gross edge must exceed round-trip cost by a meaningful margin.** If a strategy's gross edge
per trade is a few rupees against costs of several rupees, no refinement rescues it. The
competition for edges that small is high-frequency infrastructure with colocation, exchange
rebates, and cost structures a retail account cannot match. Recognising this early prevents
months of work on a structurally impossible idea.

Ask directly: at what cost level does this strategy break even, and how far is that from
actual cost? If the margin is under about 2x, treat the strategy as infeasible regardless of
backtest results.

## Decay and crowding

- Has the mechanism been publicly documented? Widely known edges in liquid index derivatives
  are usually arbitraged to the cost floor.
- Does the edge depend on a market structure that is changing? Expiry-day schedules, lot size
  revisions, position limits, and settlement rules all change and can eliminate a strategy
  overnight.
- Does the backtest period contain a regime that no longer exists? A strategy validated
  entirely during one volatility regime is a bet that the regime returns.

## Common pseudo-mechanisms to challenge

| Claim | The problem |
|---|---|
| "OI buildup shows institutional positioning" | OI change is net of all participants and doesn't reveal direction or intent. A rise in OI with a price rise is equally consistent with long buildup and short writing. Ask what independent evidence distinguishes them. |
| "This indicator crossover predicts direction" | Indicator crossovers are deterministic functions of past price. If they predicted returns, the prediction would be arbitraged. Requires an unusually specific mechanism to be credible. |
| "PCR extremes are contrarian signals" | PCR is affected by hedging flow that has no directional view. The threshold that "works" is usually fitted. |
| "Max pain pulls price to the strike" | Requires market makers to have both the ability and incentive to move an index. Implausible at index scale. |
| "The AI model says the bias is bullish" | An LLM's directional opinion on price is not evidence. Ask what the model saw that a rule couldn't, and whether that has been tested separately. |
| "It works on expiry day" | Expiry days have distinct mechanics, and there are few of them. A strategy with 50 expiry-day observations has 50 observations. |

None of these are automatically wrong — but each requires the author to supply the mechanism,
not just the correlation.

## Mechanism-to-implementation consistency

A frequent and subtle failure: the stated mechanism and the implemented rule diverge.

- If the mechanism is "IV is systematically overpriced before events," the implementation
  should be short vega around events — not a directional option buy that happens to correlate.
- If the mechanism is "range-bound days mean-revert," the implementation must have a
  regime filter that actually identifies range-bound days *in advance*, not one fitted to
  days that turned out to be range-bound.
- If the mechanism is about institutional flow, the signal must be available at decision time,
  not published after the close.

Check that the exit logic matches the mechanism too. A mean-reversion thesis with a trailing
stop that rides trends is internally inconsistent — one of the two is wrong, and the backtest
result is a blend of both.

## Checklist

- [ ] A specific counterparty and their reason for accepting negative EV is named.
- [ ] The mechanism was stated before the backtest, not derived from it.
- [ ] Gross edge exceeds round-trip cost by a margin of at least ~2x.
- [ ] The mechanism doesn't rely on information unavailable at decision time.
- [ ] The implementation expresses the stated mechanism, including on the exit side.
- [ ] Regime dependence is identified, and the backtest spans more than one regime.
- [ ] Decay risk from crowding or market-structure change is assessed.
