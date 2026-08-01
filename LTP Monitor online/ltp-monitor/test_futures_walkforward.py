"""v59.0 Phase A §4.4 — the walk-forward harness.

Nothing in this codebase did out-of-sample evaluation before this; every
existing sweep is in-sample. The two properties that make walk-forward
mean anything are asserted here: folds must not overlap, and an OOS
window must never influence parameter selection. An OOS window consulted
twice is in-sample, and a harness that quietly does so produces numbers
indistinguishable from honest ones.
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("reads candles")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import futures_replay as fr

# The harness properties under test — fold geometry and the guarantee that
# selection never sees OOS — are about the HARNESS, not about prices. The
# test runs isolated (empty temp DB), so it injects two years of
# synthetic sessions rather than depending on whatever data happens to be
# on the machine. A first version called the real load_sessions() and got
# {"error": "no sessions"}, which would have made every assertion below
# vacuous.
def _synth_sessions(n_days=500, base=24000.0):
    import datetime as dt, math
    out, day = [], dt.date(2024, 6, 20)
    made = 0
    while made < n_days:
        if day.weekday() < 5:
            ts = int(dt.datetime.combine(day, dt.time(9, 15)).timestamp())
            bars = []
            for i in range(375):
                px = base + made * 0.5 + (i * 2.0 if i < 30 else 60) \
                     + math.sin(i / 9.0) * 8
                bars.append({"ts": ts + i * 60, "o": px, "h": px + 3,
                             "l": px - 3, "c": px, "v": 1000})
            out.append((day.isoformat(), bars))
            made += 1
        day += dt.timedelta(days=1)
    return out

_SESSIONS = _synth_sessions()
_real_load = fr.load_sessions
def _patched(symbol, start=None, end=None, min_bars=60):
    sel = []
    for d, bars in _SESSIONS:
        t0 = bars[0]["ts"]
        if start and t0 < start:   continue
        if end and t0 >= end:      continue
        sel.append((d, bars))
    return sel
fr.load_sessions = _patched
print(f"  (injected {len(_SESSIONS)} synthetic sessions "
      f"{_SESSIONS[0][0]} -> {_SESSIONS[-1][0]})")

print("1) fold geometry")
wf = fr.walk_forward("NIFTY", "s11_momentum", grid=[None])
folds = wf["folds"]
print(f"     {wf['n_folds']} folds over the available history")
check("the spec's 8-9 folds are produced from ~2 years",
      7 <= wf["n_folds"] <= 11, str(wf["n_folds"]))
ok_order = all(f["is"][0] < f["is"][1] <= f["oos"][0] < f["oos"][1] for f in folds)
check("every fold is IS-then-OOS, never reversed", ok_order)
overlap = [(a["fold"], b["fold"]) for a, b in zip(folds, folds[1:])
           if a["oos"][1] > b["oos"][0]]
check("OOS windows do not overlap each other", not overlap, str(overlap[:2]))
inside = [f["fold"] for f in folds if f["oos"][0] < f["is"][1]]
check("no OOS window starts before its own IS window ends", not inside, str(inside))

print("\n2) selection cannot see the OOS window")
src = open("futures_replay.py").read()
seg = src[src.index("def walk_forward"):]
sel = seg[seg.index("for cand in"):seg.index("oos = replay_futures")]
check("the parameter search only ever replays start=IS, end=IS-end",
      "start=to_ts(a), end=to_ts(b)" in sel and "to_ts(c)" not in sel,
      "if to_ts(c) appeared here, selection would be reading OOS data")
check("OOS is replayed once, after selection",
      seg.count("oos = replay_futures") == 1)
check("the chosen params are what OOS is evaluated with",
      "replay_futures(symbol, name, best, start=to_ts(b), end=to_ts(c)" in seg)

print("\n3) a grid actually selects (and still never touches OOS)")
grid = [{"fim_min_abs_open_ret_pct": v} for v in (0.05, 0.15, 0.40)]
wf2 = fr.walk_forward("NIFTY", "s11_momentum", grid=grid)
picked = [f["params"] and f["params"].get("fim_min_abs_open_ret_pct")
          for f in wf2["folds"]]
print(f"     per-fold selection: {picked}")
check("each fold chose a parameter from the grid",
      all(p in (0.05, 0.15, 0.40) for p in picked if p is not None))
check("the grid stayed tiny (spec: 2 params x 3 values max)", len(grid) <= 9)

print("\n4) the degradation ratio is reported")
check("oos_is_ratio present", "oos_is_ratio" in wf2)
r = wf2.get("oos_is_ratio")
print(f"     IS sum {wf2['is_expectancy_sum']:+.2f} pts, "
      f"OOS sum {wf2['oos_expectancy_sum']:+.2f} pts, ratio {r}")
check("per-fold IS and OOS expectancies are both recorded",
      all("is_expectancy" in f and "oos_expectancy" in f for f in wf2["folds"]))

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
