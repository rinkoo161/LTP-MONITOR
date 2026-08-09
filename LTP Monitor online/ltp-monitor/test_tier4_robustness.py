#!/usr/bin/env python3
"""test_tier4_robustness.py — v59.71, third-eye Tier 4 (code robustness).

Failure-domain isolation, loud agent errors, exchange-clock dates in the
data layer, and bounds on the structures that used to grow forever.
Executable wherever a seam exists; AST checks (not string greps) where
the property is structural.
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_tier4_robustness")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeBus:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.alerts, self.logs = [], []
    def get(self, k, d=None):
        return self.state.get(k, d)
    def set(self, k, v):
        self.state[k] = v
    def log(self, name, msg):
        self.logs.append(msg)
    def alert(self, sev, src, sym, msg):
        self.alerts.append((sev, msg))


# --- AI auto-exit: the exit call lives OUTSIDE the advice try ----------
# AST, not grep: for each advisory method, no call to exit/exit_future/
# exit_spread may sit inside a Try whose handler assigns "ai_advice".
def _exit_calls_inside_advice_try(fn):
    tree = ast.parse(inspect.getsource(fn).lstrip() if False else
                     "\n".join(l[4:] if l.startswith("    ") else l
                               for l in inspect.getsource(fn).splitlines()))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handles_advice = any("ai_advice" in ast.dump(h) for h in node.handlers)
        if not handles_advice:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                    and inner.func.attr in ("exit", "exit_future", "exit_spread"):
                bad.append(inner.func.attr)
    return bad

for fn, label in ((agents.ExecutionAgent._futures_ai_check, "futures"),
                  (agents.ExecutionAgent._spread_ai_check, "spread"),
                  (agents.ExecutionAgent._option_ai_check, "option")):
    bad = _exit_calls_inside_advice_try(fn)
    check(f"{label} AI check: no exit call inside the advice try",
          not bad, f"found {bad} inside the swallow")

# --- per-step isolation in ExecutionAgent.cycle ------------------------
ran = []
ex = object.__new__(agents.ExecutionAgent)
ex.name = "execution"
ex.bus = FakeBus()
def _boom():
    ran.append("kill_switch")
    raise RuntimeError("deterministic bug")
_boom.__name__ = "_check_portfolio_kill_switch"   # the log names step.__name__
ex._check_portfolio_kill_switch = _boom
for name in ("_reconcile_broker", "_drain_entry_queue", "_monitor",
             "_monitor_spreads", "_monitor_futures",
             "_futures_signal_engine", "_auto_spreads"):
    setattr(ex, name, (lambda n=name: ran.append(n)))
try:
    agents.ExecutionAgent.cycle(ex)
    check("cycle re-raises the guard error", False, "no exception")
except RuntimeError:
    check("cycle re-raises the guard error", True)
# v59.72 (R2 finding H1) — a crashing GUARD must not be treated like a
# crashing monitor: monitors/exits still run, but the entry-generating
# steps are SKIPPED and the shared halt flag is raised.
check("monitors still run when the guard crashes",
      ran == ["kill_switch", "_reconcile_broker", "_monitor",
              "_monitor_spreads", "_monitor_futures"], str(ran))
check("entry-generating steps are SKIPPED when the guard crashes",
      "_drain_entry_queue" not in ran and "_auto_spreads" not in ran
      and "_futures_signal_engine" not in ran)
check("a crashing guard raises the shared entry-halt flag",
      (ex.bus.get("portfolio_halt_until") or 0) > 0)
check("the guard failure reaches the alert stream",
      any(s == "high" and "kill-switch UNAVAILABLE" in m
          for s, m in ex.bus.alerts), str(ex.bus.alerts))
check("the failing step is named in the log",
      any("_check_portfolio_kill_switch" in m for m in ex.bus.logs))
# and a NON-guard step failure still lets everything else run:
ran2 = []
ex2s = object.__new__(agents.ExecutionAgent)
ex2s.name = "execution"
ex2s.bus = FakeBus()
ex2s._check_portfolio_kill_switch = lambda: ran2.append("guard")
def _mon_boom():
    ran2.append("_monitor")
    raise KeyError("monitor bug")
_mon_boom.__name__ = "_monitor"
ex2s._monitor = _mon_boom
for name in ("_reconcile_broker", "_drain_entry_queue", "_monitor_spreads",
             "_monitor_futures", "_futures_signal_engine", "_auto_spreads"):
    setattr(ex2s, name, (lambda n=name: ran2.append(n)))
try:
    agents.ExecutionAgent.cycle(ex2s)
    check("monitor error still re-raised", False)
except KeyError:
    check("monitor error still re-raised", True)
check("a monitor crash skips nothing else",
      ran2 == ["guard", "_reconcile_broker", "_drain_entry_queue",
               "_monitor", "_monitor_spreads", "_monitor_futures",
               "_futures_signal_engine", "_auto_spreads"], str(ran2))

# --- per-position isolation in _monitor --------------------------------
mex = object.__new__(agents.ExecutionAgent)
mex.name = "execution"
mex.summary = ""
mex.bus = FakeBus({"positions": {"A": {"symbol": "A"}, "B": {"symbol": "B"}}})
def _mon_one(p):
    if p["symbol"] == "A":
        raise KeyError("malformed position")
    return "B ok"
mex._monitor_one = _mon_one
agents.ExecutionAgent._monitor(mex)
check("one malformed position does not kill the others' monitoring",
      "B ok" in mex.summary, mex.summary)
check("the unmonitorable position raises a HIGH alert",
      any(s == "high" and "NOT being enforced" in m for s, m in mex.bus.alerts),
      str(mex.bus.alerts))

# --- Agent.run alerts on crashing cycles -------------------------------
class _T(agents.Agent):
    name = "t"
    interval = 0
    def cycle(self):
        self._n = getattr(self, "_n", 0) + 1
        if self._n >= 3:
            self.stop_evt.set()
        raise ValueError("always broken")

t = _T(FakeBus(), {})
t.run()
crash_alerts = [m for s, m in t.bus.alerts if "CRASHED" in m]
check("a crashing agent reaches the alert stream (first error)",
      len(crash_alerts) >= 1, f"{len(crash_alerts)} alerts for 3 crashes")
check("but not on every single cycle (throttled by consecutive count)",
      len(crash_alerts) == 1, f"{len(crash_alerts)}")
check("consecutive-crash accounting counted every cycle",
      getattr(t, "_consec_errors", 0) == 3, str(getattr(t, "_consec_errors", 0)))
check("every crash still reaches the feed log",
      sum(1 for m in t.bus.logs if "always broken" in m) == 3)

# --- exchange-clock dates in the data layer ----------------------------
check("store exposes the IST clock",
      store.IST.utcoffset(None).total_seconds() == 5.5 * 3600)
check("ist_today returns a date", hasattr(store.ist_today(), "isoformat"))
here = os.path.dirname(os.path.abspath(__file__))
for mod in ("history.py", "broker_adapter.py", "risk_engine.py",
            "analyzer.py", "config.py"):
    src = open(os.path.join(here, mod)).read()
    check(f"{mod} has no naive date.today()", "date.today()" not in src)
for mod in ("risk_engine.py", "config.py"):
    src = open(os.path.join(here, mod)).read()
    naked = [l for l in src.splitlines()
             if "datetime.now()" in l and "ist_now" not in l]
    check(f"{mod} has no naive datetime.now()", not naked, str(naked[:2]))

# --- bounded structures -------------------------------------------------
_orig_load = config.load
_base = config.load()
try:
    config.load = lambda: {**_base, "closed_trades_memory_cap": 3}
    b = FakeBus()
    for i in range(5):
        agents._record_closed(b, {"pnl": i})
    kept = b.get("closed_trades")
    check("closed_trades window is capped at the configured size",
          len(kept) == 3 and kept[-1]["pnl"] == 4, str(kept))
finally:
    config.load = _orig_load

# activity.log rotation
scratch = os.path.join(store.home(), "test_activity.log")
_orig_logfile = agents.LOG_FILE
try:
    agents.LOG_FILE = scratch
    with open(scratch, "w") as f:
        f.write("x" * (11 * 1024 * 1024))
    agents._append_activity("fresh line")
    rotated = os.path.exists(scratch + ".1")
    small = os.path.getsize(scratch) < 1024
    check("activity log rotates past the size cap",
          rotated and small,
          f"rotated={rotated}, new size={os.path.getsize(scratch)}")
finally:
    agents.LOG_FILE = _orig_logfile
    for p in (scratch, scratch + ".1"):
        if os.path.exists(p):
            os.remove(p)

# reentry-block pruning exists at the write site (structural)
_exit_src = inspect.getsource(agents.ExecutionAgent.exit)
check("option_reentry_block prunes lapsed keys when writing",
      "option_reentry_cooldown_sec" in _exit_src
      and "_now_prune - v < _ttl" in _exit_src)

check("closed_trades_memory_cap registered in DEFAULTS",
      "closed_trades_memory_cap" in config.DEFAULTS)

# --- v59.74: archived candles are shape-identical to the live pack -----
# zigzag_series keys candles by "time"; day_index_candles rows carried
# only "ts", so the FIRST real ew_reversal/sg_ema run through run_all
# (2026-08-09 15:45) raised KeyError('time') and voided every symbol's
# daily results. Executable against the real loader + real zigzag, via
# the isolated store's own DB.
import history
import structure
from datetime import datetime as _dtdt
_day = "2026-01-05"
_t0 = int(_dtdt.fromisoformat(_day).timestamp())
_c = history._conn()
_c.execute("INSERT OR REPLACE INTO instruments(security_id, symbol, kind) "
           "VALUES ('999001', 'TESTIDX', 'idx')")
for _i in range(40):
    _px = 100 + (_i % 7)
    _c.execute("INSERT OR REPLACE INTO candles(security_id, ts, o, h, l, c) "
               "VALUES ('999001', ?, ?, ?, ?, ?)",
               (_t0 + 34200 + _i * 60, _px, _px + 1, _px - 1, _px + 0.5))
_c.commit()
_rows = history.day_index_candles("TESTIDX", _day)
check("archived candles carry BOTH 'ts' and 'time'",
      _rows and all("ts" in r and "time" in r and r["ts"] == r["time"]
                    for r in _rows), f"{len(_rows)} rows")
try:
    _pivots = structure.zigzag_series(_rows)
    check("zigzag_series accepts archived candles (the 15:45 crash shape)",
          isinstance(_pivots, list))
except KeyError as e:
    check("zigzag_series accepts archived candles (the 15:45 crash shape)",
          False, f"KeyError: {e}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tier-4 robustness checks passed")
