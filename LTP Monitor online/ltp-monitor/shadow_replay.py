#!/usr/bin/env python3
"""shadow_replay.py — re-resolve rejected signals by FIRST TOUCH against
the archived chain, instead of by live sampling.

WHY THE LIVE RESOLVER IS NOT ENOUGH (roadmap B2).
`_resolve_shadow_signals` answers "was the risk agent right to reject
that?" by polling the LIVE chain on the agent's cadence and asking
whether the premium is past target or past stop AT THAT MOMENT, giving
up after 90 minutes. Two consequences, measured on the record:

  1. CENSORING, and not at random. 314 of 653 rejected option signals
     (48%) never resolved. A signal resolves only if price reached a
     level DECISIVELY inside 90 minutes, so the resolved subset is
     selected for movers and the timeouts are selected for drift. Any
     win rate computed on the resolved half describes a different
     population from the one the gates actually rejected.
  2. SAMPLING, not first touch. A premium that spikes through target and
     retraces before the next poll records nothing, or later records the
     STOP it drifted into. The error runs in both directions, so it
     cannot be corrected by a sign — only by replaying the path.

`chain_snapshots` fixes both for recent signals: per-strike LTP every
60s, five days deep, which is the retention the archive work bought.

WHAT THIS STILL CANNOT DO. 60s snapshots are a sampled path, not ticks —
an excursion inside a minute is invisible, so every touch here is a
LOWER bound on how early a level was reached. That understates both
targets and stops, so the COMPARISON between them survives while the
absolute rates stay conservative. The same caveat the target-geometry
work carried, for the same reason.

FIRST-TOUCH CONVENTION, unchanged from the v59.0 futures grid: walk
forward in time, whichever level is crossed first decides the outcome,
and a snapshot that is past BOTH levels is charged as a STOP. Charging
the ambiguous case against the trade is the conservative choice and
keeps this comparable with the earlier work.

Read-only. Changes no live behaviour and rewrites no journal.
"""
import argparse
import collections
import datetime
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history
import store

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def load_signals():
    p = store.path("shadow_signals.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        # 'kind' marks the futures journal, a different schema.
        if r.get("verdict") == "REJECTED" and "kind" not in r:
            out.append(r)
    return out


def series(conn, symbol, strike, leg, t0, t1):
    rows = conn.execute(
        "SELECT ts,ltp,bid FROM chain_snapshots WHERE symbol=? AND strike=? "
        "AND leg=? AND ts>=? AND ts<=? ORDER BY ts",
        (symbol, strike, leg, t0, t1)).fetchall()
    # 2026-08-17 — in-session frames only. A first-touch replay that can
    # "touch" a stop on a 20:00 quote resolves counterfactuals against
    # prices no order could have filled at.
    import agents
    return [(t, l, b) for t, l, b in rows
            if l and agents.in_market_session(int(t))]


def first_touch(path, entry, stop, target):
    """(outcome, R, minutes) by first touch. Spanning snapshot = stop."""
    # Levels must be ordered stop < entry < target for R to mean anything.
    # 22 rejected signals on disk have target1 <= entry (one has target
    # BELOW its own stop: entry 345.4, stop 297.9, target 254.1 — a
    # malformed pre-repair signal). Without this guard the first snapshot
    # satisfies `ltp >= target` and the trade is labelled "target", which
    # the caller counts as a WIN even though R is negative. Caught by
    # test_shadow_replay; it inflated the first headline win rate.
    if not path or entry <= stop or target <= entry:
        return None, None, None
    risk = entry - stop
    t0 = path[0][0]
    for ts, ltp, _bid in path:
        hit_t = ltp >= target
        hit_s = ltp <= stop
        if hit_t and hit_s:                      # cannot happen for a
            return "stop", -1.0, (ts - t0) / 60  # long, but keep the rule
        if hit_s:
            return "stop", -1.0, (ts - t0) / 60
        if hit_t:
            return "target", (target - entry) / risk, (ts - t0) / 60
    last = path[-1][1]
    return "open", (last - entry) / risk, (path[-1][0] - t0) / 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=0,
                    help="minutes to allow; 0 = to the end of the archive day")
    a = ap.parse_args()

    conn = history._conn()
    try:
        mn, mx = conn.execute(
            "SELECT MIN(ts),MAX(ts) FROM chain_snapshots").fetchone()
        if not mn:
            sys.exit("chain_snapshots is empty — nothing to replay")
        sigs = load_signals()
        inwin, rows = [], []
        for s in sigs:
            try:
                ts = datetime.datetime.fromisoformat(s["ts"]).timestamp()
            except Exception:
                continue
            if not (mn <= ts <= mx):
                continue
            inwin.append(s)
            leg = "ce" if "CE" in (s.get("signal") or "") else "pe"
            end = ts + a.horizon * 60 if a.horizon else \
                (int(ts) - int(ts) % 86400) + 86400
            path = series(conn, s["symbol"], s.get("strike"), leg,
                          int(ts), int(min(end, mx)))
            if not path:
                continue
            oc, R, mins = first_touch(path, s.get("entry"), s.get("stoploss"),
                                      s.get("target1"))
            if oc is None:
                continue
            rows.append({"sig": s, "outcome": oc, "R": R, "mins": mins,
                         "n": len(path),
                         "live": s.get("resolution"),
                         "src": s.get("source") or "(unattributed)"})
    finally:
        conn.close()

    print(f"\n  archive {datetime.datetime.fromtimestamp(mn, IST):%m-%d %H:%M}"
          f" -> {datetime.datetime.fromtimestamp(mx, IST):%m-%d %H:%M}")
    print(f"  rejected signals in window: {len(inwin)}   replayed: {len(rows)}")
    if not rows:
        sys.exit("  no rejected signal had an archived strike series")

    oc = collections.Counter(r["outcome"] for r in rows)
    print(f"\n  FIRST-TOUCH outcome: " + "  ".join(
        f"{k}={v}" for k, v in oc.most_common()))
    closed = [r for r in rows if r["outcome"] in ("target", "stop")]
    if closed:
        tot = sum(r["R"] for r in closed)
        wins = sum(1 for r in closed if r["outcome"] == "target")
        print(f"  decided {len(closed)}: {wins} target / {len(closed)-wins} stop"
              f"  = {100*wins/len(closed):.1f}% win")
        print(f"  net {tot:+.1f}R over {len(closed)} = {tot/len(closed):+.3f}R "
              f"per rejected signal, BEFORE costs")
    openr = [r for r in rows if r["outcome"] == "open"]
    if openr:
        m = st.median([r["R"] for r in openr])
        print(f"  still open at horizon: {len(openr)}, median mark {m:+.2f}R "
              f"(they are NOT free — an open trade pays costs and holds risk)")

    print(f"\n  {'strategy':24} {'n':>4} {'win%':>6} {'R/signal':>10}")
    for s in sorted({r["src"] for r in rows}):
        g = [r for r in rows if r["src"] == s and r["outcome"] in ("target", "stop")]
        if len(g) < 5:
            print(f"  {s[:24]:24} {len(g):>4} {'thin':>6}")
            continue
        w = sum(1 for r in g if r["outcome"] == "target")
        print(f"  {s[:24]:24} {len(g):>4} {100*w/len(g):>5.1f}% "
              f"{sum(r['R'] for r in g)/len(g):>+9.3f}R")

    # The point of the exercise: does first touch agree with live polling?
    both = [r for r in rows if r["live"] in ("would_have_hit_target1",
                                             "would_have_hit_stoploss")]
    if both:
        agree = sum(1 for r in both
                    if (r["live"] == "would_have_hit_target1") ==
                       (r["outcome"] == "target"))
        print(f"\n  vs the LIVE resolver, where it reached a verdict "
              f"(n={len(both)}): agrees {agree} ({100*agree/len(both):.0f}%)")
    rescued = [r for r in rows if r["live"] in ("unresolved_timeout", "pending")
               and r["outcome"] in ("target", "stop")]
    if rescued:
        w = sum(1 for r in rescued if r["outcome"] == "target")
        net = sum(r["R"] for r in rescued)
        print(f"  signals the live resolver CENSORED but the archive decides: "
              f"{len(rescued)} — {w} target / {len(rescued)-w} stop, "
              f"{net/len(rescued):+.3f}R each")
        print("  if that differs from the resolved subset, the live record was "
              "biased, which is the whole reason for this script")


if __name__ == "__main__":
    main()
