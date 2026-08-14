#!/usr/bin/env python3
"""test_replay_gate_parity.py — v59.88.

`_edge_ok_pa`'s docstring states the invariant: "the admission bar must
match live or the replay measures a different strategy". v59.86 added
`edge_feasibility.target_reachable` to the LIVE risk gate and not to
the replay, breaking exactly that.

Closing it was not a copy-paste, and the two wrong turns are worth
recording because both produce a plausible-looking replay:

  1. The live gate is a percentage of PREMIUM; these replays carry only
     `entry_spot`/`t1_spot`. Applying the live threshold to spot values
     compares a spot quantity against a premium one — the dimensional
     bug analyzer.option_stop_geometry's own comment documents.

  2. Gating the strategy's OWN spot target is still wrong: live routes
     PriceAction/MTF signals through option_stop_geometry, which
     DISCARDS that target and rebuilds target1 on the premium as
     entry x (1 + stop_pct x 2). Measured: gating the strategy target
     blocked 3% of replay entries against 73% of live signals.

So the replay reads the real premium out of the chain archive and calls
the SAME two functions live calls. Where the archive has no chain for
that moment it fails OPEN — a replay must never invent a refusal live
would not have made.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_replay_gate_parity")

import backtester as B
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


SRC = open("backtester.py").read()

print("1) the live gate is applied at every PA admission point")
check("the replay has a reachability check", "def _reachable_ok_pa(" in SRC)
check("it is wired at EVERY _edge_ok_pa site, not just one",
      SRC.count("_reachable_ok_pa(ev,") == SRC.count("_edge_ok_pa(ev,"),
      f"{SRC.count('_reachable_ok_pa(ev,')} reachability vs "
      f"{SRC.count('_edge_ok_pa(ev,')} cost — an admission bar applied "
      f"to some strategies is not an admission bar")

print("\n2) it calls what live calls, rather than re-deriving it")
_i = SRC.index("def _reachable_ok_pa(")
BODY = SRC[_i:SRC.index("\ndef ", _i + 10)]
check("it builds geometry with analyzer.option_stop_geometry",
      "option_stop_geometry(" in BODY,
      "live discards the strategy's spot target and rebuilds it here")
check("it decides with edge_feasibility.target_reachable",
      "target_reachable(" in BODY, "the same function the risk gate calls")
check("it sources atr_pct from the real classifier, not a constant",
      "historical_regime(" in BODY,
      "historical_regime feeds archived bars to RegimeAgent._classify")
check("it does NOT gate the strategy's own spot target",
      "t1_spot, cfg" not in BODY and "target_reachable(prem, target_prem" not in BODY,
      "that target is discarded live")

print("\n3) it fails OPEN when the archive cannot answer")
cfg = {**config.DEFAULTS}
check("no premium available -> admitted",
      B._reachable_ok_pa({"entry_spot": 24000, "t1_spot": 24100},
                         "NOSUCHSYM", 1, cfg) is True,
      "a replay must not invent a refusal live would not have made")
check("missing geometry -> admitted",
      B._reachable_ok_pa({}, "NIFTY", 1, cfg) is True)

print("\n4) the premium lookup reads the real archive")
_j = SRC.index("def _atm_premium_at(")
LOOK = SRC[_j:SRC.index("\ndef ", _j + 10)]
check("it queries chain_snapshots via history",
      "get_chain_snapshot_map(" in LOOK,
      "the same archive replay_spreads walks")
check("it picks the ATM strike by distance from spot",
      "min(strikes" in LOOK and "abs(" in LOOK)
check("it is cached, since the PA replays evaluate every bar",
      "_cache" in LOOK)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
