#!/usr/bin/env python3
"""futures_postmortem.py — why the futures engine was switched off.

    python3 futures_postmortem.py                # the 40-trade cohort
    python3 futures_postmortem.py --all          # every futures trade
    python3 futures_postmortem.py --json         # machine-readable

v59.0 Phase 0. `futures_strategy_enabled` went True -> False on
2026-07-27 after "every futures trade closing at a loss or exact
breakeven — all via forced kill-switch closure, none via their own
profit target", and agents.py records the cohort as 40 trades, 27.5%
win, -Rs 23,863.

That signature is the reason this is a post-mortem and not a build. A
strategy that loses money has a bad edge. A strategy where NOTHING
reaches its own target has an exit-geometry or interference defect, and
those need opposite fixes. This script decides which, by testing the
five hypotheses in the spec against the journal:

  H1 target unreachable   MFE distribution in ATR units vs the 2.75 target
  H2 defence pre-empts    trades that tripped the defence zone, and their MFE after
  H3 rupee cap distorts   implied stop from the Rs 2,500 cap vs 1.5 x ATR
  H4 kill-switch label    what the exit reason ACTUALLY says (resolve first)
  H5 costs                P&L recomputed under the notional cost model

Reads only. Produces no trading decision and touches no live path.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
import store

COHORT_END = "2026-07-29"          # the 40 trades the disable decision used


def load(all_trades=False):
    p = store.path("trades.jsonl")
    rows = [json.loads(l) for l in open(p) if l.strip()]
    fut = [t for t in rows if t.get("kind") == "future"]
    if not all_trades:
        fut = [t for t in fut if str(t.get("closed_date", "")) <= COHORT_END]
    return fut


def pnl(t):
    return float(t.get("pnl") or 0)


def qty(t):
    r = agents.trade_risk_fields(t)
    return r.get("qty") or 0


def points(t, rupees):
    q = qty(t)
    return (rupees / q) if q else None


def implied_atr(t, cfg):
    """ATR at entry, derived from the target the engine actually set.

    `atr_at_entry` was only added later — 0 of the 40 cohort trades carry
    it, 16 of the 19 later ones do. But the engine sets
    target = entry +/- futures_atr_target_mult * ATR, so the distance it
    chose reveals the ATR it used. Marked derived wherever it is.
    """
    stored = t.get("atr_at_entry")
    if stored:
        return float(stored), False
    mult = cfg.get("futures_atr_target_mult", 2.75)
    try:
        d = abs(float(t["target"]) - float(t["entry"]))
    except Exception:
        return None, False
    return (d / mult, True) if d and mult else (None, False)


def target_distance(t):
    """Points from entry to the trade's own target."""
    try:
        return abs(float(t["target"]) - float(t["entry"]))
    except Exception:
        return None


def atr_units(t, rupees, cfg):
    a, _ = implied_atr(t, cfg)
    pts = points(t, rupees)
    if not a or pts is None:
        return None
    return pts / a


def classify_exit(reason):
    """H4 — the label is the whole question. `portfolio kill-switch` and
    an ordinary end-of-day square-off are completely different findings,
    and the docstring that triggered this project treats them as one."""
    r = str(reason or "").lower()
    if "kill-switch" in r or "kill switch" in r:
        return "portfolio kill-switch"
    if "square-off" in r or "squareoff" in r or "market closing" in r or "eod" in r:
        return "EOD square-off"
    if "target" in r:
        return "target"
    if "stoploss" in r or "stop loss" in r:
        return "stoploss"
    if "profit floor" in r or "profit lock" in r:
        return "profit floor"
    if "defense" in r or "defence" in r:
        return "defence tighten"
    if "ai " in r or "advisory" in r or "auto-exit" in r:
        return "AI auto-exit"
    if "transaction stop" in r:
        return "transaction rupee stop"
    if "manual" in r:
        return "manual"
    if "time stop" in r:
        return "time stop"
    return f"other: {str(reason)[:40]}"


def report(trades, as_json=False):
    cfg = __import__("config").load()
    tgt_mult = cfg.get("futures_atr_target_mult", 2.75)
    stop_mult = cfg.get("futures_atr_stop_mult", 1.5)
    cap = cfg.get("futures_risk_per_trade_rupees", 2500)

    out = {"n": len(trades), "net": sum(pnl(t) for t in trades)}
    wins = [t for t in trades if pnl(t) > 0]
    flats = [t for t in trades if pnl(t) == 0]
    out["win_rate"] = 100 * len(wins) / len(trades) if trades else 0
    out["breakeven"] = len(flats)

    print("=" * 74)
    print(f"  FUTURES POST-MORTEM — {len(trades)} trades, net ₹{out['net']:,.0f}, "
          f"win {out['win_rate']:.1f}%, {len(flats)} exact breakeven")
    print("=" * 74)

    # ---------------- H4 first: what actually closed these trades ----------
    print("\nH4 — EXIT REASON (resolve first: is 'kill-switch' an overloaded label?)")
    hist = collections.Counter(classify_exit(t.get("reason")) for t in trades)
    for k, n in hist.most_common():
        share = 100 * n / len(trades)
        pl = sum(pnl(t) for t in trades if classify_exit(t.get("reason")) == k)
        print(f"    {k:26} {n:3}  ({share:4.1f}%)   ₹{pl:>9,.0f}")
    out["exit_reasons"] = dict(hist)
    out["target_exits"] = hist.get("target", 0)
    print(f"\n    target exits: {hist.get('target', 0)} of {len(trades)} "
          f"({100*hist.get('target',0)/max(len(trades),1):.1f}%)"
          f"   [v59 Phase A gate requires >= 15%]")

    # ---------------- H1: is the target reachable at all -------------------
    print(f"\nH1 — DID PRICE EVER REACH THE TARGET? (MFE vs the target, in points)")
    rows_h1 = []
    for t in trades:
        td = target_distance(t)
        mfe_pts = points(t, float(t.get("mfe") or 0))
        if td and mfe_pts is not None:
            rows_h1.append((t, td, mfe_pts, mfe_pts / td))
    print(f"    trades with target + qty to compare: {len(rows_h1)}/{len(trades)}")
    if rows_h1:
        fracs = sorted(f for _, _, _, f in rows_h1)
        def pc(p):
            return fracs[min(len(fracs) - 1, int(len(fracs) * p))]
        print(f"    MFE as a FRACTION of the target distance —")
        print(f"      p50 {pc(.5):.3f}   p75 {pc(.75):.3f}   p90 {pc(.9):.3f}   max {fracs[-1]:.3f}")
        for thresh, label in ((1.0, "the full target"), (0.5, "half the target"),
                              (0.25, "a quarter of it")):
            n = sum(1 for f in fracs if f >= thresh)
            print(f"      reached {label:18}: {n:3}/{len(fracs)} ({100*n/len(fracs):.1f}%)")
        tds = sorted(td for _, td, _, _ in rows_h1)
        print(f"    target distance itself — median {tds[len(tds)//2]:.1f} pts, "
              f"range {tds[0]:.1f}-{tds[-1]:.1f}")
        out["mfe_frac_p50"] = pc(.5)
        out["reached_full_target"] = sum(1 for f in fracs if f >= 1.0)
        # Which geometry did the engine ACTUALLY use? enter_future() falls
        # back to fixed percentages whenever ATR is unavailable, silently.
        pct_mode = 0
        for t in trades:
            try:
                e = float(t["entry"])
                tp = abs(float(t["target"]) - e) / e * 100
                if abs(tp - cfg.get("futures_target_pct", 0.8)) < 0.01:
                    pct_mode += 1
            except Exception:
                pass
        print(f"\n    GEOMETRY ACTUALLY USED: {pct_mode}/{len(trades)} trades priced their")
        print(f"    target at exactly futures_target_pct "
              f"({cfg.get('futures_target_pct', 0.8)}% of entry), NOT {tgt_mult}x ATR.")
        if pct_mode == len(trades):
            print("    enter_future() falls back to fixed percentages whenever ATR is")
            print("    unavailable — so the ATR-adaptive geometry never engaged once.")
            print("    Deriving an ATR from these targets would be meaningless; not done.")
        out["pct_mode_trades"] = pct_mode

    # ---------------- H3: did the rupee cap distort the geometry -----------
    print(f"\nH3 — RUPEE CAP (₹{cap:,}) vs the intended {stop_mult}x ATR STOP")
    bound = 0
    checked = 0
    for t in trades:
        a = t.get("atr_at_entry")
        r = agents.trade_risk_fields(t)
        q = r.get("qty")
        if not a or not q:
            continue
        checked += 1
        intended_pts = stop_mult * a
        intended_rupees = intended_pts * q
        if intended_rupees > cap:
            bound += 1
    print(f"    trades where {stop_mult}x ATR risk exceeded the cap: {bound}/{checked}")
    print("    NOTE: in this codebase sizing.cap_by_rupee_risk reduces LOTS, it does")
    print("    not tighten the stop — so the cap changes size, not geometry. H3 as")
    print("    written does not apply to this implementation.")
    out["cap_bound"] = bound

    # ---------------- H2: defence zone -------------------------------------
    print("\nH2 — DEFENCE ZONE (did a tightened stop convert winners into breakevens?)")
    defended = [t for t in trades if t.get("defended")]
    print(f"    trades flagged `defended`: {len(defended)}/{len(trades)}")
    if defended:
        dm = [atr_units(t, float(t.get("mfe") or 0), cfg) for t in defended]
        dm = [x for x in dm if x is not None]
        if dm:
            print(f"    their MFE in ATR units — median {sorted(dm)[len(dm)//2]:.2f}, "
                  f"max {max(dm):.2f}")
        print(f"    their net: ₹{sum(pnl(t) for t in defended):,.0f}")
    out["defended"] = len(defended)

    # ---------------- H5: costs --------------------------------------------
    print("\nH5 — COSTS (flat fee_per_lot vs a notional-aware model)")
    try:
        import futures_costs
        flat = sum(float(t.get("fees") or 0) for t in trades)
        real = 0.0
        for t in trades:
            r = agents.trade_risk_fields(t)
            try:
                real += futures_costs.cost_round_trip(
                    t.get("symbol"), float(t.get("entry")), float(t.get("ltp")),
                    int(r.get("lots") or 1), cfg)
            except Exception:
                pass
        gross = sum(float(t.get("gross_pnl") or 0) for t in trades)
        print(f"    charged under fee_per_lot : ₹{flat:,.0f}")
        print(f"    true notional-model cost  : ₹{real:,.0f}   "
              f"({real/flat:.1f}x understated)" if flat else "")
        print(f"    gross P&L                 : ₹{gross:,.0f}")
        print(f"    net under the real model  : ₹{gross - real:,.0f}  "
              f"(reported: ₹{out['net']:,.0f})")
        out["cost_flat"], out["cost_real"] = flat, real
        out["net_real_costs"] = gross - real
    except ImportError:
        print("    futures_costs.py not built yet — run after §3.2")

    # ---------------- per-trade table --------------------------------------
    print("\nPER-TRADE (worst 12 by net)")
    print(f"    {'symbol':10} {'side':5} {'lots':>4} {'ATR':>6} {'stop×':>6} "
          f"{'MFE×':>6} {'MAE×':>6} {'net ₹':>9}  exit")
    for t in sorted(trades, key=pnl)[:12]:
        r = agents.trade_risk_fields(t)
        a = (implied_atr(t, cfg)[0] or 0)
        stopx = (abs(float(t["entry"]) - float(r["stop"])) / a) if (a and r.get("stop")) else 0
        print(f"    {str(t.get('symbol')):10} {str(t.get('side')):5} "
              f"{str(r.get('lots')):>4} {a:>6.1f} {stopx:>6.2f} "
              f"{(atr_units(t, float(t.get('mfe') or 0), cfg) or 0):>6.2f} "
              f"{(atr_units(t, float(t.get('mae') or 0), cfg) or 0):>6.2f} "
              f"{pnl(t):>9,.0f}  {classify_exit(t.get('reason'))}")

    if as_json:
        print("\nJSON\n" + json.dumps(out, indent=2, default=str))
    return out


def shadow_report():
    """§3.1 — which gate blocked most, from the existing shadow journal."""
    p = store.path("shadow_signals.jsonl")
    if not os.path.exists(p):
        print("\n  no shadow journal")
        return
    rows = []
    for l in open(p):
        l = l.strip()
        if not l:
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("kind") == "futures":
            rows.append(d)
    print(f"\nSHADOW JOURNAL — {len(rows)} futures signal evaluations")
    if not rows:
        return
    v = collections.Counter(str(r.get("verdict")) for r in rows)
    print("    verdicts:", dict(v))
    gates = collections.Counter()
    for r in rows:
        for g in (r.get("failed_gates") or []):
            gates[g] += 1
    if gates:
        print("    blocking gates:")
        for g, n in gates.most_common(8):
            print(f"      {g:28} {n}")
    else:
        print("    no failed_gates recorded on any entry")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every futures trade")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    trades = load(a.all)
    if not trades:
        sys.exit("no futures trades in the journal")
    report(trades, a.json)
    shadow_report()


if __name__ == "__main__":
    main()
