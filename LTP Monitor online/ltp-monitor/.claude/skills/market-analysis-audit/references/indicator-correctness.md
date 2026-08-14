# A — Indicator correctness

The failure modes below are ordered by how often they cause real losses.

## A1. Duplicate implementations that disagree

The single most productive check. If `ema()` exists in four files, at least
two differ, and the difference matters when one gates entry and another
sizes the stop.

Detect:

```
grep -rn "def ema\|def atr\|def rsi\|def adx\|def supertrend\|def bollinger" --include="*.py" .
```

Then for each duplicated name, run all copies on the same candle series and
report the first index where they diverge and by how much. Do not compare
source text — compare output. Two functions can look different and agree, or
look identical and differ by a seeding line.

Ask specifically: **does the copy used to decide direction differ from the
copy used to size the stop?** If yes, the stop is calibrated to a different
volatility than the signal, and that is a CRITICAL finding regardless of
which copy is "right".

## A2. Off-by-one and the closed-bar rule

The bar you are standing in has not closed. Using it means the live system
acts on information it will not have until the bar ends, and the backtest is
optimistic in a way that never reproduces.

Check:
- Does the signal read `candles[-1]` (forming) or `candles[-2]` (closed)?
- Is the same convention used by the backtest and the live agent? A backtest
  that uses `[-2]` and a live path that uses `[-1]` are different strategies.
- Does the fill get modelled at the same bar's close that generated the
  signal? That is lookahead unless the signal is computed on the prior bar.

The tell in live logs: signals that fire and then immediately reverse within
the same minute.

## A3. Warmup and seeding

- **EMA seeding**: `ema[0] = values[0]` versus `ema[0] = SMA(values[:period])`
  converge, but slowly — for period 200 they can differ for hundreds of bars.
  If the series is short (a fresh session, a thin symbol), they may never
  converge inside the data you have.
- **Insufficient bars**: does the function return a value when
  `len(candles) < period`? Returning something plausible from 5 bars of a
  14-period ATR is worse than returning None, because callers cannot tell.
- **ATR seeding**: Wilder's smoothing versus a simple mean of true ranges
  give materially different values for the first ~2×period bars. Wilder's is
  the standard; a simple mean runs "hot" early, which widens stops at exactly
  the time the series is least reliable.

Check what the caller does when the indicator legitimately cannot be
computed. Silent zero is the dangerous answer: a zero ATR collapses every
ATR-derived stop onto the entry price.

## A4. True range and gaps

True range must be `max(h-l, |h-prev_close|, |l-prev_close|)`. A naive
`h - l` ignores gaps entirely and understates volatility on exactly the days
that matter — which then places stops too tight the morning after a gap.

Indian index options gap frequently at 09:15. Verify the first bar of the
session uses the *previous session's* close, not the current session's open,
and not `None`.

## A5. Resampling and bar construction

If 5m/15m bars are built from 1m data:
- Does a partial final bucket get emitted as if complete?
- Are buckets aligned to the session (09:15) or to the wall clock (09:00)?
  A 15m bar starting at 09:00 contains 15 minutes of nothing and then the
  open — its range and its ATR are both wrong.
- Are out-of-session bars (pre-open, post-close, keepalive ticks) filtered
  BEFORE aggregation? One contaminating print sets a false high for the bar
  and for every indicator derived from it.

## A6. Divide-by-zero and degenerate inputs

- ADX/DI when true range sums to zero across the period (a frozen or halted
  series) — does it raise, return zero, or return the previous value?
- RSI when there are no losses in the window: the standard result is 100;
  a naive `avg_gain/avg_loss` raises ZeroDivisionError.
- `%B` when the Bollinger band width is zero.

A `try/except: pass` around an indicator is a silent-failure factory. Any
swallow that returns a stale or default value while the caller believes it is
live data is a MAJOR finding at minimum.

## A7. VWAP specifics

- VWAP must reset at session open. A VWAP carried across days is a slow
  drifting line with no meaning.
- It must be volume-weighted on *traded* volume, not tick count.
- For options, VWAP on the option premium and VWAP on the underlying are
  different lines with different uses. Confirm the caller wants the one it
  is getting.

## How to demonstrate a finding here

Load real archived candles, run both implementations (or the implementation
and a hand-computed reference for the first few bars), and print:

```
index   impl_A      impl_B      diff
  14    123.4500    123.4500    0.0000
  15    124.1120    123.9980    0.1140   <- first divergence
```

Then state which call sites use A and which use B, and what decision each one
drives. That converts a code-reading observation into a demonstrated defect.
