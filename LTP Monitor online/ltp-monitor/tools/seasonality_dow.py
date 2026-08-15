#!/usr/bin/env python3
"""seasonality_dow.py — SEAS-4, day-of-week effects.

The fourth hypothesis in `research-memo-addendum-seasonality-flow.md`:

    SEAS-4 | Day-of-week effects in direction or volatility (e.g.
    Monday gap behavior, Friday pre-weekend positioning) | Weekend risk
    premium, news accumulation over non-trading days | Mean return /
    vol by weekday, tested for significance vs. random day assignment

Memo section 5 deferred SEAS-3 and SEAS-4 because they needed data that
did not exist locally. After `backfill_index_history.py` it does — 2,312
NIFTY sessions from 2017-04 — and SEAS-4 needs nothing beyond the date,
so it is now inside Phase 1's stated scope ("test SEAS-1 through SEAS-4
as pure statistical questions on price data — no options, no OI").

SEAS-3 is still NOT testable and is deliberately not attempted here. It
needs a historical weekly-expiry calendar back to 2017. The
`instruments` table holds only current expiries (2026-07 onward) and
Dhan's /optionchain/expirylist returns only FUTURE dates (verified
2026-08-15: 18 expiries, earliest 2026-08-18). Deriving the calendar
from "weekly expiry is Thursday" would be wrong — NSE has changed the
weekly expiry day more than once — and inventing it is precisely the
"reproduce the shape rather than the meaning" failure this codebase has
already been bitten by. SEAS-3 stays blocked until a real calendar
exists.

    python3 tools/seasonality_dow.py --db ~/.ltp-monitor/research_history.db

Read-only. Writes nothing. Reaches no verdict.


PRE-REGISTRATION (frozen 2026-08-15, before any result was looked at)
=====================================================================
Shares every statistic with `seasonality_retro.py` by IMPORT rather than
by copy. Two near-identical implementations drifting at the margins has
already happened three times in this repo (market-session check, news
sentiment regexes, OI quadrant classifier); the day loader, the block
splitter, Wilson, BH and the exact binomial all come from that module.

Unit of observation
    ONE TRADING DAY, as in seasonality_retro. Day-of-week is read from
    the date; no exchange calendar is needed or assumed.

Day admission
    Identical to seasonality_retro (>=360 bars, all 25 blocks present,
    positive prices), so the two reports describe the same sample.

Measures, per day
    direction  = (session close - session open) / session open
    volatility = |direction|
    These are deliberately the two the memo names ("mean return / vol").

Primary test — OMNIBUS, per index per measure
    Is there ANY day-of-week structure at all? Statistic is the spread
    of the five weekday means:  max(mean_d) - min(mean_d).

    Tested by PERMUTATION, which is what the memo asks for ("vs. random
    day assignment"): shuffle the weekday labels across days 20,000
    times, recompute the spread, and report the fraction of shuffles
    whose spread is >= the observed one. That is an exact
    randomisation p-value and needs no distributional assumption —
    daily index returns are fat-tailed and a normal-theory ANOVA would
    overstate significance on exactly this data.

    Seeded deterministically so the number is reproducible.

    Family = {direction, volatility} x {4 indices} = 8 tests,
    Benjamini-Hochberg at q=0.10. Per-weekday cells are DESCRIPTIVE and
    excluded — testing five weekdays separately and reporting the
    largest is the multiple-comparison error the omnibus exists to
    avoid.

Negative control
    The same omnibus run against a deterministically shuffled weekday
    assignment. It must land near p=0.5. If the control is significant,
    the permutation machinery is broken and no result from it means
    anything.

Insufficient sample
    Below --min-days (default 100) admitted days, a cell reports
    "insufficient sample" and no number.

What this cannot answer
    Direction of trade, profitability, or anything after costs. A
    weekday effect in mean return is not an edge; it is a starting
    point that would then have to clear costs this file does not model.
"""
import argparse
import collections
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3

import seasonality_retro as S   # shared loaders + statistics, never re-implemented

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
N_PERM = 20000
SEED = 20260815


def day_metrics(days, min_days):
    """[(date, weekday, direction, volatility)], drops, weekend sessions.

    2026-08-15 — the first run of this tool crashed with IndexError on a
    weekday of 5. NSE really does trade on some weekends: Union Budget
    Saturdays (2020-02-01, 2025-02-01), a Budget SUNDAY (2026-02-01),
    Muhurat (2021-11-13), and a disaster-recovery live session
    (2024-01-20). Five sessions in NIFTY's 2,312.

    They are EXCLUDED, because a Budget-day or Diwali session is not a
    sample of "what Saturdays are like" — it is a sample of Budget day,
    and SEAS-4 is a question about the ordinary weekly cycle. They are
    returned rather than silently dropped, because a filter whose
    rejections are invisible is a hidden sample-selection choice.

    Worth noting the crash was the good outcome: had WEEKDAYS simply had
    seven entries, these five would have quietly formed two junk
    "weekday" cells and nothing would have said so.
    """
    out, drops, weekend = [], collections.Counter(), []
    for date in sorted(days):
        blocks, reason = S.day_blocks(days[date])
        if blocks is None:
            drops[reason] += 1
            continue
        if date.weekday() >= 5:
            weekend.append(date)
            drops["weekend_special_session"] += 1
            continue
        first = sorted(blocks[0])
        last = sorted(blocks[-1])
        o, c = first[0][1], last[-1][2]
        if not o:
            drops["bad_price"] += 1
            continue
        r = (c - o) / o
        out.append((date, date.weekday(), r, abs(r)))
    return out, drops, weekend


def omnibus_permutation(labels, values, n_perm=N_PERM, seed=SEED):
    """Randomisation test on the spread of the five weekday means.

    Returns (observed_spread, p, {weekday: (n, mean)}).
    """
    groups = collections.defaultdict(list)
    for lab, v in zip(labels, values):
        groups[lab].append(v)
    per = {d: (len(groups[d]), statistics.mean(groups[d]))
           for d in sorted(groups) if groups[d]}
    if len(per) < 2:
        return 0.0, 1.0, per
    means = [m for _n, m in per.values()]
    observed = max(means) - min(means)

    rng = random.Random(seed)
    shuffled = list(labels)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        g = collections.defaultdict(float)
        cnt = collections.defaultdict(int)
        for lab, v in zip(shuffled, values):
            g[lab] += v
            cnt[lab] += 1
        ms = [g[d] / cnt[d] for d in g if cnt[d]]
        if max(ms) - min(ms) >= observed:
            hits += 1
    # +1/+1 so a p-value is never exactly zero — with 20k permutations
    # the smallest reportable value is 1/20001, and printing 0.0000
    # would claim more resolution than the test has.
    return observed, (hits + 1) / (n_perm + 1), per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser(
        "~/.ltp-monitor/research_history.db"))
    ap.add_argument("--min-days", type=int, default=100)
    ap.add_argument("--fdr", type=float, default=0.10)
    ap.add_argument("--permutations", type=int, default=N_PERM)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no archive at {args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print("=" * 78)
    print("SEAS-4 — day-of-week effects. Distribution report, NO verdict.")
    print(f"permutation test, {args.permutations:,} shuffles, seed {SEED}")
    print("SEAS-3 is NOT tested here — no historical expiry calendar exists "
          "(see docstring)")
    print("=" * 78)

    family = []
    for sid, sym in S.SYMBOLS.items():
        days = S.load_days(conn, sid)
        if not days:
            print(f"\n{sym}: no candles")
            continue
        rows, drops, weekend = day_metrics(days, args.min_days)
        if len(rows) < args.min_days:
            print(f"\n{sym}: n={len(rows)} — insufficient sample")
            continue

        lo, hi = rows[0][0], rows[-1][0]
        print(f"\n{'-'*78}\n{sym}   {lo} .. {hi}   {len(rows)} days"
              f"   ({sum(drops.values())} dropped: {dict(drops)})")
        if weekend:
            print(f"  excluded weekend sessions (Budget/Muhurat/DR — not "
                  f"ordinary weekdays): {', '.join(str(d) for d in weekend)}")

        labels = [r[1] for r in rows]
        for measure, idx in (("direction (open->close)", 2),
                             ("volatility |open->close|", 3)):
            vals = [r[idx] for r in rows]
            spread, p, per = omnibus_permutation(
                labels, vals, n_perm=args.permutations)
            unit = 1e4   # basis points
            print(f"  {measure}")
            cells = "  ".join(
                f"{WEEKDAYS[d]} {per[d][1]*unit:+7.1f}({per[d][0]})"
                for d in sorted(per))
            print(f"    {cells}")
            print(f"    omnibus spread {spread*unit:.1f} bps   "
                  f"permutation p={p:.4f}")
            family.append((f"{sym}:{measure.split()[0]}", p))

        # Negative control — labels deliberately scrambled. Must be null.
        rng = random.Random(SEED + 1)
        fake = list(labels)
        rng.shuffle(fake)
        _s, pc, _per = omnibus_permutation(
            fake, [r[3] for r in rows], n_perm=args.permutations,
            seed=SEED + 2)
        print(f"  control (scrambled weekday labels, volatility): "
              f"p={pc:.4f}" + ("   <-- CONTROL IS SIGNIFICANT, distrust "
                               "everything above" if pc < 0.05 else ""))

    print(f"\n{'='*78}\nBENJAMINI-HOCHBERG across {len(family)} pre-registered "
          f"tests, q={args.fdr}")
    if not family:
        print("  no cell had enough days")
    else:
        adj = S.benjamini_hochberg(family, q=args.fdr)
        for key, p in sorted(family, key=lambda kp: kp[1]):
            qv, rej = adj[key]
            print(f"  {key:<28} p={p:.4f}  q={qv:.4f}  "
                  f"{'survives' if rej else 'does not survive'}")

    print(f"\n{'='*78}")
    print("A weekday effect in mean return is NOT an edge. Costs are not")
    print("modelled here at all, and the per-weekday cells are descriptive —")
    print("picking the biggest one and calling it a finding is the error the")
    print("omnibus test exists to prevent.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
