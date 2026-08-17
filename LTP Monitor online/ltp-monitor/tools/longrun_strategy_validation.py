#!/usr/bin/env python3
"""longrun_strategy_validation.py — every testable strategy over the full
free history (NIFTY 2017-04+, BANKNIFTY/FINNIFTY/SENSEX 2021-08+).

Runs the LIVE replay functions (backtester._replay_for) with the LIVE
tuned parameters (get_params, i.e. the active strategy_versions with
bounds-clamping) inside an ISOLATED store built from the research
backfill — production history.db is never opened. Set LTP_MONITOR_HOME
to that store before running; store.require_isolated enforces it.

WHAT THIS MEASURES — READ BEFORE QUOTING
----------------------------------------
* PA replays trade a SPOT PROXY with the replay's fee model; real option
  P&L (delta, IV, theta) is not modelled. Signal quality, not broker P&L.
* The v59.86 reachability gate and every chain-dependent check FAIL OPEN
  before 2026-07-29 (no option archive), so early years measure the
  strategies WITHOUT those gates — i.e. more permissively than live.
* Spreads / momentum_buy / S10 need the option-chain archive and are
  EXCLUDED, not approximated. 17 days of chain data cannot pretend to be
  nine years.
* One market regime per index per year; years are the only honest split.

Checkpoints one JSON per (strategy, symbol) so an interrupted run
resumes. Output: longrun_results.json in the store dir.
"""
import json
import os
import sys
import time
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store
store.require_isolated("longrun_strategy_validation")

import backtester as B
import history

STRATS = ["orb", "vwap_pullback", "ema_mtf", "sg_ema",
          "momentum_confluence", "ew_reversal", "ta_elliott"]
SYMS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
OUT = os.path.join(os.environ["LTP_MONITOR_HOME"], "longrun_results.json")


def main():
    results = {}
    if os.path.exists(OUT):
        results = json.load(open(OUT))
        print(f"resuming — {len(results)} cells done")
    for sym in SYMS:
        days = history.index_days(sym, limit=10000)
        byyear = collections.defaultdict(list)
        for d in days:
            byyear[d[:4]].append(d)
        print(f"{sym}: {len(days)} days, {days[0]}..{days[-1]}", flush=True)
        for name in STRATS:
            key = f"{name}:{sym}"
            if key in results:
                continue
            params = B.get_params(name, sym)
            t0 = time.time()
            cell = {"params": params, "years": {}}
            for yr in sorted(byyear):
                try:
                    tr = B._replay_for(name, sym, params, days=byyear[yr])
                except Exception as e:
                    cell["years"][yr] = {"error": f"{type(e).__name__}: {e}"[:120]}
                    continue
                pnls = [t.get("pnl", 0) or 0 for t in tr]
                wins = sum(1 for p in pnls if p > 0)
                cell["years"][yr] = {
                    "days": len(byyear[yr]), "trades": len(tr),
                    "net": round(sum(pnls)),
                    "win_pct": round(wins / len(pnls) * 100, 1) if pnls else None,
                    "worst": round(min(pnls)) if pnls else 0,
                    "best": round(max(pnls)) if pnls else 0,
                }
            cell["elapsed_s"] = round(time.time() - t0, 1)
            results[key] = cell
            json.dump(results, open(OUT, "w"), indent=1)
            tot = sum(y.get("net", 0) for y in cell["years"].values()
                      if isinstance(y, dict))
            ntr = sum(y.get("trades", 0) for y in cell["years"].values()
                      if isinstance(y, dict))
            print(f"  {key:<32} {ntr:>5} trades  net {tot:>10,}  "
                  f"({cell['elapsed_s']}s)", flush=True)
    print(f"\nDONE -> {OUT}")


if __name__ == "__main__":
    main()
