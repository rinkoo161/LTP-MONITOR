#!/usr/bin/env python3
"""Measure what the day_chain_frames/analyze fix does to replay output.

These replays feed is_live_enabled(). Run BEFORE and AFTER the change
and diff — the point is to show the operator how far the numbers move,
not to assert they should not move.
"""
import json, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtester as bt

out = {}
for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
    for name in ("bull_put_spread", "bear_call_spread"):
        try:
            r = bt.replay_spreads(sym, name)
            tr = r.get("trades", r) if isinstance(r, dict) else r
            n = len(tr)
            pnl = round(sum((t.get("pnl") or 0) for t in tr), 0)
            wins = sum(1 for t in tr if (t.get("pnl") or 0) > 0)
            out[f"spreads:{sym}:{name}"] = {"n": n, "pnl": pnl, "wins": wins}
        except Exception as e:
            out[f"spreads:{sym}:{name}"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        r = bt.replay_momentum(sym)
        tr = r.get("trades", r) if isinstance(r, dict) else r
        out[f"momentum:{sym}"] = {"n": len(tr),
                                  "pnl": round(sum((t.get("pnl") or 0) for t in tr), 0),
                                  "wins": sum(1 for t in tr if (t.get("pnl") or 0) > 0)}
    except Exception as e:
        out[f"momentum:{sym}"] = {"error": f"{type(e).__name__}: {e}"}
try:
    r = bt.replay_portfolio()
    tr = r.get("trades", r) if isinstance(r, dict) else r
    out["portfolio"] = {"n": len(tr),
                        "pnl": round(sum((t.get("pnl") or 0) for t in tr), 0),
                        "wins": sum(1 for t in tr if (t.get("pnl") or 0) > 0)}
except Exception as e:
    out["portfolio"] = {"error": f"{type(e).__name__}: {e}"}

json.dump(out, open(sys.argv[1], "w"), indent=1, sort_keys=True)
for k in sorted(out):
    print(f"  {k:34} {out[k]}")
