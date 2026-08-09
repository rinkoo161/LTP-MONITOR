#!/usr/bin/env python3
"""test_version_hygiene.py — v59.67.

A strategy version may only be created for a POSITIVE, meaningfully
improved result (operator requirement, 2026-08-09 — the old rule minted
a version for a 15%-smaller loss, and 67 of 85 versions on the live
install were non-positive dead weight). clean_versions prunes the ones
already persisted. Both are pure functions, tested by execution.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_version_hygiene")

import backtester as bt
import clean_versions as cv

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- version_worthy: the creation rule ----------------------------------
check("smaller loss is NOT version-worthy (the bug that filled the store)",
      not bt.version_worthy(-1000, -500))
check("a much smaller loss is still not worthy — negative never is",
      not bt.version_worthy(-66123, -1))
check("flipping to profit IS worthy", bt.version_worthy(-1000, 500))
check("first positive result (no incumbent) is worthy",
      bt.version_worthy(0, 500))
check("equal positive is NOT worthy ('not even the same')",
      not bt.version_worthy(1000, 1000))
check("sub-threshold positive improvement is NOT worthy",
      not bt.version_worthy(1000, 1100))
check("15%+ positive improvement IS worthy", bt.version_worthy(1000, 1200))
check("a positive result WORSE than the incumbent is NOT worthy",
      not bt.version_worthy(1000, 900))
check("None result is NOT worthy", not bt.version_worthy(100, None))

# --- the three creation sites all use it --------------------------------
# Tripwire, not proof: the executable guarantee is the truth table above;
# this catches a future path quietly reverting to meaningful_improvement.
ag = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
check("agents.py routes version creation through version_worthy (3 sites)",
      ag.count("backtester.version_worthy(") >= 3,
      f"found {ag.count('backtester.version_worthy(')}")
check("no creation site still ORs the sign test with meaningful_improvement",
      "or backtester.meaningful_improvement(" not in ag)

# --- clean(): prunes exactly the unwanted -------------------------------
synth = {
    "stratA": {"symbols": {"NIFTY": {
        "active": 3, "live_enabled": False, "manually_disabled": False,
        "versions": [
            {"v": 1, "params": {}, "reason": "initial",
             "results": {"trades": 10, "net_pnl": -500,
                         "trades_detail": [1], "equity_curve": [1]}},
            {"v": 2, "params": {}, "reason": "tightening",
             "results": {"trades": 8, "net_pnl": -200}},          # prune
            {"v": 3, "params": {}, "reason": "tightening",
             "results": {"trades": 9, "net_pnl": -900,
                         "trades_detail": [1]}},                  # active: keep
            {"v": 4, "params": {}, "reason": "sweep", "deployed": True,
             "results": {"trades": 20, "net_pnl": -50,
                         "equity_curve": [1]}},                   # deployed: keep
            {"v": 5, "params": {}, "reason": "sweep",
             "results": {"trades": 20, "net_pnl": 800,
                         "trades_detail": [1],
                         "oos": {"trades": 5, "trades_detail": [1]}}},  # positive: keep
        ]}}},
}
cleaned, stats = cv.clean(synth)
kept = {x["v"] for x in cleaned["stratA"]["symbols"]["NIFTY"]["versions"]}
check("keeps active, v1, deployed, and positive versions",
      kept == {1, 3, 4, 5}, f"kept {sorted(kept)}")
check("prunes the negative non-active version",
      2 not in kept and len(stats["removed"]) == 1)
check("input is not mutated (pure function)",
      len(synth["stratA"]["symbols"]["NIFTY"]["versions"]) == 5)
vers_by_v = {x["v"]: x for x in cleaned["stratA"]["symbols"]["NIFTY"]["versions"]}
check("active version keeps its heavy fields",
      "trades_detail" in vers_by_v[3]["results"])
check("non-active kept versions are slimmed (results and oos)",
      "trades_detail" not in vers_by_v[1]["results"]
      and "equity_curve" not in vers_by_v[4]["results"]
      and "trades_detail" not in vers_by_v[5]["results"]
      and "trades_detail" not in vers_by_v[5]["results"]["oos"])
check("positive net survives with its oos sub-dict intact otherwise",
      vers_by_v[5]["results"]["oos"].get("trades") == 5)

# An entry whose active pointer would be orphaned must raise, not ship.
broken = {"s": {"symbols": {"X": {"active": 99, "versions": [
    {"v": 2, "params": {}, "reason": "tightening",
     "results": {"trades": 1, "net_pnl": -1}}]}}}}
try:
    cv.clean(broken)
    check("orphaned active pointer raises", False, "no exception")
except AssertionError:
    check("orphaned active pointer raises", True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all version-hygiene checks passed")
