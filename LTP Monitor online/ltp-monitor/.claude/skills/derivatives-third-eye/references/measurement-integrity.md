# Tier 0 — Measurement Integrity

This tier answers one question: **can the system compute its own P&L correctly?** If not,
every metric downstream is a measurement of the error rather than the edge, and the review
stops here.

## Contents
- [The proxy trap](#the-proxy-trap)
- [Cost accounting](#cost-accounting)
- [Contract and instrument parity](#contract-and-instrument-parity)
- [Config drift](#config-drift)
- [Data retention as an audit dependency](#data-retention-as-an-audit-dependency)
- [The promotion gate](#the-promotion-gate)
- [Checklist](#checklist)

---

## The proxy trap

Many systems don't record real option fills. Instead they record index points moved and
convert with an assumed delta, e.g. `pnl = points_moved * 0.5 * lot_size`. This is a **proxy**,
and its error is usually far larger than the edge being measured.

Why the error dominates: a fixed delta assumption ignores that delta changes across the move,
that IV shifts, and that theta is bleeding throughout. For short-dated index options the
realised P&L can differ from a fixed-delta proxy by a large multiple, and the direction of
the error correlates with the direction of the trade — which means it does not average out
across a sample. It biases, not just blurs.

Checks:
- Find the P&L computation. Is it derived from `entry_price` and `exit_price` of the actual
  instrument, or reconstructed from the underlying?
- If a proxy exists, is there **any** sample of trades where both the proxy and the real fill
  price were recorded? Compute the ratio distribution. If not, the proxy has never been
  validated and its error is unknown, not small.
- Does the same proxy feed both the live dashboard and the backtest? If so, a strategy can
  be optimised against the proxy's error term and look excellent.

The reviewer's line: a P&L number whose error bar has never been estimated is not a
measurement. Treat it as absent.

## Cost accounting

Round-trip cost is the most commonly understated quantity in retail trading systems, and it
is understated by multiples, not percentages.

For Indian index derivatives a full round trip includes: brokerage, exchange transaction
charges, SEBI turnover fee, GST on (brokerage + transaction charges), STT, and stamp duty —
plus, critically, **slippage and half the bid-ask spread on each leg**, which most systems
omit entirely.

Checks:
- Locate the fee constant. Compute one round trip by hand from it. Compare against an actual
  broker contract note for a real trade. A 10x discrepancy is common and is invisible in
  aggregate P&L when the proxy error is also large.
- Is STT applied on the correct leg and correct base? For options, STT is charged on the
  sell-side premium; for **ITM options allowed to expire**, it is charged on the settlement
  value, which can exceed the entire premium collected. See
  `india-market-mechanics.md`.
- Is the spread modelled? For far-from-money weekly strikes, the spread alone can exceed the
  strategy's gross edge.
- **Compare gross edge per trade against round-trip cost per trade.** This single comparison
  invalidates more strategies than any other check. If gross edge is smaller than cost, the
  strategy is not marginal — it is structurally impossible, and no amount of parameter tuning
  fixes it. A strategy whose gross edge is a few rupees against a cost of several rupees is
  competing against infrastructure that will always win.

## Contract and instrument parity

Contract specifications change, and a stale constant silently rescales every P&L number.

Checks:
- Are lot sizes hardcoded in config, or read from the broker's scrip master at startup?
  Exchanges revise index option lot sizes; a config that still holds an old value produces
  P&L wrong by exactly that ratio.
- Is there a startup reconciliation that compares config lot sizes against the live scrip
  master and fails loudly on mismatch? If not, this is a MAJOR finding on its own.
- Are tick sizes and strike intervals correct per symbol? These differ across NIFTY,
  BANKNIFTY, FINNIFTY and SENSEX.
- For spreads and multi-leg positions, is P&L netted per leg with each leg's own costs, or
  computed on the net premium with a single cost applied?

## Config drift

The config in source control is not the config that is running. This gap is invisible to
code review by construction.

Checks:
- Get the live config file. Diff it against the defaults in source. Report every difference.
- Pay special attention to: position size / lots per trade, risk caps, kill-switch
  thresholds, and anything that scales exposure. A position-size multiplier that has drifted
  upward invalidates all prior measurement *and* silently rescales every risk control that
  was calibrated at the original size.
- Is there a startup assertion for parity between live config and defaults, or at minimum a
  logged diff? If not, recommend one — this is a cheap, high-value control.
- Are all config keys registered in every place they need to be? A key present in defaults
  but absent from the settings schema will be silently dropped on save, so a user changing it
  in the UI sees the change accepted and discarded.

**Critical interaction:** risk controls calibrated at one position size do not scale
automatically. A portfolio-level kill-switch sized for one lot becomes a single-trade stop
at five lots. Always check whether risk limits were re-derived after any size change.

## Data retention as an audit dependency

A retention policy is a measurement dependency, not a housekeeping detail.

Checks:
- What raw data is retained, and for how long? Option chain snapshots, tick data, real
  instrument OHLCV, order/fill records.
- Ask directly: **if the P&L model turned out to be wrong, could it be recomputed from
  retained data?** If the answer is no, the system cannot correct its own errors — it can
  only discover them and lose the evidence.
- Short retention (days) on chain snapshots combined with a P&L proxy is a critical
  combination: the proxy can never be validated because the ground truth is deleted before
  anyone thinks to check.
- Storage is cheap relative to this risk. Full chain snapshots for a handful of index symbols
  run on the order of 100 MB/day; a year is tens of gigabytes.

## The promotion gate

Examine what the system uses to decide a strategy is working. A gate of the form
`net_pnl > 0` over a small sample does not select for edge — it selects for whichever
strategy's *error terms* happened to be favourable, which is a random draw that will not
persist.

Checks:
- What is the gate condition, in code?
- What sample size does it require? (See `statistical-validity.md` — a gate that can be
  passed on tens of trades cannot detect realistic per-trade edge.)
- Does the gate use the same P&L computation whose validity is in question? If so, the gate
  and the measurement are not independent, and the gate cannot detect measurement failure.
- Does anything *outside* the loop check the loop's premise? A system where the promotion
  gate, the dashboard, and the performance report all read from the same possibly-wrong
  number will show green while failing. There must be at least one check that closes against
  ground truth: broker contract notes, reconciled account balance, or an independent
  recomputation from fills.

## Checklist

Tier 0 passes only if all of these are true. Any `no` is a BLOCK.

- [ ] P&L is computed from actual instrument entry/exit prices, or the proxy's error
      distribution has been measured against real fills.
- [ ] Round-trip cost has been reconciled against at least one real broker contract note.
- [ ] Slippage and spread are modelled, not assumed zero.
- [ ] Gross edge per trade exceeds round-trip cost per trade by a stated margin.
- [ ] Lot sizes and contract specs are reconciled against the broker scrip master at startup.
- [ ] The live config has been diffed against defaults and differences are intentional.
- [ ] Risk limits have been re-derived at the current position size.
- [ ] Raw data retention is long enough to recompute P&L if the model changes.
- [ ] At least one check closes against ground truth outside the system's own reporting.
