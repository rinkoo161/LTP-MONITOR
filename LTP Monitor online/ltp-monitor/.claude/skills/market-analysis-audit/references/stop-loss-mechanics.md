# D — Stop-loss & exit mechanics

This is where money is lost fastest. Audit it first when time is short.

The operator's mental model is "my loss is capped at the stop". Every item
below is a way that belief becomes false while the code looks correct.

## D1. The stop sits inside normal noise

A stop closer to entry than one ATR of the *instrument being stopped* is not
a risk control, it is a random exit. For an option buy the relevant
volatility is the **option premium's** volatility, not the underlying's — a
30-point NIFTY move is noise; a 30-point move in a ₹120 premium is 25%.

Check:
- Is the stop derived from the option's own ATR/spread, or from the index's?
- What is the median bid-ask spread of the option being traded? A stop
  inside the spread is hit on the spread alone, with no adverse move at all.
- Compute, over the archived losing trades: how many were stopped out and
  then the position would have recovered to target within the same session?
  A high rate is direct evidence the stop is inside noise.

## D2. Breakeven locks and trailing that fire on the entry bar

The classic and expensive one:

1. entry fills;
2. the first monitor cycle evaluates target1 against a *stale or wrong*
   entry price and decides target1 is already hit;
3. the breakeven lock pins the stop to entry;
4. the next downtick closes the trade for ~zero minus costs.

Check the ordering explicitly:
- Can `t1_hit` be true on the first evaluation after entry? If yes, why?
  Usually because the fill price and the reference price come from different
  sources (analysis pack vs live quote) or different legs.
- Does the trail arm immediately, or only after some minimum favourable
  excursion / minimum time?
- Is the breakeven level entry, or entry + costs? "Breakeven" that ignores
  round-trip friction is a guaranteed small loss.

The signature in the journal: a cluster of trades with P&L ≈ −(costs), and
`mfe` (max favourable excursion) near zero.

## D3. Intrabar ordering — stop vs target in the same bar

If a bar's range spans both stop and target, which fired? The honest default
is **stop first** — it is the conservative assumption and usually correct for
a bar that gapped or spiked. A backtest that resolves ties in favour of the
target manufactures a win rate that live trading cannot reproduce.

Check the replay/backtest exit function directly. This single line can be
worth several percentage points of apparent win rate.

## D4. Gaps — the stop is a trigger, not a fill

A stop-loss does not cap loss at the stop price. It becomes an order when
touched, and fills wherever liquidity is. On an Indian index option gapping
through the level at 09:15, the fill can be far beyond the stop.

Check:
- Does risk sizing assume loss = (entry − stop) × qty exactly? That is the
  *modal* loss, not the maximum. Any per-trade cap built on it understates
  tail risk.
- Is there any handling for "stop level was passed while we were not
  watching" (overnight, a data outage, a host sleeping)? On resume, does the
  code exit at the current price, or does it re-arm the original stop as if
  nothing happened?

## D5. Stops that depend on data that can go stale

- If the exit path reads a quote with a staleness ceiling, what happens when
  the quote is stale? Not exiting is a decision; exiting on a stale price is
  a different decision. Both must be deliberate and logged.
- If the monitor thread dies or its cadence slips, does anything notice? A
  stop that is only evaluated when a loop runs is only as reliable as the
  loop.
- Square-off / EOD forced closure must not use a price captured before the
  outage. Re-fetch, or refuse and alert.

## D6. Invalidation vs stop-loss

Many systems carry both a price stop and a *thesis* invalidation (e.g. spot
crossing back through a level). Confirm:
- Both are evaluated, and one cannot silently mask the other.
- The invalidation uses the underlying at the correct timeframe, and does not
  repaint (see reference B).
- Which one fired is recorded per trade. If exit reasons are not attributable,
  you cannot tell a stop problem from a thesis problem, and every subsequent
  analysis is guesswork.

## D7. Multi-leg exits

For spreads and composites:
- Is the stop evaluated on the **net** position or per leg? A per-leg stop on
  a defined-risk spread can close the hedge and leave a naked short.
- On exit, are both legs closed, and what happens if one fill fails?
- Is the "max loss" used for sizing the true spread width minus credit,
  including both legs' costs?

## D8. The exit actually taken vs the exit designed

Reconcile, over the archived trades: the distribution of `exit_reason`. If
most exits are a forced EOD square-off or a portfolio kill-switch rather than
the strategy's own stop or target, the designed exit logic is not what is
running the book — something upstream is. That reframes every other finding.

## How to demonstrate a finding here

Take real losing trades from the journal and replay them through the exit
code, printing per cycle: timestamp, price source, computed stop, computed
target, and the branch taken. The goal is a table showing the stop sitting
somewhere other than where the operator believes, on a trade that actually
lost money.
