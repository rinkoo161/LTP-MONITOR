# B — Regime & timeframe integrity

## B1. Repainting on the higher timeframe

A 15m regime computed from a 15m bar that has not closed will change before
that bar ends. A backtest reading completed 15m bars therefore saw a stable
value the live system never had.

Check:
- Does the higher-timeframe series exclude the forming bar?
- Is the regime recomputed on every tick, or only on HTF bar close? If every
  tick, does the consumer treat it as provisional?
- Live tell: a regime label that flips several times within one HTF bar.

## B2. Stale regime used as if live

A stale regime is more dangerous than a missing one, because callers degrade
gracefully on missing and not at all on stale.

Check:
- Is there an explicit staleness marker, and does EVERY consumer honour it?
  Count the consumers. One missed call site is a real-money bug.
- Is last-session regime published on the same key as live regime? It should
  not be — display paths may want it, trade paths must not have it.
- What is published during warmup, before enough bars exist? "Unknown" that
  callers treat as "not blocking" silently disables the gate.

## B3. The gate that cannot fail

A gate whose failure branch is `check(True, "not blocking")` does nothing.
Grep for gates that pass themselves when data is absent, then ask how often
data is absent in practice — if regime is missing 30% of the time, the
regime gate is off 30% of the time and the backtest never modelled that.

Distinguish honestly in the report between "gate rejected" and "gate skipped".
A log line that reads as a tick for a check that did not run is itself a
finding.

## B4. Label leakage in confluence

Multi-timeframe confluence combines several series. Leakage creeps in when:
- one timeframe is resampled from a series that already includes the future
  relative to the decision bar;
- an "aligned" flag is computed after the fact over the whole series rather
  than causally, bar by bar;
- indicators on different timeframes are indexed by position rather than by
  timestamp, so a 15m bar is paired with the wrong 5m bar near session edges.

Always join by **timestamp**, never by index offset.

## B5. Session boundaries and timezones

- Are candle timestamps IST, UTC, or naive? A mixed convention shifts every
  higher-timeframe bucket.
- Is the first bar of the session handled (no previous close, no prior VWAP)?
- Are holidays and half-days excluded, or do they produce a bar with a
  nonsense range that poisons ATR for the next `period` bars?
- Expiry day behaves differently from every other day. If the regime model
  was fitted across all days, expiry is an unmodelled regime of its own.

## B6. Does the regime label mean anything?

The empirical test, not the code test: bucket historical returns by the
regime label the system assigned at the time, and compare the distributions.
If "trending" and "rangebound" have indistinguishable forward return
distributions, the label carries no information and every gate built on it is
noise — regardless of how correct the code is.

State the sample size and the date range. Below a stated n, the honest answer
is "insufficient sample", not a verdict.
