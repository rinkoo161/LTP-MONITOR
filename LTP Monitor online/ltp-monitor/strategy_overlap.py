#!/usr/bin/env python3
"""strategy_overlap.py — do two strategies fire on the same bars?

Roadmap C4 asks whether S4 (`ema_mtf`) and S7 (`sg_ema`) are the same
trade wearing two names. It sat "deferred pending Shadow Journal
evidence" until 2026-08-03, when the journal turned out to have carried
`source` since 27 July — the evidence was on disk the whole time.

METHOD, and the two things that make it more than eyeballing:

1. A RANDOM BASELINE, MULTI-DRAW. "5 of 7 signals had a partner within
   an hour" means nothing on its own: the comparison strategy fires 28
   times over six sessions across four symbols, so partners are easy to
   find by chance. Each observed count is compared against 400 draws in
   which the first strategy's timestamps are reshuffled uniformly inside
   the SAME session date and market hours, symbol held fixed. A single
   draw would be worthless — baseline spread is the whole question, and
   an earlier result in this project was wrong for exactly that reason.

2. THREE WINDOWS, READ FOR SHAPE. Testing 5/15/60 minutes is a mild
   multiple comparison, so the result is judged on the PATTERN rather
   than on any one z. Real coincidence is strongest at the SHORTEST lag
   and decays; a spurious hit is as likely to peak at 60 minutes as at
   5. The direction of the gradient is the check.

Read-only. Reports; changes nothing.
"""
import argparse
import datetime
import json
import os
import random
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

WINDOWS = (5, 15, 60)
DRAWS = 400
SESSION_MINUTES = 375          # 09:15 -> 15:30, the span signals fall in
SEED = 20260803


def load():
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
        if r.get("source") and "kind" not in r:
            out.append(r)
    return out


def when(r):
    return datetime.datetime.fromisoformat(r["ts"])


def partners(times, other, win):
    """How many of `times` have a same-symbol partner within `win` min."""
    n = 0
    for sym, ts in times:
        if any(o["symbol"] == sym
               and abs((when(o) - ts).total_seconds()) <= win * 60
               for o in other):
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="ema_mtf")
    ap.add_argument("--b", default="sg_ema")
    ap.add_argument("--draws", type=int, default=DRAWS)
    args = ap.parse_args()

    rows = load()
    A = [r for r in rows if r.get("source") == args.a]
    B = [r for r in rows if r.get("source") == args.b]
    if not A or not B:
        sys.exit(f"no signals for {args.a} ({len(A)}) or {args.b} ({len(B)})")

    print(f"\n  {args.a} n={len(A)}   {args.b} n={len(B)}")
    print(f"  {args.a} dates: {sorted({r['ts'][:10] for r in A})}")
    print(f"  {args.b} dates: {sorted({r['ts'][:10] for r in B})}")

    real = [(a["symbol"], when(a)) for a in A]
    rng = random.Random(SEED)
    print(f"\n  {'window':>7} {'observed':>9} {'random':>16} {'z':>7}")
    zs = {}
    for w in WINDOWS:
        obs = partners(real, B, w)
        got = []
        for _ in range(args.draws):
            fake = []
            for a in A:
                d = when(a)
                base = datetime.datetime(d.year, d.month, d.day, 9, 15,
                                         tzinfo=d.tzinfo)
                fake.append((a["symbol"], base + datetime.timedelta(
                    minutes=rng.randint(0, SESSION_MINUTES))))
            got.append(partners(fake, B, w))
        m = st.mean(got)
        s = st.pstdev(got) or 1e-9
        zs[w] = (obs - m) / s
        print(f"  {w:>5}m  {obs:>4}/{len(A):<4} {m:>8.2f} +/- {s:<5.2f} "
              f"{zs[w]:>+6.2f}")

    print("\n  SHAPE CHECK — real coincidence is strongest at the SHORTEST")
    print("  lag and decays. A spurious hit has no reason to.")
    ordered = all(zs[WINDOWS[i]] >= zs[WINDOWS[i + 1]]
                  for i in range(len(WINDOWS) - 1))
    print(f"  z decreasing with window: {'YES' if ordered else 'NO'} "
          f"({' > '.join(f'{zs[w]:+.2f}' for w in WINDOWS)})")

    print("\n  what the overlapping pairs actually looked like:")
    same_dir = tot = 0
    for a in A:
        near = sorted([o for o in B if o["symbol"] == a["symbol"]
                       and abs((when(o) - when(a)).total_seconds()) <= 60 * 60],
                      key=lambda o: abs((when(o) - when(a)).total_seconds()))
        if not near:
            continue
        o = near[0]
        tot += 1
        agree = o["signal"] == a["signal"]
        same_dir += agree
        lag = (when(o) - when(a)).total_seconds() / 60
        print(f"    {a['ts'][5:16]} {a['symbol']:9} {a['signal']:7} vs "
              f"{o['signal']:7} {lag:+5.0f}m  strike "
              f"{a.get('strike')} vs {o.get('strike')}"
              f"{'' if agree else '   <- DIRECTIONS DISAGREE'}")
    if tot:
        print(f"\n  direction agreement: {same_dir}/{tot}")
        ce = sum(1 for o in B if o["signal"] == "BUY_CE")
        print(f"  ({args.b} is {100*ce/len(B):.0f}% BUY_CE, so agreement by "
              f"chance is NOT 50% — read it against that)")

    print(f"\n  n={len(A)} is thin. This says the two OVERLAP, not how often "
          f"they would\n  over a longer record.")


if __name__ == "__main__":
    main()
