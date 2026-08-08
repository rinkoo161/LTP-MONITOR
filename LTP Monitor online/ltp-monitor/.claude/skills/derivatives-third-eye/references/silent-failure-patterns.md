# Tier 4 — Silent Failure Patterns

This tier answers: **when this breaks, will anyone find out?**

Trading systems fail differently from ordinary software. An ordinary bug produces a stack
trace and a bug report. A trading-system bug produces a plausible number, a green dashboard,
and a slowly draining account. The review priority is therefore not "is this correct" but
"is incorrectness detectable."

## Contents
- [Exception swallowing](#exception-swallowing)
- [Fallbacks that mask](#fallbacks-that-mask)
- [The single-source-of-truth problem](#the-single-source-of-truth-problem)
- [Parity oracles](#parity-oracles)
- [Test discipline](#test-discipline)
- [Concurrency and shared state](#concurrency-and-shared-state)
- [Observability requirements](#observability-requirements)
- [Checklist](#checklist)

---

## Exception swallowing

The highest-yield grep in any trading codebase:

```bash
grep -rn "except:" --include=*.py .
grep -rn "except Exception" --include=*.py .
grep -rn "except.*:\s*pass" --include=*.py .
```

For each hit, ask which exceptions it is *intended* to catch. A handler written for transient
network errors will equally catch `NameError`, `AttributeError`, `KeyError`, and `TypeError` —
that is, it will catch **code bugs** and log them as if they were network blips. The strategy
then silently stops evaluating, and the log line looks routine.

The rule to apply: catch the specific exception classes that represent genuine transient
conditions (connection errors, timeouts, rate limits). Let everything else raise. If a broad
catch is unavoidable at a top-level loop boundary, it must re-raise programming errors:

```python
except (NameError, AttributeError, TypeError, KeyError):
    raise            # code bugs must surface
except (ConnectionError, TimeoutError) as e:
    log.warning("transient: %s", e)
```

Also check: does the log distinguish "this cycle did nothing because conditions weren't met"
from "this cycle did nothing because it crashed"? If both produce the same log line, the
system cannot tell you it is broken.

## Fallbacks that mask

A fallback is a decision to continue with degraded input. Each one needs to be examined for
whether continuing is actually safe.

- `value = data.get('ltp', 0)` — a missing price becomes zero, and zero flows into a P&L
  calculation or a comparison, producing a plausible-looking wrong answer.
- Default parameters substituted when config load fails — the system runs, on parameters
  nobody chose.
- Cached data returned when a fetch fails, with no staleness check — decisions made on old
  prices.
- An indicator returning `None` treated as "no signal" when it actually means "computation
  failed."

For each fallback, ask: **is silently continuing better than stopping?** For anything on the
order-placement or exit path, the answer is almost always no. Halt and alert.

## The single-source-of-truth problem

If the promotion gate, the dashboard, the performance report, and the alerting all read from
the same computed number, they cannot cross-check each other. They will agree while all being
wrong, and their agreement will read as confirmation.

Look for at least one path that closes against ground truth external to the system:

- Broker contract notes or the broker's own P&L report, reconciled on a schedule.
- Account balance movement compared against computed P&L for the day.
- An independent recomputation from raw fills, by different code, with an alert on divergence
  beyond a threshold.

If no such path exists, this is a CRITICAL finding regardless of how well the rest of the
system is written. Its absence is precisely why multiple simultaneous errors can persist
undetected.

## Parity oracles

The most effective technique for validating signal logic: implement the same signal a second
time, independently, and require the two to agree on every bar.

A practical version for charting-based systems: implement the strategy's entry/exit markers in
the charting platform's language (e.g. Pine Script on TradingView), and require that every
server-side signal has a corresponding chart marker on the same bar, and vice versa. Any
mismatch is a bug in one of them.

This catches a class of bug that inspection reliably misses. A representative example: a
detector written as a **level comparison** (`if fast > slow`) rather than a **cross-event
comparison** (`if fast > slow and prev_fast <= prev_slow`) re-fires on every subsequent bar
while the condition holds. Reading the code, it looks like a crossover detector. Only the
bar-for-bar comparison against an independent implementation reveals that it fires dozens of
times instead of once.

Recommend extending parity oracles to every new strategy as a standing practice, not as a
one-off debugging tool.

## Test discipline

- **Do tests assert against executable code, or against comments and docstrings?** A test that
  greps source for a pattern can pass on a commented-out line or a docstring example. Strip
  comments before matching.
- **Do tests leave persisted state?** A test that writes to the real config file, trade log, or
  database will break subsequent tests and can corrupt live data. Check for fixtures that
  isolate persistent paths.
- **Module-level mutable state** — caches, registries, singletons — persists across tests in
  the same process. Any test reusing the same key must clear it explicitly, or it is asserting
  on a previous test's result.
- **Are there tests for the failure paths at all?** Most trading-system test suites test that
  signals fire correctly and never test what happens when the broker returns an error, the
  websocket drops mid-position, or a fill comes back partial.

## Concurrency and shared state

- Agents or threads writing to a shared bus or dict without synchronisation — check whether the
  runtime actually serialises them (asyncio single-threaded) or not.
- Unbounded growth in shared structures: an in-memory event log, alert stream, or cache with
  no maximum size grows for the life of the process. On a long-running server this manifests
  as gradual slowdown, then memory pressure, then swap thrashing — and it is usually diagnosed
  as a hardware problem rather than a leak. Every accumulating structure needs a bound
  (`collections.deque(maxlen=N)`) or an eviction policy.
- A TTL that gates reads without pruning writes is a leak wearing a cache's clothes.
- Blocking calls inside an async event loop stall every other agent. Check that broker HTTP
  calls and LLM inference calls are either async or dispatched to an executor.

## Observability requirements

For each of these, the system should be able to answer the question from logs alone:

- Why did the strategy not take a trade in the last hour? (Requires logging *which* gate
  rejected, not just that no signal fired.)
- What was the exact input state when this trade was entered?
- Has this protection ever armed?
- Is the position the system believes it holds the same as the broker's?
- When did config last change, and to what?

A system that cannot answer these will accumulate undiagnosable failures.

## Checklist

- [ ] No bare `except:`; broad handlers re-raise programming errors.
- [ ] Logs distinguish "no signal" from "crashed while evaluating."
- [ ] Every fallback default examined; none on the order/exit path silently continues.
- [ ] At least one reconciliation path closes against broker ground truth.
- [ ] A parity oracle exists for signal logic, or is recommended.
- [ ] Crossover detectors check the previous bar, not just the current level.
- [ ] Tests match executable code, isolate persistent state, and clear module-level caches.
- [ ] Failure paths have test coverage.
- [ ] All accumulating in-memory structures are bounded.
- [ ] No blocking calls inside the async event loop.
- [ ] Rejection reasons are logged per gate.
