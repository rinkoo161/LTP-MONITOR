# Indian Index Derivatives — Market Mechanics

Failure modes specific to NSE/BSE index derivatives that generic trading-system review misses.

**Standing instruction: do not trust any hardcoded contract specification, including the ones
implied below.** Lot sizes, expiry schedules, position limits, and STT rates in this market
have all been revised repeatedly and on short notice. Every specification must be read from
the broker's scrip master or the exchange circular at runtime and reconciled at startup. When
reviewing, verify current values from the exchange rather than assuming — a spec that was
correct when the code was written is a leading cause of silent P&L error.

## Contents
- [Lot size revisions](#lot-size-revisions)
- [STT on ITM expiry](#stt-on-itm-expiry)
- [Expiry schedule changes](#expiry-schedule-changes)
- [Expiry-day behaviour](#expiry-day-behaviour)
- [Freeze quantity and position limits](#freeze-quantity-and-position-limits)
- [Liquidity and spread structure](#liquidity-and-spread-structure)
- [Data and calendar](#data-and-calendar)
- [Checklist](#checklist)

---

## Lot size revisions

Index derivative lot sizes are revised by the exchanges, and revisions have been substantial —
large enough that a stale constant misstates every P&L figure by that ratio.

Review actions:
- Find every place a lot size appears. Config, defaults, strategy code, backtest fixtures,
  and the sizing module frequently disagree with each other.
- Confirm the system reads lot size from the broker scrip master and **fails loudly** on
  mismatch with config at startup.
- Check that historical backtests use the lot size that was in force *during* that historical
  period, not today's. A backtest spanning a revision that applies one constant throughout is
  wrong on one side of the change.
- Different indices have different lot sizes, and they are revised independently. Verify per
  symbol, never globally.

## STT on ITM expiry

**The single most expensive trap in Indian options.**

STT on option *trades* is charged on the premium of the sell leg. But an option allowed to
expire in-the-money is treated as exercised, and STT is charged on the **settlement value** —
the notional — not on the premium.

The consequence: a long option position worth a small premium, held to expiry ITM, can incur
an STT charge that dwarfs the entire position value and can exceed the profit many times over.
Squaring off before expiry avoids this entirely.

Review actions:
- Does the system have a mandatory square-off before expiry close for any long option
  position that could be ITM? This should be a hard rule, not a strategy preference.
- Is the square-off time early enough to actually fill given expiry-day liquidity, rather than
  in the final minute?
- Does the cost model include exercise STT for any path where a position could reach expiry?
  Most cost models omit it entirely because most backtests never let a position expire.
- For short options: assignment on ITM shorts also settles at notional. Check the margin and
  cost treatment.

If a backtest permits positions to expire ITM without modelling exercise STT, its P&L is
overstated on exactly the trades that looked most profitable.

## Expiry schedule changes

Weekly and monthly expiry days for index derivatives have been changed by both exchanges,
including which weekday each index expires on and how many weekly expiries exist at a time.

Review actions:
- Is the expiry calendar hardcoded to a weekday, or derived from the instrument master?
  Hardcoded weekday logic breaks silently on schedule changes — the system computes days-to-
  expiry wrongly, which corrupts every theta, IV, and greeks calculation.
- Does the backtest use the expiry schedule that was actually in force historically?
- Is "days to expiry" computed against exchange holidays, or calendar days? Theta over a long
  weekend differs from theta over a weekday.

## Expiry-day behaviour

Expiry-day option behaviour is qualitatively different and warrants separate treatment:

- Gamma is extreme near the money; delta assumptions that hold on other days do not hold here.
  Any fixed-delta P&L proxy is worst on expiry day.
- Theta decay is non-linear and concentrated.
- Premiums for out-of-the-money strikes collapse toward zero, so percentage-based stops and
  targets behave erratically at low absolute premium.
- There are few expiry days. A strategy that only trades expiry day accumulates observations
  slowly — check the effective sample size, which is likely far smaller than the trade count
  suggests.

Review action: check whether the backtest and the live system treat expiry day distinctly, or
apply the same parameters throughout. If the same parameters are used, the expiry-day trades
are likely contributing disproportionate noise or disproportionate apparent edge.

## Freeze quantity and position limits

Exchanges impose a maximum order quantity (freeze quantity) per order, and position limits per
client. Orders exceeding freeze quantity are rejected outright.

Review actions:
- Does the order module split large orders below the freeze limit, or will it simply get
  rejected at size?
- Is rejection handled, or does the system believe it has a position it does not have? This is
  a position-reconciliation failure with real exposure consequences.
- Are freeze quantities read from the instrument master (they vary by symbol and are revised),
  or hardcoded?

## Liquidity and spread structure

- Liquidity concentrates heavily in near-the-money strikes of the nearest expiry. A backtest
  that selects strikes by delta or distance may pick strikes with negligible volume.
- Spreads widen sharply: in the opening minutes, near the close, on expiry day, for
  far-from-money strikes, and for the less-traded indices.
- Check whether the strike selection logic includes a liquidity floor (open interest or volume
  threshold) and whether that floor was applied in the backtest as well as live. Applying it
  only in live means the backtest traded instruments the live system will refuse.

## Data and calendar

- **Timezone:** the exchange operates in IST. Any host with a different system timezone will
  misfire every scheduled task. Verify the timezone is set explicitly in code, not inherited
  from the host.
- **Holiday calendar:** trading holidays are announced annually and revised. A hardcoded
  holiday list goes stale. Special sessions (muhurat trading) are additional edge cases.
- **Session timings:** verify against current exchange circulars, including any pre-open
  session handling.
- **Index vs futures:** the index level and the futures price differ by basis, which varies
  with time to expiry and carrying cost. A system that uses index levels where futures prices
  are appropriate (or vice versa) introduces a systematic and time-varying error. Check which
  is used for signal generation and which for P&L.
- **Corporate actions** affect constituent stocks and can affect index computation; adjusted
  historical data may not match what was observable in real time.

## Checklist

- [ ] All contract specs read from scrip master at runtime, reconciled against config at
      startup, failing loudly on mismatch.
- [ ] Historical backtests use period-appropriate lot sizes and expiry schedules.
- [ ] Mandatory pre-expiry square-off exists for positions that could settle ITM.
- [ ] Exercise STT modelled in the cost function for any expiry path.
- [ ] Days-to-expiry computed from the instrument master and exchange holiday calendar.
- [ ] Expiry-day trades analysed separately; effective sample size stated.
- [ ] Freeze quantity respected with order splitting; rejections handled and reconciled.
- [ ] Strike selection includes a liquidity floor, applied identically in backtest and live.
- [ ] Timezone set explicitly to IST in code.
- [ ] Holiday calendar sourced, not hardcoded.
- [ ] Index vs futures price usage is deliberate and consistent between signal and P&L.
