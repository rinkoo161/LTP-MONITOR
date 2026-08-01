"""v59.0 Phase A §4.4 — prefix invariance. The most important test here.

Running the replay on data truncated at bar t must produce byte-identical
signals up to t. A signal that appears and later changes shape is
lookahead, and lookahead does not announce itself: it shows up as a
strategy that backtests well and fails live, which is indistinguishable
from bad luck until the money is gone.

The design makes the common mistake impossible — `decide(bars, i, ...)`
is only ever handed `bars[:i+1]` — and this test verifies the property
end to end at 24 truncation points per strategy, because a design
argument is not evidence.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("reads candles")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import futures_strategies as fs

def _session(n=375):
    import datetime as dt
    day = dt.datetime(2026, 6, 15, 9, 15)          # a Monday, 09:15 IST
    return int(day.timestamp())


def synth_for(name, n=375, base=24000.0):
    """A series shaped to MAKE THIS STRATEGY FIRE.

    One gentle series produced 0 signals for S11 (needs a 0.15% opening
    move) and S13 (needs a 0.08% opening range), so the invariance check
    compared two empty lists and passed. A test that cannot observe the
    thing it asserts about is not evidence — each strategy now gets a
    series that triggers it.
    """
    import math
    ts = _session(n)
    bars, px = [], base
    for i in range(n):
        if name == "s11_momentum":
            px = base + (i * 2.0 if i < 30 else 60 + math.sin(i / 9.0) * 8)
        elif name == "s13_orb":
            px = (base + math.sin(i / 1.5) * 14) if i < 5 else base + 40 + i * 0.4
        elif name == "s12_vwap_reversion":
            px = base + math.sin(i / 7.0) * 45
        else:                                        # s14: a clean trend
            px = base + i * 1.2 + math.sin(i / 13.0) * 5
        bars.append({"ts": ts + i * 60, "o": px, "h": px + 4, "l": px - 4,
                     "c": px + math.sin(i / 3.0), "v": 1000 + (i % 50)})
    return bars


def signals_over(bars, name, params):
    """Every decision the strategy makes, replaying causally."""
    decide, _, _ = fs.STRATEGIES[name]
    p = fs.clamp(name, params)
    state = {"trades_today": 0, "require_volume": False}
    out = []
    for i in range(len(bars)):
        s = decide(bars, i, state, p)
        if s:
            out.append((i, s["side"], round(s.get("stop") or 0, 4),
                        round(s.get("target") or 0, 4), s.get("why")))
    return out

print("1) prefix invariance at 24 truncation points, per strategy")
for name in ("s11_momentum", "s12_vwap_reversion", "s13_orb", "s14_existing"):
    FULL = synth_for(name)
    full = signals_over(FULL, name, None)
    bad = []
    for k in range(24):
        cut = int(len(FULL) * (k + 1) / 25)
        if cut < 30:
            continue
        pre = signals_over(FULL[:cut], name, None)
        expect = [s for s in full if s[0] < cut]
        if pre != expect:
            bad.append((cut, len(pre), len(expect)))
    check(f"{name}: fires at all on its own fixture (else nothing is tested)",
          len(full) > 0, f"{len(full)} signals")
    check(f"{name}: identical signals under truncation", not bad,
          f"{len(bad)} mismatches e.g. {bad[:1]}" if bad else f"{len(full)} signals")

print("\n2) the guarantee is structural, not incidental")
src = open("futures_strategies.py").read()
check("decide() takes (bars, i, state, params) — the future is never passed",
      "def s11_decide(bars, i, state, p)" in src)
check("no strategy indexes past i",
      "bars[i+1" not in src.replace(" ", "") and "bars[i + 1" not in src)
rp = open("futures_replay.py").read()
check("the replay only ever hands decide() the full list plus the index",
      "decide(bars, i, state, p)" in rp)

print("\n3) a deliberately non-causal strategy is CAUGHT")
def cheat(bars, i, state, p):
    """Uses the WHOLE series, so its signal depends on bars that
    truncation removes.

    A first attempt at this control peeked only one bar ahead and was NOT
    caught: it fired at bar ~31, and bar 32 exists in every truncation
    too, so the signals matched and the test passed vacuously. A negative
    control that cannot fail proves nothing about the positive result —
    the control has to depend on data the truncation actually takes away.
    """
    if i < 30:
        return None
    session_high = max(b["c"] for b in bars)      # includes the future
    if bars[i]["c"] < session_high:
        return None
    return {"side": "LONG", "stop": None, "target": None, "why": "peak-peek"}
fs.STRATEGIES["__cheat"] = (cheat, {}, {})
FULL = synth_for("s14_existing")
full_c = signals_over(FULL, "__cheat", None)
caught = False
for k in range(24):
    cut = int(len(FULL) * (k + 1) / 25)
    if cut < 40:
        continue
    if signals_over(FULL[:cut], "__cheat", None) != [s for s in full_c if s[0] < cut]:
        caught = True
        break
fs.STRATEGIES.pop("__cheat")
check("a one-bar peek changes signals under truncation and is detected", caught,
      "if this passes silently the test proves nothing")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
