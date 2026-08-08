#!/usr/bin/env python3
"""test_trial_log.py — N must be a measured number, not a guess.

Part 4 of the strategy-reset memo deflates a Sharpe ratio by N, the
number of configurations tried. N was UNRECOVERABLE: the tuner searches
continuous bounds `(lo, hi, relax_dir)` with no grid and persisted only
ACCEPTED results — 11 records on disk for a search over 37 free
parameters. The protocol had to pre-commit a conservative floor of
N=1000 (hurdle E[max SR] = 3.255) instead of using a real count.

The reason it was unrecoverable is the thing this file defends: there
was more than one evaluation path. `sweep_params()` tracked its
candidates in memory and threw them away; LearningAgent's daily tuner
called `backtester.replay_spreads()` / `replay_pa()` directly and was
invisible to anything watching `_replay_for()`.

So the rule is not "log trials" — it is "there is ONE place a parameter
set gets evaluated, and it logs". A second path is how this broke.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_trial_log")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import backtester as bt
import trial_log

HERE = os.path.dirname(os.path.abspath(__file__))

print("1) the recorder works and counts EVERY row, not just accepted ones")
# DELTAS, not absolute counts. run_tests.py gives the whole suite ONE
# isolated store, and any earlier test that runs a backtest appends here
# through _replay_for — which is the point of the feature. An
# absolute "starts empty" assertion passed in isolation and failed in
# the suite, which is the same class of bug as the vacuous check in
# test_evaluate_params: a test whose result depends on what ran before
# it is not testing what it claims.
base = trial_log.count()
base_distinct = trial_log.summary()["n_distinct_configs"]
trial_log.record("bull_put_spread", "NIFTY", {"wall_gap_frac": 2.0},
                 {"trades": 5, "net_pnl": -100}, "unit", accepted=False)
trial_log.record("bull_put_spread", "NIFTY", {"wall_gap_frac": 2.5},
                 {"trades": 7, "net_pnl": 250}, "unit", accepted=True)
check("both rows are counted", trial_log.count() == base + 2,
      f"{base} -> {trial_log.count()}")
s = trial_log.summary()
check("a REJECTED configuration still counts toward N",
      s["n_trials"] == base + 2,
      "N for the deflated Sharpe is configurations TRIED. Counting only "
      "the accepted ones is what made the original N unrecoverable")
check("distinct configurations are reported separately",
      s["n_distinct_configs"] == base_distinct + 2,
      f"{base_distinct} -> {s['n_distinct_configs']}")

print("\n2) there is ONE evaluation chokepoint, and it records")
src = open(os.path.join(HERE, "backtester.py")).read()
body = src.split("def _replay_for(")[1]
body = body[:body.index("\ndef ")]
check("_replay_for records to trial_log", "trial_log.record(" in body)
check("and takes a source label", "source" in body.split("\n")[0] or
      "source=" in src.split("def _replay_for(")[1][:200])

print("\n3) the daily tuner does NOT bypass it")
AG = open(os.path.join(HERE, "agents.py")).read()
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]
bypass = [l.strip() for l in _code
          if "backtester.replay_" in l and "params=" in l]
check("no parameterised replay_* call bypasses _replay_for in agents.py",
      not bypass,
      f"{bypass} — this is the exact path that made N unrecoverable: "
      f"the daily tuner evaluated candidates through replay_spreads()/"
      f"replay_pa() and nothing watching _replay_for() ever saw them")
check("the tuner calls _replay_for instead",
      AG.count("backtester._replay_for(") >= 2,
      f"{AG.count('backtester._replay_for(')} call sites")

print("\n4) a recorder failure is REPORTED, never swallowed")
tl = open(os.path.join(HERE, "trial_log.py")).read()
check("record() prints on failure rather than passing",
      "except Exception" in tl and "print(" in tl.split("except Exception")[1][:300],
      "a silently-broken recorder recreates precisely the gap this "
      "module exists to close")
check("_replay_for's guard also reports",
      "except Exception" in body and "print(" in body.split("except Exception")[1][:200],
      "and a trial_log failure must not take a backtest down with it")

print("\n5) it survives a torn line (crash mid-append)")
with open(trial_log.PATH, "a") as f:
    f.write('{"strategy": "bull_put_spread", "sym')      # truncated write
check("read_all skips the torn line instead of raising",
      trial_log.count() == base + 2, str(trial_log.count()))
trial_log.record("orb", "BANKNIFTY", {"buf_frac": 0.1},
                 {"trades": 1, "net_pnl": 10}, "unit")
check("and appending still works afterwards", trial_log.count() == base + 3,
      f"{trial_log.count()} — without healing the torn line, this "
      f"record splices onto the fragment and BOTH are lost")

print("\n6) it does not live in the contended database")
check("the log is its own file, not a history.db table",
      trial_log.PATH.endswith(".jsonl"),
      f"{trial_log.PATH} — a sweep is exactly when history.db is "
      f"busiest, and 'database is locked' is already in activity.log")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all trial-log checks passed")
