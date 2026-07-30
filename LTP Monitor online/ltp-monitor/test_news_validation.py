"""v58.51 — roadmap B7: has the news impact classifier ever been right?

Two questions never asked of real data:
  1. When classify_impact_window() says a headline affects the 5m
     candle, does the 5m candle actually move more than usual?
  2. Of the ~9 RSS feeds, which precede movement and which are noise?

The measurement design matters more than the code. Absolute moves are
meaningless — NIFTY moves ~0.05% in a typical 5m window, so a headline
followed by 0.06% looks like a hit and is noise. Every event is scored
as a PERCENTILE RANK against all same-length windows on the SAME DAY,
and events the classifier said would NOT move price are the control
group. Without a control there is no way to tell a working classifier
from one that labels everything.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import news_validation as nv

DAY = "2026-07-29"
T0 = int(time.mktime(time.strptime(DAY + " 09:15", "%Y-%m-%d %H:%M")))

def candles(spike_bars=(100, 101, 102, 200, 201, 202), amp=60, quiet=4):
    out = []
    for i in range(375):
        base = 24000 + i * 0.2
        a = amp if i in spike_bars else quiet
        out.append({"ts": T0 + i * 60, "open": base, "high": base + a,
                    "low": base - a, "close": base})
    return out

C = candles()

print("1) Range, not close-to-close")
m = nv._window_move_pct(C, T0 + 100 * 60, 5)
q = nv._window_move_pct(C, T0 + 300 * 60, 5)
check("a spike window measures larger than a quiet one", m > q, f"{m:.3f} vs {q:.3f}")
check("a spike that reverts still counts as movement", m > 0,
      "close-to-close would score a spike-and-revert as nothing")
check("window outside the session -> None",
      nv._window_move_pct(C, T0 + 9999 * 60, 5) is None)

print("\n2) Percentile ranking against a same-day baseline")
base = nv._baseline_distribution(C, 5)
check("baseline is built", len(base) > 20, f"{len(base)} windows")
check("baseline is sorted", base == sorted(base))
check("a spike ranks high", nv._percentile_rank(base, m) >= 90,
      f"{nv._percentile_rank(base, m)}")
check("a quiet window ranks low", nv._percentile_rank(base, q) <= 60,
      f"{nv._percentile_rank(base, q)}")
check("empty baseline -> None", nv._percentile_rank([], 1.0) is None)

print("\n3) The control group exists and is used")
ev = [
    {"fetched_ts": T0 + 100 * 60, "impact_windows": ["5m"], "source": "Good", "title": "RBI"},
    {"fetched_ts": T0 + 200 * 60, "impact_windows": ["5m"], "source": "Good", "title": "Fed"},
    {"fetched_ts": T0 + 300 * 60, "impact_windows": ["5m"], "source": "Noise", "title": "celeb"},
    {"fetched_ts": T0 + 320 * 60, "impact_windows": ["5m"], "source": "Noise", "title": "sport"},
    {"fetched_ts": T0 + 150 * 60, "impact_windows": [],     "source": "Good", "title": "routine"},
    {"fetched_ts": T0 + 250 * 60, "impact_windows": [],     "source": "Noise", "title": "routine"},
]
r = nv.validate(ev, {DAY: C}, min_samples=2)
check("classified events are scored", not r["overall_classified"]["insufficient"])
check("UNCLASSIFIED events form a control group",
      not r["control_unclassified"]["insufficient"],
      "without a control, a classifier that labels everything looks perfect")
check("verdict compares the two", "vs control" in r["verdict"], r["verdict"])
check("verdict quantifies the lift", "lift" in r["verdict"])

print("\n4) Per-feed scoring is the objective 'review the feeds'")
f = r["by_feed"]
check("a feed whose flags land on real moves scores high",
      f["Good"]["median_rank"] >= 65, str(f["Good"]))
check("and is labelled as adding signal", f["Good"]["verdict"] == "adds signal")
check("a feed whose flags land in quiet windows scores low",
      f["Noise"]["median_rank"] < 55, str(f["Noise"]))
check("and is labelled no better than random",
      f["Noise"]["verdict"] == "no better than random")
check("item and classified counts are reported",
      f["Good"]["items"] == 3 and f["Good"]["classified"] == 2)

print("\n5) Sparse data says so rather than inventing a number")
r2 = nv.validate(ev[:1], {DAY: C}, min_samples=5)
check("below min_samples -> insufficient, not a fabricated median",
      r2["overall_classified"]["insufficient"])
check("the verdict admits it", r2["verdict"] == "insufficient data")
check("a feed below min_samples is flagged insufficient",
      all(v.get("insufficient") for v in r2["by_feed"].values()))
r3 = nv.validate([], {}, min_samples=5)
check("no events at all -> no crash", isinstance(r3, dict))
check("events without a timestamp are counted, not silently dropped",
      nv.validate([{"impact_windows": ["5m"]}], {DAY: C})["skipped"]["no_ts"] == 1)
check("events with no candles for their day are counted",
      nv.validate([{"fetched_ts": T0, "impact_windows": ["5m"]}], {})
      ["skipped"]["no_candles"] == 1)

print("\n6) The lag limitation is stated, not buried")
check("caveat is part of the RESULT, not only a docstring",
      "fetched_ts" in r["caveat"] and "0-15min" in r["caveat"])
check("it names the direction of the bias",
      "AGAINST the classifier" in r["caveat"],
      "a null result is inconclusive; a positive one is meaningful")
APP = open("app.py").read()
check("endpoint exists", "/api/news/impact-validation" in APP)
check("endpoint explains the control group", "control group" in APP)
check("CLI entry point exists", '__name__ == "__main__"' in open("news_validation.py").read())

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f2 in failed: print("  - " + f2)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
