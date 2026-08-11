# AUDIT — LTP Monitor hot path, failure modes, ranked risks

Produced per `ltp-monitor-claude-code-brief.md` Kickoff item 3.
**Code audited:** v59.79 (`df340a5`), tagged `stable-baseline-2026-08-11`.
**Measurements:** `bench_hotpath.py`, 300 real archived NIFTY chain frames
from 2026-08-10, no network. Re-runnable; re-run after every perf commit.

> **Read the reconciliation first.** `docs/BRIEF-RECONCILIATION.md` records
> where the brief's assumptions and this repo's reality differ. In short: the
> brief describes a *read-only tick monitor for a 0-DTE short straddle*; this
> repo is an *order-placing multi-strategy system* on 60-second option-chain
> snapshots. Several of the brief's targets (p99 < 5 ms, "tick" semantics)
> are inherited from that other architecture and are re-derived below rather
> than adopted blindly.

---

## 1. Module map — the decision path

```
  BROKER (Dhan REST, 1 chain request / 3 s hard limit)
        │                                    ┌─ dhan_ws.py (optional LTP/OI overlay)
        ▼                                    ▼
  MarketDataAgent.cycle()            [agents.py:1305+]   every 3 s, ONE symbol per cycle
        │  writes bus: chain:{SYM}, chain_ts:{SYM}, spot_hist:{SYM}
        ▼
  analyzer.analyze(chain)            [analyzer.py]       per-strike view + OI quadrants
        │  writes bus: analysis:{SYM}                    ← classify_leg() single source
        ▼
  ┌───────────────────────────┬──────────────────────────┐
  │ StrategyAgent (event)     │ ExecutionAgent._auto_spreads()   every 60 s
  │   pa_strategies.evaluate  │   strategies.evaluate()   ← 2.78 MB disk read, see §3
  │   ta_elliott / ew_reversal│                           │
  ▼                           ▼                          ▼
  RiskAgent.evaluate()        [agents.py:3900+]  gates: runway, feasibility, daily loss,
        │                                        news, confidence, R:R, concurrency
        ▼
  ExecutionAgent.cycle()      [agents.py:4528+]  every 2 s, per-step isolated
        ├─ _check_portfolio_kill_switch()   ← guard; failure halts entries (v59.72)
        ├─ _monitor() → _monitor_one()      ← instant_exit_reason() = the exit predicate
        ├─ _monitor_spreads() / _monitor_futures()
        └─ _drain_entry_queue() → _enter() → broker order
```

**The genuinely latency-sensitive step is `instant_exit_reason()`** — the
predicate deciding whether an open position has hit its stop/target. It runs
per position per 2 s cycle. Measured p99 **0.116 ms**. That is the number that
matters for "could a slow decision cost money", and it is three orders of
magnitude inside its budget.

---

## 2. Measured latency (300 frames, ~113 strikes/frame)

| Stage | p50 | p95 | p99 | max |
|---|---|---|---|---|
| `analyzer.analyze()` | 4.917 | 11.321 | 25.732 | 101.780 |
| `strategies.evaluate()` ×2 | 44.249 | 106.010 | 250.167 | 507.480 |
| `instant_exit_reason()` | **0.007** | **0.023** | **0.116** | 0.915 |
| **TOTAL per frame** | 49.119 | 122.157 | **256.344** | 516.160 |

(milliseconds)

### How to read this honestly

Against the brief's inherited **p99 < 5 ms** budget, the total is **50× over**.
But that budget assumes a per-trade tick feed where compute sits between the
tick and the order. This system's cadence is set by the broker, not by compute:

| Interval | Value |
|---|---|
| Broker chain fetch (Dhan hard limit) | ~3,000 ms |
| ExecutionAgent cycle | 2,000 ms |
| Spread re-evaluation gate | 60,000 ms |
| Stale-quote ceiling for exits | 90,000 ms |

Mean compute is **61.9 ms — about 2 % of a single fetch interval**. So the
finding is *not* "the system is too slow to trade"; it is **"there is
avoidable disk I/O on a live decision path, and it grows without bound"**
(§3). The exit predicate — the only step where milliseconds could plausibly
cost money — is already fast.

---

## 3. Blocking I/O on the decision path — the real finding

**`strategies.evaluate()` re-reads and JSON-parses `strategy_versions.json`
on every call.**

- `strategies.py:evaluate()` → `backtester.get_params()` → `load_versions()`
  → `json.load(open(VERS_PATH))`.
- Measured: `load_versions()` **19.72 ms/call**; `get_params()` 19.38 ms;
  file size **2.78 MB** and growing (it was 1.9 MB after the v59.67 clean —
  the daily run re-adds `oos` blocks and `trades_detail`).
- Called twice per symbol per 60 s evaluation → 4 symbols × 2 strategies =
  **8 × 19.7 ms ≈ 158 ms of pure JSON parsing per minute**, forever, growing.

This is the textbook version of the brief's "blocking I/O inside the hot
path". Fix (Phase A, issue #1): cache on file mtime — the same shape
`app._lifetime_trade_totals()` already uses.

Other I/O touching the path, in descending order:

| Site | Cost | Note |
|---|---|---|
| `load_versions()` via `get_params()` | 19.7 ms | above — the one that matters |
| `config.load()` | 0.13 ms | uncached, re-parses `config.json`; called many times per cycle (`_monitor_one` twice per position). Cheap individually, sloppy in aggregate. |
| `_append_activity()` | ~0.1 ms + `getsize` syscall per line | one stat call per log line since the v59.71 rotation |
| `_append_trade()` / `_save_open_state()` | ms-scale | exit path only, not per tick |
| `history.*` writes | ms-scale | snapshot writer, off the decision path |

**No pandas, no LLM call, and no network call sits on the exit path.** The
LLM advisory paths are explicitly time-gated (`ai_advisory_due`) and their
exits execute *outside* the try (v59.71), so an LLM stall cannot delay a
stop-loss.

---

## 4. Failure modes

| Scenario | Current behaviour | Verdict |
|---|---|---|
| **WebSocket drop** (market feed) | `dhan_ws` reconnects; REST polling remains the source of truth for chain shape/greeks | Handled |
| **WebSocket drop** (order updates) | `dhan_order_ws` reconnects with 5→60 s backoff; polling confirm + reconciler are independent belts | Handled (v59.76) |
| **Tick/snapshot gap** | Exits refuse to act on quotes older than `exit_quote_max_age_sec` (90 s) and say so; P&L is not updated from stale prices | Handled (v59.69) |
| **Out-of-order / duplicate snapshot** | **No sequence or duplicate detection.** Frames are keyed by timestamp on write; a repeated frame is simply re-processed | **GAP — issue #4** |
| **Broker 429** | Shared `rate_limit.py` cooldown + per-symbol backoff to 300 s | Handled |
| **Clock skew vs exchange** | **Not checked.** All logic uses local IST (`store.ist_now()`); a skewed host silently shifts session boundaries and the square-off | **GAP — issue #5** |
| **Mid-session restart** | Positions re-seeded from `open_state.json`; live mode reconciles against broker positions every 300 s; realized day-P&L derives from `closed_trades`, so the daily-loss gate survives restarts (v59.69) | Handled |
| **Kill-switch step crashes** | Entry-generating steps skipped, `portfolio_halt_until` raised, HIGH alert (v59.72) | Handled |
| **Feed dies with positions open** | Kill-switch inputs go stale → throttled "UNVERIFIED" alert; it still cannot distinguish flat from unknown | Partial — issue #6 |
| **Broker rejects an order** | REJECTED/CANCELLED acted on: no phantom entry, no booked exit (v59.72) | Handled |
| **Partial fill** | HIGH alert; the book tracks intended qty | Partial — no order splitting |

---

## 5. Where a silently WRONG number could still be produced

1. **The 0.5-delta P&L proxy** in `replay_pa`/`replay_ew_reversal`/
   `replay_ta_elliott` (`backtester.py`): backtest P&L is index-points ×
   0.5 × lot, not real premiums. Measured error sd ≈ ₹1,143/trade. It feeds
   the promotion gate — which is why the gate carries that sd as an explicit
   margin term rather than pretending the number is exact.
2. **`spot` vs futures price**: index level is used for signals; futures
   trade at a basis. `basis_residual.py` measures it, but no strategy
   currently corrects for it.
3. **Lot size**: reconciled against the scrip master at startup with a HIGH
   alert on mismatch (v59.68) — but it is *report-only*, so a stale map keeps
   scaling every rupee figure until a human acts.
4. **Unrealized P&L on the dashboard is gross** of costs (labelled in the
   API since v59.72); only realized P&L is net.
5. **The slippage impact alpha (0.5) is an assumption**, not a measurement.
   It is applied live but not in the replays' 1-lot pricing — a known,
   documented divergence for multi-lot trades.

---

## 6. Top 10 issues, ranked by (risk of real money loss) × (likelihood)

| # | Issue | Risk × Likelihood | Fix |
|---|---|---|---|
| 1 | `load_versions()` — 19.7 ms disk+parse of a 2.78 MB file on the live spread path, growing unbounded | Low × **Certain** | mtime cache; cap `trades_detail` in persisted versions |
| 2 | **No strategy has demonstrated an edge that clears its own costs** — the record's gross edge sits below friction | **Severe** × Observed | Not a code fix. The feasibility gate (v59.73) blocks the impossible trades; the OOS gate decides the rest. |
| 3 | Real broker charges never reconciled against the cost model (zero live trades to date) | **Severe** × Unknown | The 1-lot live pilot + contract-note reconciliation |
| 4 | No duplicate/out-of-order snapshot detection | Medium × Medium | Sequence + hash guard on frame ingest |
| 5 | No clock-skew check vs exchange timestamps | **High** × Low | Compare broker timestamp to local IST; alert past N seconds |
| 6 | Kill-switch cannot distinguish "flat" from "feed dead" | High × Low | Treat stale inputs as a halt condition, not just an alert |
| 7 | `config.load()` uncached, called many times per cycle | Low × Certain | mtime cache (same shape as #1) |
| 8 | Multi-lot slippage modelled live but not in replays | Medium × Medium | Apply the impact model in replays, or price the gate's bias at live size |
| 9 | Partial fills alert but do not split orders | Medium × Low | Order splitting under freeze quantity |
| 10 | `history.db` at 460 MB+ with no size ceiling | Low × Certain | Tiering exists (v59.0); add a disk-space alert |

**Note on #2 and #3:** these are the only two that can lose real money at
scale, and neither is a latency or refactor problem. Everything measured in
§2 is comfortably inside the intervals that actually govern this system.
Optimising the hot path is worth doing for hygiene (issue #1 is free), but it
should not be mistaken for progress on the two issues that matter.
