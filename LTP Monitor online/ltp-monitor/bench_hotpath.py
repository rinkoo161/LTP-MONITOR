#!/usr/bin/env python3
"""bench_hotpath.py — measured tick-to-decision latency. Numbers, not adjectives.

Per ltp-monitor-claude-code-brief.md: re-run after every performance
commit and record before/after. A change that does not measurably move
p99 gets reverted.

WHAT IS ACTUALLY BEING MEASURED, stated plainly so the numbers are not
over-read: this system's "tick" is a 60s option-chain SNAPSHOT (REST at
3s cadence, or a websocket LTP overlay on top of it), not a per-trade
tick feed. So the decision path timed here is

    archived chain frame  ->  analyzer.analyze()          (per-strike view)
                          ->  strategies.evaluate()       (spread admission)
                          ->  instant_exit_reason()       (the exit predicate)

Everything is replayed from `history.chain_snapshots` — real frames, real
strike counts, real greeks — with NO network and NO broker involved.

Deliberately EXCLUDED, because including them would flatter the result:
the broker round trip (~100-3000 ms, dominated by Dhan's 1-per-3s chain
limit) and the 2s ExecutionAgent cycle. Those two dominate real
wall-clock reaction time by three orders of magnitude over anything
measured here — which is itself the most important finding in the audit.

Usage:  ./venv/bin/python3 bench_hotpath.py [--symbol NIFTY] [--frames 300]
"""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyzer
import config
import history
import strategies


def _pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def _report(name, samples_ms, budget_ms=None):
    if not samples_ms:
        print(f"  {name:28} no samples")
        return
    line = (f"  {name:28} n={len(samples_ms):<5} "
            f"p50={_pct(samples_ms, 50):7.3f}  "
            f"p95={_pct(samples_ms, 95):7.3f}  "
            f"p99={_pct(samples_ms, 99):7.3f}  "
            f"max={max(samples_ms):8.3f}  (ms)")
    if budget_ms is not None:
        line += f"   {'OK' if _pct(samples_ms, 99) < budget_ms else 'OVER'} vs {budget_ms}ms p99 budget"
    print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--frames", type=int, default=300)
    a = ap.parse_args()

    cfg = config.load()
    days = history.chain_days(a.symbol)
    if not days:
        print(f"no archived chain days for {a.symbol} — nothing to measure")
        return 1
    day = days[-1]
    expiry = history.front_expiry_on(a.symbol, day)
    frames = list(history.day_chain_frames(a.symbol, day, expiry=expiry))[:a.frames]
    if not frames:
        print(f"no frames on {day}")
        return 1

    print(f"\n  HOT-PATH LATENCY — {a.symbol} {day}, {len(frames)} archived "
          f"chain frames (no network)")
    print(f"  python {sys.version.split()[0]} · "
          f"strikes/frame ~{len(frames[0][1].get('rows') or [])}\n")

    t_analyze, t_eval, t_exit, t_total = [], [], [], []
    regime = {"regime": "rangebound"}
    for ts, chain in frames:
        f0 = time.perf_counter()

        t0 = time.perf_counter()
        an = analyzer.analyze(chain)
        t_analyze.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        for name in ("bull_put_spread", "bear_call_spread"):
            try:
                strategies.evaluate(name, an, regime)
            except Exception:
                pass          # a frame that cannot be evaluated still costs time
        t_eval.append((time.perf_counter() - t0) * 1000)

        # The exit predicate runs per open position on every 2s cycle —
        # the single most latency-sensitive decision in the system.
        row = (an.get("strikes") or [{}])[0]
        pos = {"ltp": (row.get("ce") or {}).get("ltp") or 100.0, "leg": "CE",
               "stoploss": 50.0, "target2": 300.0, "t1_hit": False,
               "spot_invalidation": None}
        t0 = time.perf_counter()
        try:
            import agents
            agents.instant_exit_reason(pos, pos["ltp"], an.get("spot"))
        except Exception:
            pass
        t_exit.append((time.perf_counter() - t0) * 1000)

        t_total.append((time.perf_counter() - f0) * 1000)

    _report("analyzer.analyze()", t_analyze)
    _report("strategies.evaluate() x2", t_eval)
    _report("instant_exit_reason()", t_exit)
    _report("TOTAL per frame", t_total, budget_ms=5.0)

    print(f"\n  CONTEXT — what actually dominates reaction time:")
    print(f"    broker chain fetch      ~3000 ms  (Dhan: 1 request / 3 s, hard limit)")
    print(f"    ExecutionAgent cycle    ~2000 ms  (interval=2)")
    print(f"    exit_quote_max_age_sec  {cfg.get('exit_quote_max_age_sec', 90)*1000:>6} ms  (staleness ceiling)")
    print(f"    ^ compute above is {statistics.mean(t_total):.2f} ms mean — "
          f"~{3000 / max(statistics.mean(t_total), 0.001):.0f}x smaller than one fetch interval.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
