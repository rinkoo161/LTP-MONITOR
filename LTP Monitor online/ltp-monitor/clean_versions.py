#!/usr/bin/env python3
"""clean_versions.py — one-shot pruning of strategy_versions.json.

v59.67, operator request (2026-08-09): the tuners and the on-demand
sweep minted a version for ANY meaningful movement, including a
15%-smaller loss — 67 of the 85 versions on the live install carried
non-positive results and were pure dead weight, parsed by every
get_params() call (the file had grown to 4 MB). version_worthy() stops
new ones being created; this script removes the ones already there.

KEEP rules, per (strategy, symbol) entry — a version survives if ANY of:
  * it is the ACTIVE version (its params are in force; removing it would
    orphan the `active` pointer),
  * it is v1 "initial" (the shipped-defaults baseline — the rollback
    target the UI offers),
  * it was ever deployed (`deployed: true`),
  * its recorded results are positive (net_pnl > 0 with trades > 0) —
    evidence of a configuration that worked, worth keeping for rollback.
Everything else — the negative "tightening/relaxing" churn — is removed.

SLIM rule: kept versions that are NOT active have `trades_detail` and
`equity_curve` stripped from their results (and their `oos` sub-dict).
Those fields exist for the Backtest-page chart overlay, which reads the
active/fresh results — on historical versions they are the bulk of the
4 MB.

Safety: refuses to run while app.py is running unless --force (the
BacktestAgent writes this file at 15:45 and on Optimize clicks); always
writes a timestamped backup next to the original; replaces the file
atomically (os.replace).

Usage:  python3 clean_versions.py [--dry-run] [--force]
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

PATH = store.path("strategy_versions.json")
HEAVY = ("trades_detail", "equity_curve")


def _keep(ver, active):
    if ver.get("v") == active:
        return "active"
    if ver.get("v") == 1:
        return "initial baseline"
    if ver.get("deployed"):
        return "was deployed"
    r = ver.get("results") or {}
    if (r.get("trades") or 0) > 0 and (r.get("net_pnl") or 0) > 0:
        return "positive results"
    return None


def _slim(ver):
    """Strip chart-overlay bulk from a non-active version, in place."""
    r = ver.get("results")
    removed = 0
    if isinstance(r, dict):
        for k in HEAVY:
            removed += 1 if r.pop(k, None) is not None else 0
        oos = r.get("oos")
        if isinstance(oos, dict):
            for k in HEAVY:
                removed += 1 if oos.pop(k, None) is not None else 0
    return removed


def clean(v):
    """Pure transform: (cleaned copy, stats). Does no I/O — testable."""
    out = json.loads(json.dumps(v))          # deep copy, JSON-safe by construction
    stats = {"entries": 0, "versions_before": 0, "versions_after": 0,
             "removed": [], "slimmed": 0}
    for name, node in out.items():
        for sym, entry in (node.get("symbols") or {}).items():
            stats["entries"] += 1
            vers = entry.get("versions") or []
            stats["versions_before"] += len(vers)
            active = entry.get("active")
            kept = []
            for ver in vers:
                why = _keep(ver, active)
                if why is None:
                    r = ver.get("results") or {}
                    stats["removed"].append(
                        f"{name}/{sym} v{ver.get('v')} "
                        f"(net ₹{r.get('net_pnl', '?')}, "
                        f"{(ver.get('reason') or '')[:48]})")
                    continue
                if ver.get("v") != active:
                    stats["slimmed"] += _slim(ver)
                kept.append(ver)
            # The active pointer must survive by construction; assert
            # rather than silently shipping an orphaned entry.
            if vers and not any(k.get("v") == active for k in kept):
                raise AssertionError(
                    f"{name}/{sym}: active v{active} not in kept set")
            entry["versions"] = kept
            stats["versions_after"] += len(kept)
    return out, stats


def main():
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    try:
        running = subprocess.run(
            ["pgrep", "-f", r"python.*app\.py"],
            capture_output=True).returncode == 0
    except Exception:
        running = False
    if running and not force and not dry:
        print("app.py is RUNNING — it writes this file at 15:45 and on "
              "Optimize clicks. Stop it first, or re-run with --force "
              "(safe outside those windows; a backup is written either way).")
        sys.exit(2)
    if not os.path.exists(PATH):
        print(f"nothing to clean — {PATH} does not exist")
        return
    v = json.load(open(PATH))
    before_bytes = os.path.getsize(PATH)
    cleaned, stats = clean(v)
    print(f"entries: {stats['entries']}  versions: "
          f"{stats['versions_before']} -> {stats['versions_after']} "
          f"(removing {len(stats['removed'])}, slimmed {stats['slimmed']} "
          f"heavy field(s))")
    for line in stats["removed"]:
        print(f"  - {line}")
    if dry:
        print("\n--dry-run: nothing written")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = PATH.replace(".json", f".pre-clean-{stamp}.json")
    with open(backup, "w") as f:
        json.dump(v, f, indent=1)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cleaned, f, indent=1)
    os.replace(tmp, PATH)
    after_bytes = os.path.getsize(PATH)
    print(f"\nbackup : {backup}")
    print(f"written: {PATH}  {before_bytes:,} -> {after_bytes:,} bytes")


if __name__ == "__main__":
    main()
