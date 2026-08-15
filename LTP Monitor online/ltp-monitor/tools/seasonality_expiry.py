#!/usr/bin/env python3
"""seasonality_expiry.py — SEAS-3, expiry-day intraday volatility SHAPE.

    SEAS-3 | Weekly expiry days show different intraday volatility shape
    than non-expiry days | Pinning near max-OI strikes, dealer gamma
    hedging unwind | Intraday realized vol profile differs (SHAPE, not
    just level) expiry vs non-expiry, tested per index

Unblocked by `fetch_expiry_calendar.py`, which reconstructs the real
calendar from NSE's own bhavcopies. That mattered more than expected:

    NIFTY      Thu through 2024, then TUESDAY from mid-2025
    BANKNIFTY  Thu through 2023, WEDNESDAY in 2024, Tuesday by 2026

A "weekly expiry is Thursday" rule — the shortcut this project refused —
would have mislabelled most of 2024-2026 and essentially all of
BANKNIFTY 2024. Because the mislabelling is contiguous in time rather
than scattered, it would have moved the estimate rather than blurring
it, and would have looked like a finding.

    python3 tools/seasonality_expiry.py

Read-only. Writes nothing. Reaches no verdict.


PRE-REGISTRATION (frozen 2026-08-15, before any result was looked at)
=====================================================================
Shares loaders and statistics with `seasonality_retro.py` by IMPORT.

Scope
    NIFTY from 2019-01-01 — NIFTY weekly options did not exist before
    2019, so "weekly expiry day" is not a well-defined question earlier
    (the calendar shows 12-13 NIFTY expiries/year in 2017-18 against
    46+ from 2019). BANKNIFTY from the start of its candle history.
    A day counts as an expiry day for an index if that index has an
    expiry on that date. Nothing is inferred from the weekday.

The hypothesis is about SHAPE, so level must be removed first
    Each day's 25 block |returns| are divided by that day's own mean.
    Expiry days may simply be more (or less) volatile overall; that is a
    LEVEL difference and would dominate any raw comparison while saying
    nothing about shape. Normalising per day removes it by construction,
    which is the only way "shape, not just level" can be tested honestly.
    The level difference is reported separately as descriptive.

Statistic
    L1 distance between the mean normalised profile of expiry days and
    that of non-expiry days, summed over the 25 blocks.

Test
    PERMUTATION on the expiry labels (20,000 shuffles, seeded). Under
    the null, which days are expiry days is arbitrary, so shuffling the
    labels gives the exact reference distribution. No distributional
    assumption is needed, and none would be safe: block volatilities are
    heavy-tailed and strongly correlated within a day.

Family
    One primary test per index. Benjamini-Hochberg at q=0.10 across
    them. Per-block differences are DESCRIPTIVE — 25 blocks scanned for
    the largest gap is exactly the multiple-comparison error the L1
    omnibus exists to avoid.

Negative control
    The same test with expiry labels replaced by a deterministic shuffle
    of equal size. Must land near p=0.5.

Insufficient sample
    Fewer than 30 expiry days or 100 non-expiry days -> the index is
    reported as insufficient and no number is printed.

What this cannot answer
    Direction, profitability, or anything after costs. A different
    volatility shape is not an edge.
"""
import argparse
import collections
import datetime
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3

import seasonality_retro as S

CAL = os.path.expanduser("~/.ltp-monitor/expiry_calendar.json")
N_PERM = 20000
SEED = 20260815
MIN_EXPIRY_DAYS = 30
MIN_OTHER_DAYS = 100
SCOPE_FROM = {"NIFTY": datetime.date(2019, 1, 1)}


def normalised_profiles(days, expiry_set, since=None):
    """([expiry_profiles], [other_profiles], level_expiry, level_other)."""
    exp, oth, lv_e, lv_o = [], [], [], []
    for date in sorted(days):
        if since and date < since:
            continue
        if date.weekday() >= 5:
            continue                      # Budget/Muhurat specials
        blocks, _r = S.day_blocks(days[date])
        if blocks is None:
            continue
        absr = [abs(S.block_return(b)) for b in blocks]
        m = statistics.mean(absr)
        if m <= 0:
            continue
        prof = [a / m for a in absr]
        if date.isoformat() in expiry_set:
            exp.append(prof)
            lv_e.append(m)
        else:
            oth.append(prof)
            lv_o.append(m)
    return exp, oth, lv_e, lv_o


def mean_profile(profiles):
    n = len(profiles[0])
    return [statistics.mean(p[i] for p in profiles) for i in range(n)]


def l1(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def permutation_p(exp, oth, n_perm=N_PERM, seed=SEED):
    pool = exp + oth
    k = len(exp)
    observed = l1(mean_profile(exp), mean_profile(oth))
    rng = random.Random(seed)
    idx = list(range(len(pool)))
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(idx)
        a = [pool[i] for i in idx[:k]]
        b = [pool[i] for i in idx[k:]]
        if l1(mean_profile(a), mean_profile(b)) >= observed:
            hits += 1
    return observed, (hits + 1) / (n_perm + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser(
        "~/.ltp-monitor/research_history.db"))
    ap.add_argument("--calendar", default=CAL)
    ap.add_argument("--permutations", type=int, default=N_PERM)
    ap.add_argument("--fdr", type=float, default=0.10)
    args = ap.parse_args()

    if not os.path.exists(args.calendar):
        print(f"no expiry calendar at {args.calendar} — run "
              f"tools/fetch_expiry_calendar.py first", file=sys.stderr)
        return 2
    cal = json.load(open(args.calendar))["expiries"]
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print("=" * 78)
    print("SEAS-3 — expiry-day intraday volatility SHAPE. NO verdict.")
    print(f"permutation on expiry labels, {args.permutations:,} shuffles, "
          f"seed {SEED}")
    print("level removed per day; shape is the question")
    print("=" * 78)

    family = []
    for sid, sym in S.SYMBOLS.items():
        if sym not in cal:
            print(f"\n{sym}: no expiries in the calendar")
            continue
        days = S.load_days(conn, sid)
        if not days:
            continue
        exp_set = set(cal[sym])
        exp, oth, lv_e, lv_o = normalised_profiles(
            days, exp_set, since=SCOPE_FROM.get(sym))
        if len(exp) < MIN_EXPIRY_DAYS or len(oth) < MIN_OTHER_DAYS:
            print(f"\n{'-'*78}\n{sym}: {len(exp)} expiry / {len(oth)} other "
                  f"— insufficient sample")
            continue

        since = SCOPE_FROM.get(sym)
        print(f"\n{'-'*78}\n{sym}   {len(exp)} expiry days vs {len(oth)} other"
              + (f"   (from {since}, weeklies did not exist earlier)"
                 if since else ""))

        obs, p = permutation_p(exp, oth, n_perm=args.permutations)
        print(f"  SHAPE  L1 distance {obs:.4f}   permutation p={p:.4f}")

        me, mo = statistics.mean(lv_e), statistics.mean(lv_o)
        print(f"  level (descriptive, NOT the hypothesis): expiry "
              f"{me*1e4:.1f} bps vs other {mo*1e4:.1f} bps "
              f"({(me/mo-1)*100:+.1f}%)")

        pe, po = mean_profile(exp), mean_profile(oth)
        gaps = sorted(range(25), key=lambda i: -abs(pe[i] - po[i]))[:3]
        print("  largest per-block gaps (DESCRIPTIVE — scanning 25 blocks "
              "for the biggest is the error the L1 test avoids):")
        for i in gaps:
            clock = S.SESSION_START + i * S.BLOCK_MINUTES
            print(f"    {clock//60:02d}:{clock%60:02d}  expiry {pe[i]:.3f} vs "
                  f"other {po[i]:.3f}  ({pe[i]-po[i]:+.3f})")

        rng = random.Random(SEED + 7)
        pool = exp + oth
        rng.shuffle(pool)
        _o, pc = permutation_p(pool[:len(exp)], pool[len(exp):],
                               n_perm=args.permutations, seed=SEED + 8)
        print(f"  control (random split of the same days): p={pc:.4f}"
              + ("   <-- CONTROL SIGNIFICANT, distrust the above"
                 if pc < 0.05 else ""))
        family.append((f"{sym}:SEAS-3", p))

    print(f"\n{'='*78}\nBENJAMINI-HOCHBERG across {len(family)} tests, "
          f"q={args.fdr}")
    if family:
        adj = S.benjamini_hochberg(family, q=args.fdr)
        for key, p in sorted(family, key=lambda kp: kp[1]):
            qv, rej = adj[key]
            print(f"  {key:<20} p={p:.4f}  q={qv:.4f}  "
                  f"{'survives' if rej else 'does not survive'}")
    else:
        print("  no index had enough days")

    print(f"\n{'='*78}")
    print("A different volatility SHAPE is not an edge. No direction, no")
    print("costs, nothing tradeable follows from this file.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
