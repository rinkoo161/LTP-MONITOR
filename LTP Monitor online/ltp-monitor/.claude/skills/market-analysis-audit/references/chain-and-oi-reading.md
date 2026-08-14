# C — Option chain & OI reading

## C1. OI change needs a reference, and the reference must be the right one

"OI change" is meaningless without stating change *since when*. Common
errors:

- comparing against the previous **snapshot** (60s ago) and calling it the
  day's buildup;
- comparing against yesterday's close without accounting for the roll, so on
  expiry day every strike shows a fabricated collapse;
- using the exchange's `oi_chg` field for one symbol and a locally computed
  delta for another, then aggregating the two.

Verify one definition, used everywhere. Where a quadrant classifier exists
(long buildup / short buildup / short covering / long unwinding), it must be
the single source — a strategy that re-derives quadrants inline will drift
from the one the dashboard shows.

## C2. Price and OI must be measured over the same interval

The four quadrants are a joint statement about ΔPrice and ΔOI. If ΔOI is
measured since the open and ΔPrice since the last tick, the classification is
not wrong occasionally — it is meaningless.

## C3. Walls, and what they actually predict

A "wall" of open interest is a position, not a barrier. Auditing the *claim*
matters as much as the code:

- Is the wall computed on OI, or on OI change? Standing OI is largely
  historical; today's change is today's positioning.
- Is it normalised by the strike's typical OI, or is it just "the biggest
  number", which will always be the round strike?
- Does the strategy require the wall to persist for N snapshots, or can one
  bad print create one?

The empirical test: how often does spot actually stall at the identified
wall, versus at a random strike of similar distance? If the two rates match,
the wall carries no information.

## C4. IV fields

- Is IV per-leg, per-strike, or an index-level ATM IV? Mixing them produces
  a "vol" series that is partly smile and partly level.
- Is IV in percent or decimal? A 10× error here silently rescales every
  greek and every vol-based filter.
- Is IV present at all for illiquid strikes, or defaulted to zero? Zero IV
  through a Black-Scholes call gives a zero-delta, zero-vega option, which
  will size and hedge nonsensically.

## C5. Strike selection and liquidity

- ATM must be computed from the *current* spot, not the analysis pack's spot
  from up to a minute ago, when strike spacing is 50 and spot moves 30 in
  that minute.
- Is there a liquidity filter — minimum volume/OI, maximum spread? A signal
  on a strike with a ₹15 spread has already lost more to friction than its
  designed edge.
- Far-OTM options quote in ticks. A stop expressed as a percentage of a ₹2
  premium is a stop of ₹0.60, i.e. inside one tick.

## C6. Expiry-day and settlement effects

- Time decay is not linear on expiry day; a stop calibrated on ordinary days
  is far too tight into the last hours.
- ITM options at expiry attract STT on the full settlement value, which
  dwarfs ordinary brokerage. If the cost model treats expiry-day exits like
  any other exit, exit costs are understated exactly when they are largest.
- Weekly versus monthly expiry, and the difference in lot size revisions
  across symbols, must be read from the instrument master rather than
  hardcoded.

## C7. Freshness of the chain itself

Every number above is only as good as the snapshot. Confirm:
- the chain's own timestamp is checked, not just the time it was received;
- a partially-populated chain (some legs missing ltp/oi) is detected rather
  than averaged over as if the missing values were zero;
- out-of-session prints are excluded before any aggregation.
