#!/usr/bin/env python3
"""test_golden_replay.py — characterisation ("golden file") test.

Per ltp-monitor-claude-code-brief.md Kickoff item 2: capture CURRENT
behaviour as an executable snapshot BEFORE changing anything, so any
future refactor must reproduce it exactly or explain the diff.

The brief asks for a raw-tick fixture. This system has no raw tick store;
it has `chain_snapshots` — real archived 60 s option-chain frames with
per-strike OI, volume and greeks. Replaying those gives the same
guarantee from data that already exists (see docs/BRIEF-RECONCILIATION.md
§6).

WHAT IS PINNED, and why each one matters for reproducibility:
  * frames come from a FIXED (symbol, day, expiry) in history.db
  * `as_of=day` — analyze() derives days-to-expiry from this; without it
    the golden file would change every calendar day (this exact bug was
    v59.53)
  * a FROZEN config subset — lot sizes and thresholds are inputs to the
    per-strike view, so a Settings change must not silently rewrite the
    baseline
  * floats rounded to 4 dp before hashing — identical arithmetic, but
    immune to repr wobble across Python patch versions

USAGE
    ./venv/bin/python3 test_golden_replay.py            # verify vs golden
    ./venv/bin/python3 test_golden_replay.py --bless    # re-record it

`--bless` is the ONLY way the baseline moves, and it prints a diff summary
first so a re-record is a deliberate act, never a silent one.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyzer
import config
import history
import strategies

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "tests", "golden", "chain_replay.json")
MAX_FRAMES = 60

# Frozen inputs. Anything analyze()/evaluate() reads from config must be
# pinned here, or a Settings edit would move the baseline.
FROZEN_CFG = {
    "lot_sizes": {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "SENSEX": 20},
    "spread_profit_target_pct": 18,
    "spread_defense_zone_pct": 30,
    "spread_require_liquidity_confluence": False,
    "min_edge_cost_ratio": 2.0,
    "opt_halfspread_points": 0.5,
    "opt_brokerage_per_order": 20.0,
    "opt_stt_sell_pct": 0.001,
    "opt_exchange_txn_pct": 0.0005,
    "opt_sebi_turnover_pct": 0.000001,
    "opt_stamp_duty_pct": 0.00003,
    "opt_gst_pct": 0.18,
    "fee_per_lot": 40,
}
FROZEN_PARAMS = {"wall_gap_frac": 2.0, "credit_min_frac": 0.28,
                 "profit_capture": 0.25, "loss_mult": 1.0}

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def _round(o, nd=4):
    """Recursively round floats — identical maths, no repr wobble."""
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round(v, nd) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_round(v, nd) for v in o]
    return o


def _pick_source():
    """A FIXED (symbol, day, expiry). Deliberately the OLDEST archived day
    with enough frames: the newest day changes as the archive grows, which
    would make the golden file self-invalidating."""
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        for day in sorted(history.chain_days(sym) or []):
            exp = history.front_expiry_on(sym, day)
            frames = list(history.day_chain_frames(sym, day, expiry=exp))
            if len(frames) >= 20:
                return sym, day, exp, frames[:MAX_FRAMES]
    return None, None, None, []


def build_snapshot():
    sym, day, expiry, frames = _pick_source()
    if not frames:
        return None
    cfg = {**config.load(), **FROZEN_CFG}
    out = {"source": {"symbol": sym, "day": day, "expiry": expiry,
                      "frames": len(frames)},
           "frames": []}
    _orig_load = config.load
    config.load = lambda: cfg          # freeze for evaluate()'s internals
    try:
        for ts, chain in frames:
            an = analyzer.analyze(chain, as_of=day)
            rec = {
                "ts": ts,
                "spot": an.get("spot"),
                "error": an.get("error"),
                "pcr": an.get("pcr"),
                "max_pain": an.get("max_pain"),
                "atr_pct": an.get("atr_pct"),
                "n_strikes": len(an.get("strikes") or []),
                # The OI quadrant classification is the single most
                # reused derived value in the system (classify_leg is its
                # one definition) — snapshot it per strike.
                "quadrants": [
                    {"strike": s.get("strike"),
                     "ce": (s.get("ce") or {}).get("quadrant"),
                     "pe": (s.get("pe") or {}).get("quadrant")}
                    for s in (an.get("strikes") or [])
                ],
                "signal_lines": an.get("signal_lines"),
                "spreads": {},
            }
            for name in ("bull_put_spread", "bear_call_spread"):
                r = strategies.evaluate(name, an, {"regime": "rangebound"},
                                        params=FROZEN_PARAMS)
                if r is None:
                    rec["spreads"][name] = None
                else:
                    rec["spreads"][name] = {
                        "eligible": r.get("eligible"),
                        "credit": r.get("credit"),
                        "max_loss": r.get("max_loss"),
                        "short_strike": r.get("short_strike"),
                        "width": r.get("width"),
                        "reasons": r.get("reasons"),
                    }
            out["frames"].append(_round(rec))
    finally:
        config.load = _orig_load
    return out


def digest(snap):
    return hashlib.sha256(
        json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main():
    bless = "--bless" in sys.argv
    snap = build_snapshot()
    if snap is None:
        print("no archived chain frames available — cannot build a golden "
              "file. This is a SKIP, not a pass.")
        return 0

    if not os.path.exists(GOLDEN):
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w") as f:
            json.dump(snap, f, sort_keys=True, indent=1)
        print(f"RECORDED golden file: {GOLDEN}\n"
              f"  source: {snap['source']}\n  sha256: {digest(snap)[:16]}…")
        return 0

    with open(GOLDEN) as f:
        gold = json.load(f)

    check("golden file replays the SAME source frames",
          gold.get("source") == snap.get("source"),
          f"{gold.get('source')} vs {snap.get('source')}")

    same = digest(gold) == digest(snap)
    if not same and not bless:
        # Point at the first divergence — "it differs" is not a useful
        # failure message when the file has 60 frames.
        gf, sf = gold.get("frames", []), snap.get("frames", [])
        where = "frame count differs" if len(gf) != len(sf) else None
        if where is None:
            for i, (a, b) in enumerate(zip(gf, sf)):
                if a != b:
                    keys = sorted({k for k in set(a) | set(b)
                                   if a.get(k) != b.get(k)})
                    where = f"frame #{i} (ts={a.get('ts')}) fields: {keys}"
                    break
        print(f"\n  FIRST DIVERGENCE: {where}")
        print(f"  golden sha256 {digest(gold)[:16]}…  "
              f"current sha256 {digest(snap)[:16]}…")
        print("  If this change is INTENDED, re-record deliberately:\n"
              "      ./venv/bin/python3 test_golden_replay.py --bless")
    check("current behaviour reproduces the golden file bit-for-bit", same,
          f"{len(snap['frames'])} frames, sha {digest(snap)[:12]}…")

    if bless and not same:
        with open(GOLDEN, "w") as f:
            json.dump(snap, f, sort_keys=True, indent=1)
        print(f"\n  RE-RECORDED golden file → sha {digest(snap)[:16]}…")
        return 0

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("golden replay matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
