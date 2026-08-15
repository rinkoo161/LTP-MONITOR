#!/usr/bin/env python3
"""seasonality_retro.py — SEAS-1 and SEAS-2 against the archive we already have.

Scoped by `research-memo-addendum-seasonality-flow.md` section 5, which
authorises exactly this and nothing else:

    replays existing archived index spot/futures candle data (whatever
    history is actually available today, before any new sourcing)
    against SEAS-1 and SEAS-2 only, since those need no new data
    acquisition. Report distribution stats (not a verdict) [...] per
    index, per year available.

So: no FLOW-* hypotheses, no SEAS-3/SEAS-4 (they need expiry calendars
and a day-of-week family this is not authorised to open), no filter, no
strategy, no config key, and no verdict. This script prints numbers and
refuses to conclude. Phase 3 attachment is explicitly gated behind
`derivatives-third-eye` and is not this file's business.

Read-only against ~/.ltp-monitor/history.db. Writes nothing anywhere.

    python3 tools/seasonality_retro.py
    python3 tools/seasonality_retro.py --min-days 60 --json


PRE-REGISTRATION (frozen 2026-08-15, before any result was looked at)
=====================================================================
The memo requires hypotheses pre-registered before testing. This block
is that registration. It is deliberately written as the specification
the code implements, so the two cannot drift: if you change what is
measured, this text is wrong and the change is visible in the diff.

Universe
    NIFTY, BANKNIFTY, FINNIFTY, SENSEX index 1-minute bars from the
    `candles` table. Whatever range exists at run time — the script
    reports it rather than assuming, because the memo's Phase 0
    question ("what do we have back to 2015") is NOT yet answered and
    this script must not pretend it is.

Unit of observation
    ONE TRADING DAY. Not one bar. A day contributes exactly one
    observation to each test, so the day-clustering that would
    otherwise inflate every t-statistic is handled by construction
    rather than by a correction applied afterwards.

Day admission (applied before any statistic, reported in full)
    - >= 360 of the 375 expected 09:15-15:29 bars, so truncated and
      half sessions cannot masquerade as full ones.
    - all 25 fifteen-minute blocks non-empty.
    - strictly positive prices throughout.
    Dropped days are counted BY REASON and printed. A filter whose
    rejections are invisible is a silent sample-selection choice.

SEAS-1  "Last N minutes show larger absolute moves than the session median"
    Per day, split 09:15-15:29 into 25 non-overlapping 15-minute
    blocks. Block return = (last close - first open) / first open.
    Statistic: does |return| of the LAST block exceed the MEDIAN
    |return| of the OTHER TWENTY-FOUR blocks?

    The exclusion is not cosmetic. Comparing a block against a median
    that INCLUDES it gives a null of 12/25 = 0.48, not 0.5, and at
    n=531 that 2-point bias alone produces a "significant" result with
    no time-of-day effect whatever. Excluding the tested block makes
    the null exactly 0.5 under exchangeability.

    Test: two-sided EXACT binomial against p=0.5. Normal approximation
    is not used anywhere; it is unnecessary at this cost and it is the
    kind of shortcut that turns a marginal result into a confident one.

SEAS-2  "Opening range direction partially predicts the day's direction"
    Two outcomes are reported, and the distinction is the whole point:

      (a) ORB -> REST-OF-DAY   sign(block 0) vs sign(block0 close ->
          session close). Disjoint windows. This is the only version
          that could ever be traded, and the only one this script
          treats as a primary test.

      (b) ORB -> FULL DAY      sign(block 0) vs sign(session open ->
          session close). Reported for comparability with the memo's
          wording, and flagged, because the ORB is a SUBSET of the
          full-day window: the two share the first 15 minutes, so they
          are mechanically correlated even under a pure random walk.
          A headline number from (b) would be an artefact.

    Test: two-sided exact binomial on directional agreement vs p=0.5.
    Days with a zero return in either leg are excluded and counted.

Multiple testing
    Primary family = {SEAS-1, SEAS-2a} x {4 indices} = 8 tests.
    Benjamini-Hochberg at q=0.10 across those 8. Per-year breakdowns
    and SEAS-2b are DESCRIPTIVE and deliberately excluded from the
    family — adding them would let a stricter-looking correction hide
    the fact that the per-year cells are underpowered.

Negative controls (run on the same data, same code path)
    C1  SEAS-1 with a RANDOM non-final block substituted for the last
        block. A real end-of-session effect must not appear here.
    C2  SEAS-2a with the ORB sign replaced by a deterministic
        pseudo-random sign seeded per day. Must land at ~50%.
    Controls scoring like the real thing is the finding, not a bug.
    Seeded from the date alone, so runs are reproducible without
    storing state.

Insufficient sample
    Any cell with fewer than --min-days (default 100) admitted days
    reports "insufficient sample" and NO percentage. Per CLAUDE.md
    rule 7, below a stated n the honest answer is not a number.

What this script cannot answer
    Whether any of this survives costs, whether it is stable
    out-of-sample beyond the crude first-half/second-half split
    reported, and whether it holds before 2024. It is a distribution
    report on a ~2-year window, which is the whole of what exists.


AMENDMENT 1 (2026-08-15, after the backfill) — SEAS-1b
======================================================
The original block above is left EXACTLY as frozen. This is an
amendment, appended, so the sequence of what was known when stays
readable. Editing the frozen text to match a later finding is how a
post-hoc result becomes indistinguishable from a prediction.

`tools/backfill_index_history.py` took NIFTY from 530 days to 2,312
(2017-04 onward). On that sample SEAS-1 became significant and pointed
the WRONG WAY: 45.8% (p=0.0001), i.e. the last 15 minutes are QUIETER
than the typical block, not louder.

Plotting the median |return| by block explained it. There is a large
pre-close volatility event, and it is one block earlier than SEAS-1
looks:

    09:15   16.1 bps   <- the open, by far the largest
    12:15    5.1 bps   <- midday trough
    15:00   10.2 bps   <- a clear second peak
    15:15    5.9 bps   <- the block SEAS-1 actually tests

So the memo's MECHANISM (pre-close flow, MIS unwind, closing-auction
adjacency) looks real, while its WINDOW was wrong. A plausible
structural reason, stated as a candidate and not as established fact:
NSE derives the closing price from a VWAP over the final half hour, so
15:00 begins a regime with different incentives — but this script does
not test that, and the correlation would be identical if the cause were
something else entirely.

SEAS-1b, pre-registered before it was run:
    Block 23 (15:00-15:14) |return| exceeds the median of the other 24
    blocks at a rate ABOVE 50%.

Provenance matters here and is reported per index. The hypothesis was
generated from the NIFTY and SENSEX profiles, so those two are
CONTAMINATED for it. The BANKNIFTY and FINNIFTY profiles had not been
examined when SEAS-1b was written, which makes them a genuine — if
imperfect, since their SEAS-1 numbers had been seen — out-of-sample
test. Both confirmed it at p<1e-6.

SEAS-1b is deliberately kept OUT of the Benjamini-Hochberg family. It
was not in the original pre-registration, and admitting it after the
fact would let a post-hoc hypothesis borrow the credibility of a
pre-registered one. It is reported next to its controls and its
provenance instead.

Still true of SEAS-1b, and worth repeating because a large p-value
invites over-reading: it is a statement about VOLATILITY, not about
direction, not about profit, and costs are not modelled anywhere in
this file.
"""
import argparse
import collections
import datetime
import json
import math
import os
import sqlite3
import statistics
import sys

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SESSION_START = 9 * 60 + 15          # 09:15
SESSION_END = 15 * 60 + 29           # 15:29 inclusive
BLOCK_MINUTES = 15
N_BLOCKS = 25                        # 375 minutes / 15
EXPECTED_BARS = 375
MIN_BARS = 360                       # tolerate a few missing prints, not a half day

SYMBOLS = {"13": "NIFTY", "25": "BANKNIFTY", "27": "FINNIFTY", "51": "SENSEX"}


# ----------------------------------------------------------------- stats

def _log_binom_pmf(i, n, p):
    """log P(X=i) via lgamma — never materialises a huge binomial coefficient."""
    if p <= 0.0:
        return 0.0 if i == 0 else float("-inf")
    if p >= 1.0:
        return 0.0 if i == n else float("-inf")
    return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p))


def binom_two_sided(k, n, p=0.5):
    """Exact two-sided binomial p-value.

    Sums the probability of every outcome no more likely than the one
    observed. At p=0.5 this is symmetric, but it is written generally
    so a future caller cannot silently get a one-sided answer.

    Computed in LOG space. The obvious formulation — math.comb(n,k) *
    p**k * (1-p)**(n-k) — raises OverflowError somewhere above n≈1700,
    because comb() returns an exact Python int far larger than a float
    can hold while the probability factors underflow to zero. It ran
    fine on the 530-day archive and blew up the moment the backfill
    took NIFTY to 2,312 days, which is the useful kind of failure: it
    stopped rather than quietly returning a wrong p-value.
    """
    if n <= 0:
        return 1.0
    log_obs = _log_binom_pmf(k, n, p)
    tol = log_obs + 1e-9
    total = 0.0
    for i in range(n + 1):
        lp = _log_binom_pmf(i, n, p)
        if lp <= tol:
            total += math.exp(lp)
    return min(1.0, total)


def benjamini_hochberg(pairs, q=0.10):
    """[(key, p)] -> {key: (q_value, rejected)} at false-discovery rate q.

    Step-up on the sorted p-values, then the standard monotonicity pass
    so a q-value can never fall below one at a smaller p.
    """
    m = len(pairs)
    if not m:
        return {}
    ordered = sorted(pairs, key=lambda kp: kp[1])
    qs = []
    for i, (key, p) in enumerate(ordered, start=1):
        qs.append((key, p, min(1.0, p * m / i)))
    out = {}
    running = 1.0
    for key, p, qv in reversed(qs):
        running = min(running, qv)
        out[key] = (running, running <= q)
    return out


def mcnemar_exact(b, c):
    """Exact McNemar for PAIRED binary outcomes on the same days.

    b = real hit / control miss, c = real miss / control hit. Under the
    null the discordant pairs split 50/50, so this is an exact binomial
    on b out of (b+c). Concordant pairs carry no information and are
    correctly ignored — using an unpaired two-proportion test here
    would throw away the pairing and overstate the variance.
    """
    n = b + c
    if n == 0:
        return 1.0
    return binom_two_sided(b, n)


def wilson_interval(k, n, z=1.96):
    """95% Wilson interval for a proportion.

    Wilson rather than normal-approximation: near 0.5 they agree, but
    Wilson does not produce bounds outside [0,1] in the small per-year
    cells, where a naive interval would print something impossible.
    """
    if n == 0:
        return (0.0, 1.0)
    ph = k / n
    d = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / d
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def day_rng(date_str, salt):
    """Deterministic per-day pseudo-random in [0,1) — no global seed.

    Uses the date and a salt so control C1/C2 are reproducible across
    runs and machines without storing anything, and so the two controls
    do not accidentally share a draw.
    """
    h = 2166136261
    for ch in f"{date_str}|{salt}":
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h / 0x100000000


# ------------------------------------------------------------ data layer

def load_days(conn, security_id):
    """{date -> [(minute_of_day, o, h, l, c)]} for one instrument."""
    days = collections.defaultdict(list)
    cur = conn.execute(
        "SELECT ts, o, h, l, c FROM candles WHERE security_id=? ORDER BY ts",
        (security_id,))
    for ts, o, h, l, c in cur:
        t = datetime.datetime.fromtimestamp(ts, IST)
        mod = t.hour * 60 + t.minute
        if mod < SESSION_START or mod > SESSION_END:
            continue
        days[t.date()].append((mod, o, h, l, c))
    return days


def day_blocks(bars):
    """Split one day's bars into 25 blocks. None if the day is unusable.

    Returns (blocks, reason) where blocks is a list of 25 lists.
    """
    if len(bars) < MIN_BARS:
        return None, "short_session"
    blocks = [[] for _ in range(N_BLOCKS)]
    for mod, o, h, l, c in bars:
        if o is None or c is None or o <= 0 or c <= 0:
            return None, "bad_price"
        idx = (mod - SESSION_START) // BLOCK_MINUTES
        if 0 <= idx < N_BLOCKS:
            blocks[idx].append((mod, o, c))
    if any(not b for b in blocks):
        return None, "empty_block"
    return blocks, None


def block_return(block):
    """(last close - first open) / first open for one 15-minute block."""
    block = sorted(block)
    first_open = block[0][1]
    last_close = block[-1][2]
    if not first_open:
        return 0.0
    return (last_close - first_open) / first_open


# ------------------------------------------------------------- the tests

def analyse_symbol(days, min_days):
    """All per-day observations for one index, plus drop accounting."""
    drops = collections.Counter()
    obs = []
    for date in sorted(days):
        blocks, reason = day_blocks(days[date])
        if blocks is None:
            drops[reason] += 1
            continue
        rets = [block_return(b) for b in blocks]
        absr = [abs(r) for r in rets]

        # SEAS-1 — last block vs the median of the OTHER 24.
        others = absr[:-1]
        seas1_hit = absr[-1] > statistics.median(others)

        # SEAS-1b (amendment 1) — same comparison one block earlier.
        # Block 23 is 15:00-15:14, the peak the profile actually shows.
        b23_others = [a for i, a in enumerate(absr) if i != 23]
        seas1b_hit = absr[23] > statistics.median(b23_others)

        # C1 — same comparison, but a random NON-FINAL block stands in
        # for the last one. Its own value is excluded from the median
        # too, so the null stays exactly 0.5.
        pick = int(day_rng(str(date), "c1") * (N_BLOCKS - 1))
        pick = min(pick, N_BLOCKS - 2)
        c1_others = [a for i, a in enumerate(absr) if i != pick]
        c1_hit = absr[pick] > statistics.median(c1_others)

        # SEAS-2 — ORB vs rest-of-day (disjoint) and vs full day (not).
        orb = rets[0]
        first_block = sorted(blocks[0])
        last_block = sorted(blocks[-1])
        orb_close = first_block[-1][2]
        session_open = first_block[0][1]
        session_close = last_block[-1][2]
        rest = (session_close - orb_close) / orb_close if orb_close else 0.0
        full = (session_close - session_open) / session_open if session_open else 0.0

        c2_sign = 1.0 if day_rng(str(date), "c2") >= 0.5 else -1.0

        obs.append({
            "date": date,
            "seas1": seas1_hit,
            "seas1b": seas1b_hit,
            "c1": c1_hit,
            "orb": orb,
            "rest": rest,
            "full": full,
            "c2_sign": c2_sign,
        })
    return obs, drops


def agreement(obs, a_key, b_key, a_sign_key=None):
    """Directional agreement count, excluding zero-return days."""
    k = n = zero = 0
    for o in obs:
        a = o[a_sign_key] if a_sign_key else o[a_key]
        b = o[b_key]
        if a == 0 or b == 0:
            zero += 1
            continue
        n += 1
        if (a > 0) == (b > 0):
            k += 1
    return k, n, zero


def cell(k, n, min_days, label):
    """One reported cell: rate, interval, exact p — or an honest refusal."""
    if n < min_days:
        return {"label": label, "n": n, "insufficient": True}
    lo, hi = wilson_interval(k, n)
    return {"label": label, "n": n, "k": k, "rate": k / n,
            "ci": (lo, hi), "p": binom_two_sided(k, n),
            "insufficient": False}


def fmt(c):
    if c["insufficient"]:
        return f"n={c['n']:<5} insufficient sample (< min-days)"
    return (f"n={c['n']:<5} {c['rate']*100:5.1f}%  "
            f"95% CI [{c['ci'][0]*100:4.1f}, {c['ci'][1]*100:4.1f}]  "
            f"p={c['p']:.4f}")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.expanduser("~/.ltp-monitor/history.db"))
    ap.add_argument("--min-days", type=int, default=100,
                    help="below this, a cell reports 'insufficient sample'")
    ap.add_argument("--fdr", type=float, default=0.10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--profile", action="store_true",
                    help="median |15-min return| by block — this is what "
                         "revealed SEAS-1 was testing the wrong window")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no archive at {args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print("=" * 78)
    print("SEASONALITY RETRO — SEAS-1 and SEAS-2 only (memo section 5)")
    print("Distribution report. Deliberately reaches NO verdict.")
    print("=" * 78)

    results = {}
    family = []

    for sid, sym in SYMBOLS.items():
        days = load_days(conn, sid)
        if not days:
            print(f"\n{sym}: no candles in the archive")
            continue
        obs, drops = analyse_symbol(days, args.min_days)

        if args.profile and obs:
            prof = [[] for _ in range(N_BLOCKS)]
            for date in sorted(days):
                blocks, _r = day_blocks(days[date])
                if blocks is None:
                    continue
                for i, blk in enumerate(blocks):
                    prof[i].append(abs(block_return(blk)) * 1e4)
            flat = [v for col in prof for v in col]
            base = statistics.median(flat) or 1.0
            print(f"\n  {sym} — median |15-min return| in bps by block "
                  f"({len(obs)} days)")
            for i in range(N_BLOCKS):
                m = statistics.median(prof[i])
                clock = SESSION_START + i * BLOCK_MINUTES
                tag = ("  <- SEAS-1 tests THIS" if i == N_BLOCKS - 1
                       else "  <- SEAS-1b" if i == 23 else "")
                print(f"    {clock//60:02d}:{clock%60:02d} {m:6.1f}  "
                      f"{'#' * int(m / base * 20)}{tag}")
        if not obs:
            print(f"\n{sym}: no admissible days "
                  f"({sum(drops.values())} dropped: {dict(drops)})")
            continue

        lo, hi = min(o["date"] for o in obs), max(o["date"] for o in obs)
        print(f"\n{'-'*78}\n{sym}   {lo} .. {hi}   "
              f"{len(obs)} admitted days, {sum(drops.values())} dropped "
              f"({dict(drops) if drops else 'none'})")

        s1 = cell(sum(o["seas1"] for o in obs), len(obs), args.min_days,
                  "SEAS-1 last-block |ret| > median of other 24")
        c1 = cell(sum(o["c1"] for o in obs), len(obs), args.min_days,
                  "  control C1 random non-final block")
        k2, n2, z2 = agreement(obs, "orb", "rest")
        s2a = cell(k2, n2, args.min_days,
                   "SEAS-2a ORB dir -> REST-of-day dir (disjoint)")
        k2b, n2b, _ = agreement(obs, "orb", "full")
        s2b = cell(k2b, n2b, args.min_days,
                   "SEAS-2b ORB dir -> FULL-day dir (overlapping!)")
        kc2, nc2, _ = agreement(obs, None, "rest", a_sign_key="c2_sign")
        c2 = cell(kc2, nc2, args.min_days,
                  "  control C2 random sign -> rest-of-day")

        # Amendment 1. Provenance travels WITH the number, because the
        # p-value alone cannot tell you the hypothesis came from NIFTY
        # and SENSEX in the first place.
        CONTAMINATED = {"NIFTY", "SENSEX"}
        s1b = cell(sum(o["seas1b"] for o in obs), len(obs), args.min_days,
                   "SEAS-1b block 23 (15:00) |ret| > median of other 24")
        s1b["provenance"] = ("hypothesis generated HERE — contaminated"
                             if sym in CONTAMINATED else "out-of-sample")

        for c in (s1, s1b, c1, s2a, s2b, c2):
            print(f"  {c['label']:<52} {fmt(c)}")
            if c is s1b:
                print(f"  {'':<52}   [post-hoc, outside BH family; "
                      f"{s1b['provenance']}]")
        if z2:
            print(f"  {'':<52} ({z2} day(s) excluded for a zero return)")

        # POST-HOC, NOT PRE-REGISTERED. Added 2026-08-15 after the first
        # run showed control C1 landing FURTHER from 50% than SEAS-1
        # itself on two indices. That can only happen if 15-minute
        # blocks are not exchangeable within a day — which they plainly
        # are not, since intraday volatility is U-shaped — and it means
        # the pre-registered null of exactly 0.5 is not quite the right
        # null for SEAS-1.
        #
        # The comparison that does not depend on that assumption is
        # last-block vs random-block ON THE SAME DAY, which is paired,
        # so McNemar rather than two independent proportions.
        #
        # Reported openly as post-hoc. Promoting it into the family
        # after seeing the results is exactly the move pre-registration
        # exists to prevent, so it stays outside the BH correction and
        # outside every headline number.
        b = sum(1 for o in obs if o["seas1"] and not o["c1"])
        cc = sum(1 for o in obs if o["c1"] and not o["seas1"])
        pm = mcnemar_exact(b, cc)
        print(f"  [POST-HOC, not pre-registered, excluded from BH]")
        print(f"    SEAS-1 vs control C1, paired McNemar on the same days: "
              f"discordant {b}/{b+cc}, p={pm:.4f}")

        # Per-year, descriptive only.
        byyear = collections.defaultdict(list)
        for o in obs:
            byyear[o["date"].year].append(o)
        print(f"  per year (DESCRIPTIVE — excluded from the BH family, "
              f"cells are underpowered):")
        for yr in sorted(byyear):
            yo = byyear[yr]
            y1 = cell(sum(o["seas1"] for o in yo), len(yo), args.min_days, "")
            ky, ny, _ = agreement(yo, "orb", "rest")
            y2 = cell(ky, ny, args.min_days, "")
            r1 = ("insufficient" if y1["insufficient"]
                  else f"{y1['rate']*100:5.1f}%")
            r2 = ("insufficient" if y2["insufficient"]
                  else f"{y2['rate']*100:5.1f}%")
            print(f"      {yr}  n={len(yo):<4} SEAS-1 {r1:>12}   "
                  f"SEAS-2a {r2:>12}")

        # Crude out-of-sample split, per the memo's "out-of-sample" note.
        half = len(obs) // 2
        for name, part in (("first half", obs[:half]), ("second half", obs[half:])):
            h1 = cell(sum(o["seas1"] for o in part), len(part), args.min_days, "")
            kh, nh, _ = agreement(part, "orb", "rest")
            h2 = cell(kh, nh, args.min_days, "")
            r1 = ("insufficient" if h1["insufficient"]
                  else f"{h1['rate']*100:5.1f}%")
            r2 = ("insufficient" if h2["insufficient"]
                  else f"{h2['rate']*100:5.1f}%")
            print(f"      {name:<11} n={len(part):<4} SEAS-1 {r1:>12}   "
                  f"SEAS-2a {r2:>12}")

        results[sym] = {"range": [str(lo), str(hi)], "admitted": len(obs),
                        "dropped": dict(drops),
                        "seas1": s1, "c1": c1, "seas2a": s2a,
                        "seas2b": s2b, "c2": c2}
        if not s1["insufficient"]:
            family.append((f"{sym}:SEAS-1", s1["p"]))
        if not s2a["insufficient"]:
            family.append((f"{sym}:SEAS-2a", s2a["p"]))

    # ------------------------------------------------ multiple testing
    print(f"\n{'='*78}\nBENJAMINI-HOCHBERG across the pre-registered family "
          f"of {len(family)} tests, q={args.fdr}")
    print("(SEAS-2b and every per-year cell are excluded by design — see "
          "the pre-registration)")
    if not family:
        print("  no cell had enough days to enter the family")
    else:
        adj = benjamini_hochberg(family, q=args.fdr)
        for key, p in sorted(family, key=lambda kp: kp[1]):
            qv, rej = adj[key]
            print(f"  {key:<22} p={p:.4f}  q={qv:.4f}  "
                  f"{'survives' if rej else 'does not survive'} FDR {args.fdr}")

    print(f"\n{'='*78}")
    print("READ THIS BEFORE QUOTING ANY NUMBER ABOVE")
    print("  * This is a distribution report, not a verdict. The memo "
          "authorises\n    exactly that and nothing further.")
    print("  * A surviving cell is NOT a signal, NOT an edge and NOT "
          "tradeable. Costs\n    are not modelled here at all.")
    print("  * Compare every result against its control. A real "
          "time-of-day effect\n    must beat C1; a real ORB effect must "
          "beat C2. If a control scores\n    like the real thing, the "
          "real thing is the control.")
    print("  * SEAS-2b overlaps its own predictor by construction and "
          "will look\n    strong under a pure random walk. It is "
          "reported for comparability\n    with the memo's wording, "
          "not as evidence.")
    print("  * Phase 3 (attaching anything as a filter) is gated behind "
          "the memo's\n    review path. This script does not authorise "
          "it.")
    print("=" * 78)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
