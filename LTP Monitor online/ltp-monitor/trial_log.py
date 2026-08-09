"""trial_log.py — every parameter configuration ever evaluated.

Part 4 of the strategy-reset memo needs N, the number of configurations
tried, to deflate a Sharpe ratio. **We could not recover it.** The tuner
uses continuous bounds `(lo, hi, relax_dir)` with no grid and persists
only ACCEPTED results — 11 records were on disk for the whole search.
The true N is certainly in the hundreds or thousands and is gone, so the
protocol had to pre-commit a conservative floor of N=1000 (hurdle
E[max SR] = 3.255) rather than use a measured number.

The search space is `free_param_count()` — 71 bounded parameters across
the spread/PA/TA-Elliott/OI-composite/futures bounds dicts as of
v59.66. (An earlier revision of this docstring said 37; that figure was
stale the day it was written, which is precisely why the count is now
COMPUTED from the bounds dicts rather than transcribed. Third-eye
review, 2026-08-09.) The pre-committed N=1000 floor stands regardless:
promotion_gate.deflation_k() uses max(floor, recorded count), so the
recorded count can only ever raise the bar.

This file exists so that is a one-time loss. Every evaluation is
appended here, accepted or not, and `summary()` reports the count the
deflated Sharpe should use.

WHY ONE APPEND-ONLY FILE, and not a table in history.db:
  * the record must survive a crash mid-sweep, so it is written per
    trial rather than at the end of a run;
  * it is read a handful of times a year by an analysis script, not on
    any hot path, so there is nothing to index;
  * history.db is already the contended resource ("database is locked"
    appears in activity.log), and a sweep is exactly when it is busiest.

A failure to record must NEVER break a backtest — but it is reported,
not swallowed. A silent recorder that stops working would recreate the
precise problem this file exists to fix.
"""
import json
import os
import sys

import store

PATH = store.path("tuner_trials.jsonl")


def record(name, symbol, params, metrics, source, accepted=None):
    """Append one evaluated configuration.

    `source` says which path evaluated it — "sweep", "daily_tune",
    "audit", etc. `accepted` is tri-state: True/False when the caller
    knows whether the configuration was adopted, None when the
    evaluation itself is the whole event. N for the deflated Sharpe is
    the count of ROWS, not the count of accepted ones; that is the entire
    point.
    """
    row = {
        "ts": None,          # filled below; kept first for readability
        "strategy": name,
        "symbol": symbol,
        "params": params if isinstance(params, dict) else None,
        "trades": (metrics or {}).get("trades"),
        "net_pnl": (metrics or {}).get("net_pnl"),
        "source": source,
        "accepted": accepted,
    }
    try:
        import time as _t
        row["ts"] = _t.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        # Heal a torn final line before appending. A crash mid-write
        # leaves a fragment with no trailing newline; appending straight
        # onto it would splice the next record into that fragment and
        # lose BOTH — one row corrupted by the crash, and one good row
        # silently destroyed by the recovery. Caught by test_trial_log's
        # torn-line check, which failed on the first version of this.
        needs_nl = False
        if os.path.exists(PATH) and os.path.getsize(PATH) > 0:
            with open(PATH, "rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        with open(PATH, "a") as f:
            if needs_nl:
                f.write("\n")
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        # Loud, not silent. See the module docstring.
        print(f"  ⚠ trial_log.record FAILED ({type(e).__name__}: {e}) — "
              f"N for the deflated Sharpe will undercount",
              file=sys.stderr)
    return row


def read_all():
    if not os.path.exists(PATH):
        return []
    out = []
    for line in open(PATH):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue          # a torn final line after a crash; skip it
    return out


def count():
    """N — every configuration evaluated, accepted or not."""
    return len(read_all())


def distinct_count():
    """DISTINCT configurations evaluated — the N a deflated Sharpe
    actually needs. count() (raw rows) double-counts the daily baseline
    re-evaluations of unchanged params (source=daily_baseline), which
    would inflate the deflation bar with calendar time rather than with
    searching (v59.72, third-eye R2 finding M5)."""
    return len({(r.get("strategy"), r.get("symbol"),
                 json.dumps(r.get("params"), sort_keys=True))
                for r in read_all()})


def free_param_count():
    """Tunable parameters across every bounds dict, computed live so the
    number cannot go stale the way the hand-written '37' did. Families
    that fail to import count as 0 — the caller uses this for REPORTING;
    the deflation floor in promotion_gate does not depend on it."""
    n = 0
    try:
        import strategies
        n += sum(len(b) for b in strategies.SPREAD_BOUNDS.values())
    except Exception:
        pass
    try:
        import pa_strategies
        n += sum(len(b) for b in pa_strategies.PA_BOUNDS.values())
    except Exception:
        pass
    try:
        import ta_elliott
        n += sum(len(b) for b in ta_elliott.TA_ELLIOTT_BOUNDS.values())
    except Exception:
        pass
    try:
        import oi_composite
        n += len(getattr(oi_composite, "BOUNDS", {}) or {})
    except Exception:
        pass
    try:
        import futures_strategies
        for _fn, _defaults, bounds in futures_strategies.STRATEGIES.values():
            n += len(bounds or {})
    except Exception:
        pass
    return n


def summary():
    """N overall and broken down, for the deflated-Sharpe calculation."""
    rows = read_all()
    by_source, by_strategy, distinct = {}, {}, set()
    for r in rows:
        by_source[r.get("source")] = by_source.get(r.get("source"), 0) + 1
        k = f"{r.get('strategy')}/{r.get('symbol')}"
        by_strategy[k] = by_strategy.get(k, 0) + 1
        distinct.add((r.get("strategy"), r.get("symbol"),
                      json.dumps(r.get("params"), sort_keys=True)))
    return {"n_trials": len(rows),
            "n_distinct_configs": len(distinct),
            "free_params": free_param_count(),
            "by_source": by_source,
            "by_strategy_symbol": by_strategy,
            "path": PATH,
            "first": rows[0].get("ts") if rows else None,
            "last": rows[-1].get("ts") if rows else None}
