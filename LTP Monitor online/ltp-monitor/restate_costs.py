#!/usr/bin/env python3
"""restate_costs.py — one-shot cost restatement of trades.jsonl (v59.68).

Two defects found by the 2026-08-09 third-eye review put wrong costs on
persisted paper trades:

  FUTURES — agents._cost_parts called futures_costs.cost_round_trip with
  a (premium_in, premium_out, lot) positional mismatch; the AttributeError
  was swallowed and every futures round trip fell back to the flat
  fee_per_lot model (₹120 on a 2-lot BANKNIFTY trip the notional model
  prices at ~₹1,200). The 2026-08-06 restatement ("fees_restated") also
  used the flat model, so all 59 futures trades still carry it, and none
  carry any slippage at all.

  SPREADS — the live exit passed pnl_per_share as the exit premium, so a
  losing spread's sell notional went NEGATIVE and STT became a rebate
  (₹224 charged where the corrected model says ₹280 on a credit-150
  loser). All 194 spread trades are recomputed with the exit VALUE
  (agents.spread_exit_value), the single definition the live path and
  the backtester now share.

Single-leg option trades already went through the notional model with
correct arguments and are untouched.

Period-appropriate contract sizes: each futures record's own `lot_size`
and each spread's own `qty/lots` are used — NOT today's config map —
because lot sizes were revised on 2026-08-01 and a backtest/restatement
must use the size in force when the trade happened
(india-market-mechanics: historical replays with today's constant are
wrong on one side of the revision).

Each restated row keeps its previous numbers under `restated_v5968_from`
and is stamped `cost_model` so the correction is auditable. gross_pnl,
mfe and mae are untouched (they are cost-free quantities);
pnl = gross − fees − slippage is recomputed.

Safety: timestamped backup, atomic replace, --dry-run. The running app
holds `closed_trades` in memory — restart it after a real run so the
dashboards reload the restated file.

Usage:  python3 restate_costs.py [--dry-run]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
import config
import agents
import futures_costs
import options_costs

PATH = store.path("trades.jsonl")
STAMP_MODEL_FUT = "notional-restated-v59.68"
STAMP_MODEL_SPR = "notional-exitvalue-restated-v59.68"


def _is_future(t):
    return t.get("side") in ("LONG", "SHORT") and t.get("leg") is None


def restate(trades, cfg=None):
    """Pure transform: (new list, stats). No I/O — testable."""
    cfg = cfg if cfg is not None else config.load()
    out, stats = [], {"futures": 0, "spreads": 0, "skipped": 0,
                      "fees_delta": 0.0, "slip_delta": 0.0, "pnl_delta": 0.0}
    for t in trades:
        t = dict(t)
        try:
            if _is_future(t) and t.get("lot_size") and t.get("lots") \
                    and t.get("entry") and t.get("ltp"):
                b = futures_costs.breakdown(
                    t.get("symbol"), float(t["entry"]), float(t["ltp"]),
                    lots=1, cfg=cfg, lot=int(t["lot_size"]))
                lots = max(1, int(t["lots"]))
                fees = round(b["statutory_rupees"] * lots, 0)
                slip = round(b["items"]["slippage"] * lots, 0)
                stats["futures"] += 1
                model = STAMP_MODEL_FUT
            elif t.get("leg") == "SPREAD" and t.get("qty") and t.get("lots") \
                    and t.get("entry") is not None and t.get("ltp") is not None:
                credit = float(t["entry"])
                pnl_ps = float(t["ltp"])          # spread records store
                                                  # pnl_per_share in `ltp`
                lots = max(1, int(t["lots"]))
                per_lot = int(round(float(t["qty"]) / lots))
                r = options_costs.cost_round_trip(
                    credit, agents.spread_exit_value(credit, pnl_ps),
                    per_lot, legs=2, cfg=cfg)
                fees = round(float(r.get("statutory", 0)) * lots, 0)
                slip = round(float(r.get("spread", 0)) * lots, 0)
                stats["spreads"] += 1
                model = STAMP_MODEL_SPR
            else:
                stats["skipped"] += 1
                out.append(t)
                continue
        except Exception as e:
            # A row that cannot be recomputed keeps its numbers — a
            # restatement must never invent values, and the failure is
            # visible in the stats rather than swallowed.
            t["restate_v5968_error"] = f"{type(e).__name__}: {e}"
            stats["skipped"] += 1
            out.append(t)
            continue
        gross = float(t.get("gross_pnl") or 0)
        old_fees = float(t.get("fees") or 0)
        old_slip = float(t.get("slippage") or 0)
        old_pnl = t.get("pnl")
        new_pnl = round(gross - fees - slip, 0)
        t["restated_v5968_from"] = {"fees": t.get("fees"),
                                    "slippage": t.get("slippage"),
                                    "pnl": t.get("pnl"),
                                    "cost_model": t.get("cost_model")}
        t["fees"], t["slippage"], t["pnl"] = fees, slip, new_pnl
        t["cost_model"] = model
        stats["fees_delta"] += fees - old_fees
        stats["slip_delta"] += slip - old_slip
        stats["pnl_delta"] += new_pnl - float(old_pnl or 0)
        out.append(t)
    return out, stats


def main():
    dry = "--dry-run" in sys.argv
    if not os.path.exists(PATH):
        print(f"nothing to restate — {PATH} does not exist")
        return
    trades = [json.loads(l) for l in open(PATH) if l.strip()]
    new, stats = restate(trades)
    print(f"futures restated : {stats['futures']}")
    print(f"spreads restated : {stats['spreads']}")
    print(f"left untouched   : {stats['skipped']} (single-leg options + "
          f"unrecomputable rows)")
    print(f"fees   Δ         : ₹{stats['fees_delta']:+,.0f}")
    print(f"slippage Δ       : ₹{stats['slip_delta']:+,.0f}")
    print(f"net P&L Δ        : ₹{stats['pnl_delta']:+,.0f}  "
          f"(negative = record was too flattering)")
    if dry:
        print("\n--dry-run: nothing written")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = PATH.replace(".jsonl", f".pre-notional-restate-{stamp}.jsonl")
    os.replace(PATH, backup)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        for t in new:
            f.write(json.dumps(t, default=str) + "\n")
    os.replace(tmp, PATH)
    print(f"\nbackup : {backup}")
    print(f"written: {PATH}")
    print("NOTE: the running app still holds the OLD numbers in memory — "
          "restart it so dashboards and the learning agents reload the "
          "restated record.")


if __name__ == "__main__":
    main()
