#!/usr/bin/env python3
"""Do OI walls predict anything? — frozen specification.

Run:  ./venv/bin/python scratch/wall_predictiveness.py

The credit spreads sell the short leg at an OI wall, and their own
descriptions say the trade "profits while the index stays above the
wall". That is a falsifiable claim about the market, and nothing had
tested it. This script is the test, written BEFORE looking at any
outcome and deliberately left unchanged so re-running it on more data
is a genuine replication rather than a fresh search.

PRE-REGISTERED SPECIFICATION
----------------------------
WALL DEFINITION — analyzer._top_wall_from_snapshot's exact formula:
    score = oi*0.5 + max(oi_chg,0)*0.3 + volume*0.2
    CE candidates strike >= spot, PE candidates strike <= spot,
    wall = argmax(score)
Deliberately the system's OWN definition. A proxy of my own would test
a different strategy than the one that trades.

IDENTIFICATION — distance and wall-ness cannot be varied independently
within a snapshot: at a given distance there is exactly ONE strike. So
the comparison is ACROSS days, WITHIN a distance bucket — for strikes
the same distance from spot, is the one that happens to be the top wall
breached less often than the ones that are not? Comparing walls against
all strikes instead would measure moneyness, not wall-ness, and would
produce a large, meaningless result.

UNIT — one snapshot per symbol per day (10:30 IST). NOT the ~23k rows
per symbol-day: those are the same wall observed repeatedly, and
treating them as independent manufactures significance.

OUTCOMES, strictly forward of the snapshot:
    touch — spot reaches the strike before session close
    close — spot ends the session beyond the strike (the one that
            settles a credit spread's P&L)

PLACEBO — yesterday's wall strike used to predict today. It should land
near zero with a tight interval. If it does not, the harness is too
noisy to certify and the primary result means nothing.

RESULT, 2026-08-14 (12 usable days, 2026-07-29..08-14, 62 wall obs)
-------------------------------------------------------------------
    outcome          wall vs non-wall        95% CI
    touch                 +2.7 pp      [ -9.8, +15.2]
    close beyond          +6.6 pp      [ -2.7, +16.0]
    placebo/touch         -8.2 pp      [-32.4, +16.0]
    placebo/close         +5.2 pp      [ -2.6, +13.1]

Negative = walls breached LESS = walls "work". Both real results are
POSITIVE (walls breached slightly more) and both span zero. On the
touch outcome the meaningless predictor scored BETTER than the real
one. The placebo did not land near zero, so the harness is itself
unvalidated at this sample size.

Verdict: INSUFFICIENT EVIDENCE. Day-clustered SE 4.8-6.4 pp means the
smallest detectable effect is ~13 pp; a real 5 pp effect would be
invisible. This does NOT show the spreads don't work — selling premium
can pay even if strike selection is arbitrary — it shows the WALL
story is unsupported.

Re-run unchanged once chain_snapshots holds 60+ days. Do not re-slice
the same 12 days at a different snapshot time or MAX_GAPS: that is
multiple testing, and promotion_gate.deflation_k exists precisely
because this codebase has been burned by it.
"""
import collections
import os
import sqlite3
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history                                            # noqa: E402

DB = os.path.expanduser("~/.ltp-monitor/history.db")
SYMS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
SNAP_MIN = 75          # minutes after 09:15 -> 10:30 IST
MAX_GAPS = 6           # only strikes within 6 strike-gaps of spot


def score(oi, oi_chg, vol):
    """analyzer._top_wall_from_snapshot's formula, verbatim."""
    return (oi or 0) * 0.5 + max(oi_chg or 0, 0) * 0.3 + (vol or 0) * 0.2


def days_for(db, sym):
    q = ("select distinct date(ts,'unixepoch','+5 hours','+30 minutes') "
         "from chain_snapshots where symbol=? order by 1")
    return [r[0] for r in db.execute(q, (sym,))]


def run():
    db = sqlite3.connect(DB)
    obs = []                      # one row per (sym, day, side, strike)
    walls_by_day = {}             # (sym, side, day) -> wall strike
    for sym in SYMS:
        for day in days_for(db, sym):
            candles = history.day_index_candles(sym, day, for_compute=True)
            if not candles or len(candles) < SNAP_MIN + 30:
                continue
            c0 = candles[SNAP_MIN]
            t_snap, spot = c0["ts"], c0["close"]
            fwd = candles[SNAP_MIN + 1:]
            if not fwd:
                continue
            hi = max(x["high"] for x in fwd)
            lo = min(x["low"] for x in fwd)
            close_end = fwd[-1]["close"]

            rows = db.execute(
                "select strike, leg, oi, oi_chg, volume from chain_snapshots "
                "where symbol=? and ts=(select ts from chain_snapshots "
                "where symbol=? order by abs(ts-?) limit 1)",
                (sym, sym, t_snap)).fetchall()
            if not rows:
                continue
            strikes = sorted({r[0] for r in rows})
            if len(strikes) < 5:
                continue
            gap = st.median([b - a for a, b in zip(strikes, strikes[1:])]) or 50

            for side in ("ce", "pe"):
                cands = [(k, score(o, oc, v)) for k, lg, o, oc, v in rows
                         if lg == side and ((side == "ce" and k >= spot) or
                                            (side == "pe" and k <= spot))]
                if len(cands) < 3:
                    continue
                wall = max(cands, key=lambda x: x[1])[0]
                walls_by_day[(sym, side, day)] = wall
                for k, _s in cands:
                    d = round(abs(k - spot) / gap)
                    if d < 1 or d > MAX_GAPS:
                        continue
                    obs.append(dict(
                        sym=sym, day=day, side=side, strike=k, dist=d,
                        is_wall=(k == wall),
                        touch=(hi >= k) if side == "ce" else (lo <= k),
                        close=(close_end >= k) if side == "ce"
                        else (close_end <= k)))
    return obs, walls_by_day


def compare(obs, flag="is_wall", label="WALL", oc="touch"):
    """Within-distance-bucket comparison, summarised with day clusters."""
    print(f"\n--- {label} vs non-{label} — outcome '{oc}' ---")
    print(f"{'gaps':>5} {'n_flag':>8} {oc+'%':>8} {'n_other':>9} {oc+'%':>8} {'diff':>8}")
    per_day = collections.defaultdict(list)
    tot_w = tot_o = 0
    for d in range(1, MAX_GAPS + 1):
        w = [o for o in obs if o["dist"] == d and o[flag]]
        n = [o for o in obs if o["dist"] == d and not o[flag]]
        if len(w) < 5 or len(n) < 5:
            continue
        pw = sum(o[oc] for o in w) / len(w) * 100
        pn = sum(o[oc] for o in n) / len(n) * 100
        tot_w += len(w)
        tot_o += len(n)
        print(f"{d:5} {len(w):8} {pw:7.1f}% {len(n):9} {pn:7.1f}% {pw - pn:+7.1f}")
        for day in {o["day"] for o in w}:
            wd = [o for o in w if o["day"] == day]
            nd = [o for o in n if o["day"] == day]
            if wd and nd:
                per_day[day].append(sum(o[oc] for o in wd) / len(wd) * 100
                                    - sum(o[oc] for o in nd) / len(nd) * 100)
    means = [st.mean(v) for v in per_day.values() if v]
    if len(means) < 3:
        print("  too few day clusters to summarise")
        return
    m = st.mean(means)
    se = st.stdev(means) / (len(means) ** 0.5)
    print(f"\n  day-clustered mean difference: {m:+.1f} pp "
          f"(SE {se:.1f}, n_days={len(means)})")
    print(f"  95% CI: [{m - 1.96 * se:+.1f}, {m + 1.96 * se:+.1f}] pp")
    print("  negative = wall breached LESS often, i.e. walls 'work'")
    print(f"  smallest detectable effect at this SE: ~{1.96 * se:.0f} pp")
    print(f"  observations: {tot_w} flagged vs {tot_o} other")


if __name__ == "__main__":
    obs, walls = run()
    print(f"observations: {len(obs)} strike-days across "
          f"{len({(o['sym'], o['day']) for o in obs})} symbol-days, "
          f"{len({o['day'] for o in obs})} distinct days")

    days = sorted({o["day"] for o in obs})
    prev = {d: days[i - 1] for i, d in enumerate(days) if i > 0}
    for o in obs:
        pd = prev.get(o["day"])
        o["is_stale_wall"] = (pd is not None and
                              walls.get((o["sym"], o["side"], pd)) == o["strike"])

    for oc in ("touch", "close"):
        compare(obs, "is_wall", "WALL", oc=oc)
        compare(obs, "is_stale_wall", "STALE-WALL(placebo)", oc=oc)
