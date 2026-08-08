# Tier 3 — Execution Realism

This tier answers: **will this survive contact with real fills, real spreads, and real risk
limits?** A strategy can be statistically sound and economically grounded and still lose money
because its exits never trigger or its risk controls were sized for a different position.

## Contents
- [Exit geometry](#exit-geometry)
- [Denomination of thresholds](#denomination-of-thresholds)
- [Risk controls and position scaling](#risk-controls-and-position-scaling)
- [Fill modelling](#fill-modelling)
- [Order lifecycle](#order-lifecycle)
- [Paper-to-live divergence](#paper-to-live-divergence)
- [Checklist](#checklist)

---

## Exit geometry

**The most under-diagnosed failure in retail systems: targets that are never reached.**

The diagnostic is MFE distribution. For every trade, compute maximum favourable excursion as a
fraction of the target:

```
mfe_ratio = (max_price_during_trade - entry_price) / (target_price - entry_price)
```

Then look at the distribution, not the mean:

- Median near 1.0 — target is well calibrated.
- Median around 0.4-0.6 — target is somewhat ambitious; consider partial exits.
- **Median under 0.1** — the target is unreachable. The strategy is not "a trend strategy
  with a stop"; it is *only* its stop-loss, and every backtest assumption about the
  risk-reward ratio is fiction.

Run the same analysis for MAE (maximum adverse excursion) against the stop, to see whether
stops are being hit on noise before the thesis has a chance to play out.

Common causes of unreachable targets:
- Targets set as a multiple of ATR computed on the **underlying**, then applied to **option
  premium**, which moves differently (delta < 1, and theta works against you).
- ATR computed on a different timeframe than the holding period.
- A fixed multiple that was reasonable in one volatility regime and never revisited.

## Denomination of thresholds

Percentage-denominated exit thresholds frequently fail to arm at all.

The mechanism: a trailing stop that requires a 0.25% favourable move to activate will never
activate if the typical intraday range of the instrument is smaller than that, or if the move
happens faster than the polling interval. The protection exists in code, passes tests, and
never fires in production.

Checks:
- For every threshold expressed as a percentage, compute what it means in absolute terms for
  the actual instrument at typical prices. A 0.25% trigger on a ₹150 premium is ₹0.375 — below
  tick granularity for practical purposes.
- Compare against the empirical distribution of intraday moves for that instrument. If the
  threshold sits above the 90th percentile of favourable excursion, it is decorative.
- **Prefer absolute (rupee) denomination for option premium exits.** Premium prices vary by an
  order of magnitude across strikes and expiries; a single percentage applies wildly different
  effective thresholds across them.
- Verify by instrumenting: log every time a protection *evaluates* and whether it armed. A
  protection that has never armed in production is a finding, not a feature.

## Risk controls and position scaling

**Risk limits do not scale automatically with position size.** This produces silent, severe
failures.

The canonical case: a portfolio-level daily loss kill-switch calibrated so that it triggers
after several losing trades at one lot. Increase to five lots and the same rupee threshold is
breached by a *single* trade — the portfolio protection has silently become a per-trade stop,
and the intended diversification across trades never happens.

Checks:
- List every risk limit with its units. For each, compute how many trades at the *current*
  position size it takes to breach.
- Was each limit re-derived after the most recent size change? Ask for the derivation.
- Are limits expressed relative to capital, or as fixed rupee amounts? Fixed amounts drift in
  meaning as the account changes.
- Is there a per-trade stop *and* a portfolio stop, with the portfolio stop meaningfully
  above N × the per-trade stop? If not, one of them is redundant.
- What happens when the kill-switch fires — are open positions closed, or only new entries
  blocked? Both are defensible; the code must clearly implement one and the operator must know
  which.

## Fill modelling

Backtests and paper trading almost always assume better fills than reality.

Checks:
- What price does the paper engine fill at — last traded price, mid, or the far touch? LTP is
  optimistic; it's a price someone else got.
- Is the bid-ask spread applied? For index options the spread widens dramatically for
  far-from-money strikes, on expiry day, and in the first and last minutes of the session.
- Is there a liquidity filter? A backtest that trades strikes with negligible open interest is
  trading instruments that could not have absorbed the order.
- Is partial fill modelled? For larger sizes, assuming complete fill at one price is
  optimistic.
- Is market impact considered at the intended size relative to typical strike volume?
- Are order rejections modelled — margin shortfalls, freeze quantity breaches, circuit limits?

## Order lifecycle

Checks on the live execution path:

- **Idempotency:** if the order-placement call times out, does a retry risk placing a
  duplicate? Is there a client order ID to deduplicate?
- **Reconciliation:** does the system verify that its internal position matches the broker's
  position, on a schedule? Divergence between believed and actual position is how large
  unintended exposures accumulate.
- **Exit priority:** when data is rate-limited or the system is under load, does it prioritise
  fetching data for symbols with open positions over whatever is being displayed? An exit that
  is late because the system was updating a chart is a real and expensive bug.
- **Restart behaviour:** if the process restarts with positions open, does it recover them
  from the broker, or start believing it is flat?
- **Stale data guards:** is there a maximum age on quotes used for exit decisions? Acting on a
  stale price during a fast move is worse than not acting.
- **Clock:** is the system timezone explicitly set to the exchange timezone? Scheduled logic
  keyed to local time breaks silently when the host timezone differs.

## Paper-to-live divergence

Before any go-live decision, require a documented comparison:

- Run paper and live simultaneously on small size for a period, and compare fill prices trade
  by trade. The distribution of (live fill − paper fill) is the slippage estimate, and it is
  usually worse than assumed.
- Compare the *number* of trades: if live takes fewer trades than paper, some signals are
  being lost to rejections, margin, or latency, and the live strategy is not the tested one.
- Check that live and paper share the same code path for signal generation. Divergent paths
  mean paper results do not transfer.

## Checklist

- [ ] MFE distribution computed; median MFE/target ratio is reasonable.
- [ ] MAE distribution computed; stops are not being hit on routine noise.
- [ ] Every percentage threshold converted to absolute terms and compared against the
      instrument's actual move distribution.
- [ ] Every protection has been observed to arm at least once in production logs.
- [ ] Every risk limit re-derived at current position size, with the derivation shown.
- [ ] Portfolio stop is meaningfully above N × per-trade stop.
- [ ] Fill model uses conservative prices, applies spread, and filters illiquid strikes.
- [ ] Order placement is idempotent; position reconciled against broker on a schedule.
- [ ] Data fetch prioritises symbols with open positions.
- [ ] Position recovery on restart is implemented and tested.
- [ ] Timezone set explicitly to exchange timezone.
- [ ] Paper-vs-live slippage measured on real trades before size increase.
