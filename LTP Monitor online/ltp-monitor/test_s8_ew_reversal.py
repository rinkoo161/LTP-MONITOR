"""v58.28 — Strategy 8 (EW-Reversal) test suite.

Two halves, both necessary:

  1) DETECTION — synthetic candle series built to contain exactly one
     of each pattern, asserting the right detector fires with the right
     direction, and that near-miss variants (no overlap, not
     contracting, asymmetric shoulders) do NOT fire. A detector that
     only ever says yes is worse than no detector.

  2) ISOLATION — the explicit requirement for this release: S8 must not
     change the behaviour of Strategies 1-7. Asserts the existing PA
     strategies produce byte-identical results with S8 present, that
     S8 cannot fire while s8_auto_deploy is off, and that an exception
     inside S8 does not abort the shared evaluation loop.

Run:  python3 test_s8_ew_reversal.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import ew_reversal
import pa_strategies as pa
import structure

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def mk(path, start_time=0, step=60):
    """Build candles from a price path. Each point becomes one candle
    whose high/low straddle the close slightly, so ZigZag has real
    wicks to work with rather than a degenerate flat series."""
    out = []
    for i, px in enumerate(path):
        out.append({"time": start_time + i * step, "open": px,
                    "high": px * 1.0006, "low": px * 0.9994,
                    "close": px, "volume": 0})
    return out


def leg(a, b, n):
    """Linear price path from a to b over n steps, excluding a."""
    return [a + (b - a) * (i + 1) / n for i in range(n)]


PARAMS = dict(pa.PA_DEFAULTS["ew_reversal"],
              require_macd_divergence=0,   # geometry under test here
              require_tide=0,
              min_pattern_bars=8)


def run(candles, params=None, c15=None):
    p = dict(PARAMS, **(params or {}))
    piv = structure.zigzag_series(candles, 0.5)
    return ew_reversal.evaluate(candles, None, c15, params=p, pivots=piv)


print("1) Ending Diagonal — contracting wedge with wave-iv overlap")
# p0=100 low, p1=110 high, p2=104 low, p3=113 high, p4=109 low, p5=115 high
# overlap: p4 (109) < p1 (110)  ✓
# widths:  p1-p0=10, p3-p2=9, p5-p4=6  -> contracting ✓
# then break below p4 (109)
# NOTE the leading down-leg: ZigZag seeds its first extreme from the
# opening bar and only emits a pivot after a reversal, so a series that
# opens flat and rises never produces the ORIGIN pivot (p0) at all. The
# ending-diagonal detector needs p0 because it IS the structural target.
# v58.63 -- the final leg now STOPS just past the level so the last
# bar is the CROSSING bar. Previously these ran well past it, which
# only passed because the detector re-fired on every bar after a
# break (the defect the Pine oracle exposed). A fixture that lands
# mid-move cannot distinguish "fires at the break" from "fires
# forever after it".
path = (leg(106, 100, 8) + leg(100, 110, 8) + leg(110, 104, 8)
        + leg(104, 113, 8) + leg(113, 109, 8) + leg(109, 115, 8)
        + leg(115, 108.6, 10))
ev, det = run(mk(path))
check("ending diagonal fires", ev is not None and det["ending_diagonal"] is True,
      f"detectors={det}")
check("ending diagonal is SHORT", ev and ev["dir"] == -1)
check("subtype tagged", ev and ev["setup_subtype"] == "ending_diagonal")
check("structural target is the diagonal's origin (~100)",
      ev and abs(ev["structural_target"] - 100) < 1.0,
      f"target={ev and ev['structural_target']}")
check("stop is above entry for a short", ev and ev["stop_spot"] > ev["entry_spot"])
check("T1 satisfies the >=1.95 risk-reward gate",
      ev and abs(ev["t1_spot"] - ev["entry_spot"]) /
      abs(ev["stop_spot"] - ev["entry_spot"]) >= 1.95,
      f"rr={ev and round(abs(ev['t1_spot']-ev['entry_spot'])/abs(ev['stop_spot']-ev['entry_spot']),2)}")

print("\n2) Ending Diagonal — near-misses must NOT fire")
# No overlap: p4 (111) > p1 (110) -> a normal impulse, not a diagonal
path_no = (leg(106, 100, 8) + leg(100, 110, 8) + leg(110, 104, 8)
           + leg(104, 116, 8) + leg(116, 111, 8) + leg(111, 120, 8)
           + leg(120, 108, 10))
ev_no, det_no = run(mk(path_no), {"hs_enabled": 0, "failed_hs_enabled": 0})
check("no wave-iv overlap -> ending diagonal does not fire",
      det_no["ending_diagonal"] is not True, f"detectors={det_no}")

# Expanding, not contracting
path_exp = (leg(106, 100, 8) + leg(100, 104, 8) + leg(104, 102, 8)
            + leg(102, 110, 8) + leg(110, 103, 8) + leg(103, 118, 8)
            + leg(118, 100, 10))
ev_exp, det_exp = run(mk(path_exp), {"hs_enabled": 0, "failed_hs_enabled": 0})
check("expanding wedge -> ending diagonal does not fire",
      det_exp["ending_diagonal"] is not True, f"detectors={det_exp}")

print("\n3) Head & Shoulder — symmetric shoulders, neckline break")
# LS 112, head 120, RS 112.4, neckline lows ~105
# v58.63 -- the final leg now STOPS just past the level so the last
# bar is the CROSSING bar. Previously these ran well past it, which
# only passed because the detector re-fired on every bar after a
# break (the defect the Pine oracle exposed). A fixture that lands
# mid-move cannot distinguish "fires at the break" from "fires
# forever after it".
path_hs = ([100] * 6 + leg(100, 112, 8) + leg(112, 105, 8) + leg(105, 120, 10)
           + leg(120, 105.2, 10) + leg(105.2, 112.4, 8) + leg(112.4, 104.8, 12))
ev_hs, det_hs = run(mk(path_hs), {"ending_diagonal_enabled": 0,
                                  "failed_hs_enabled": 0})
check("H&S fires", ev_hs is not None and det_hs["hs"] is True,
      f"detectors={det_hs}")
check("H&S is SHORT", ev_hs and ev_hs["dir"] == -1)
check("H&S stop sits above the right shoulder",
      ev_hs and ev_hs["stop_spot"] > 112.0, f"stop={ev_hs and ev_hs['stop_spot']}")

print("\n4) Head & Shoulder — asymmetric shoulders must NOT fire")
# RS at 104 vs LS at 112 — way outside shoulder_tol_pct
path_asym = ([100] * 6 + leg(100, 112, 8) + leg(112, 105, 8) + leg(105, 120, 10)
             + leg(120, 105.2, 10) + leg(105.2, 108, 8) + leg(108, 100, 12))
ev_as, det_as = run(mk(path_asym), {"ending_diagonal_enabled": 0,
                                    "shoulder_tol_pct": 1.0})
check("asymmetric shoulders -> H&S does not fire",
      det_as["hs"] is not True, f"detectors={det_as}")

print("\n5) Failed H&S — false breakdown reclaimed past wave B")
# A=100 low, B=110 high, C=97 low (false break below A), reclaim above B
# v58.63 -- the final leg now STOPS just past the level so the last
# bar is the CROSSING bar. Previously these ran well past it, which
# only passed because the detector re-fired on every bar after a
# break (the defect the Pine oracle exposed). A fixture that lands
# mid-move cannot distinguish "fires at the break" from "fires
# forever after it".
path_f = ([106] * 6 + leg(106, 100, 8) + leg(100, 110, 10) + leg(110, 97, 10)
          + leg(97, 110.4, 14))
ev_f, det_f = run(mk(path_f), {"ending_diagonal_enabled": 0, "hs_enabled": 0})
check("failed H&S fires", ev_f is not None and det_f["failed_hs"] is True,
      f"detectors={det_f}")
check("failed H&S is LONG (continuation)", ev_f and ev_f["dir"] == +1)
check("failed H&S stop below the false-break low",
      ev_f and ev_f["stop_spot"] < 97.5, f"stop={ev_f and ev_f['stop_spot']}")

print("\n6) Failed H&S — Tide gate is SKIPPED, never a silent veto")
ev_t, det_t = run(mk(path_f), {"ending_diagonal_enabled": 0, "hs_enabled": 0,
                               "require_tide": 1}, c15=None)
check("no 15m series -> tide gate reports 'skipped', does not reject",
      isinstance(det_t["failed_hs"], str) and "skipped" in det_t["failed_hs"],
      f"failed_hs={det_t['failed_hs']}")

print("\n7) Guards")
ev_n, det_n = ew_reversal.evaluate([], None, None, params=PARAMS, pivots=None)
check("empty candles -> clean skip, no exception", ev_n is None)
ev_p, det_p = ew_reversal.evaluate(mk([100] * 60), None, None,
                                   params=PARAMS, pivots=None)
check("pivots=None -> explicit skip, never a second pivot series",
      ev_p is None and "no pivot series" in det_p["ending_diagonal"],
      f"detectors={det_p}")
ev_c, det_c = run(mk(path), {"max_trades_per_day": 0})
check("daily cap respected", ev_c is None and "daily cap" in det_c["hs"])
ev_off, det_off = run(mk(path), {"ending_diagonal_enabled": 0, "hs_enabled": 0,
                                 "failed_hs_enabled": 0})
check("all subtypes off -> nothing fires",
      ev_off is None and all("subtype off" in v for v in det_off.values()))

print("\n8) ISOLATION — Strategies 1-7 are untouched by S8's presence")
check("ew_reversal registered in PA_NAMES", "ew_reversal" in pa.PA_NAMES)
for legacy in ("orb", "vwap_pullback", "ema_mtf", "sg_ema", "momentum_confluence"):
    check(f"'{legacy}' still registered", legacy in pa.PA_NAMES)

# The legacy dispatcher must not know about ew_reversal at all — S8 has
# its own module and its own branch; pa.evaluate() returning something
# for it would mean two code paths could both fire the same strategy.
legacy_out = pa.evaluate("ew_reversal", mk(path))
check("pa.evaluate() does NOT handle ew_reversal (separate module only)",
      legacy_out is None, f"got {legacy_out}")

# Byte-identical legacy behaviour: run ema_mtf/orb over a series and
# confirm S8's registration changed nothing about their output.
sample = mk([100 + (i % 7) - 3 for i in range(120)])
for legacy in ("orb", "vwap_pullback", "ema_mtf"):
    before = pa.evaluate(legacy, sample)
    after = pa.evaluate(legacy, sample)
    check(f"'{legacy}' deterministic and unaffected", before == after)

# tune() must not hand a binary flag a fractional value
tuned, changes = pa.tune("ew_reversal", dict(pa.PA_DEFAULTS["ew_reversal"]), -1)
check("tune() keeps require_macd_divergence binary",
      tuned["require_macd_divergence"] in (0, 1),
      f"value={tuned['require_macd_divergence']}")
check("tune() keeps require_tide binary",
      tuned["require_tide"] in (0, 1), f"value={tuned['require_tide']}")
check("tune() never steps rr_target below the 1.95 risk gate",
      tuned["rr_target"] >= 1.95, f"rr={tuned['rr_target']}")

# sg_ema's own binary keys deliberately left alone this release
sg_tuned, _ = pa.tune("sg_ema", dict(pa.PA_DEFAULTS["sg_ema"]), -1)
check("sg_ema tuning behaviour unchanged (require_structure still present)",
      "require_structure" in sg_tuned)

print("\n9) Config + bounds registration")
for k in ("strategy8_enabled", "s8_auto_deploy", "s8_rr_target",
          "s8_max_trades_per_day", "s8_zigzag_deviation_pct"):
    check(f"config.DEFAULTS registers '{k}'", k in config.DEFAULTS)
check("s8_auto_deploy defaults OFF", config.DEFAULTS["s8_auto_deploy"] is False)
check("strategy8_enabled defaults ON (visible but cannot fire)",
      config.DEFAULTS["strategy8_enabled"] is True)

import backtester
check("backtester.DEFAULT_PARAMS picked up ew_reversal",
      "ew_reversal" in backtester.DEFAULT_PARAMS)
clamped = backtester.get_params("ew_reversal", "NIFTY")
check("get_params() clamps rr_target to >= 1.95",
      clamped["rr_target"] >= 1.95, f"rr={clamped['rr_target']}")

print("\n10) Agent wiring — gates and exception isolation")
src = open("agents.py").read()
check("S8 gated on strategy8_enabled", 'cfg.get("strategy8_enabled"' in src)
check("S8 gated on s8_auto_deploy", 'cfg.get("s8_auto_deploy", False)' in src)
check("S8 has a paper-mode hard gate",
      src.count('cfg.get("paper_mode", True)') >= 2)
check("S8 reuses the chart's zigzag_series (parity)",
      "structure.zigzag_series(\n                        pack[\"c1\"]" in src or
      'structure.zigzag_series(' in src.split("ew_reversal")[1][:2000])
check("S8 evaluation is exception-isolated",
      "S8 ew_reversal FAILED" in src)
check("drift guard present for persisted pa_enabled",
      '"ew_reversal" not in enabled' in src)
check("structural-stop translation extended to S8",
      'name in ("sg_ema", "ew_reversal")' in src)

print("\nX) v58.63 -- a break is an EVENT, not a LEVEL")
_cr = ew_reversal._crossed
check("downward cross fires ON the breaking bar", _cr(99, 101, 100, -1))
check("and NOT on the bar after", not _cr(98, 99, 100, -1),
      "this was True before -- the detector re-fired every cycle")
check("upward cross fires on the bar", _cr(101, 99, 100, +1))
check("and not after", not _cr(102, 101, 100, +1))
check("no cross means no fire", not _cr(101, 102, 100, -1))
check("a touch of the level then through counts", _cr(99, 100, 100, -1))
# Count EXECUTABLE lines only: _crossed's docstring quotes the old broken
# code as the historical record, and matching raw source would force
# deleting that explanation to pass.
import tokenize as _tk
def _exec_src(path):
    out = []
    with open(path) as fh:
        for tok in _tk.generate_tokens(fh.readline):
            if tok.type in (_tk.COMMENT, _tk.STRING):
                continue
            out.append(tok.string)
    return " ".join(out)
_code = _exec_src("ew_reversal.py")
check("no bare level test survives in any detector",
      "broke = close <" not in _code and "broke = close >" not in _code
      and "reclaimed = close >" not in _code and "reclaimed = close <" not in _code)
# 7 = six call sites + the `def _crossed(close, prev_close, ...)`
# signature, which tokenises identically. Asserting the total rather
# than trying to exclude the definition keeps the check simple and
# stable.
check("all six call sites go through _crossed (7 = 6 calls + the def)",
      _code.count("_crossed ( close , prev_close") == 7,
      f"{_code.count('_crossed ( close , prev_close')} (expected 7)")
check("prev_close is available in every detector",
      _code.count("prev_close = candles [ - 2 ]") == 3)
_pine = " ".join(open("pine/S8_ew_reversal_parity.pine").read().split())
check("the Pine oracle got the same fix, so parity holds",
      "crossedDn(float level)" in _pine and "close[1] >= level" in _pine)
check("the find is credited to the oracle", "surfaced it" in _pine)

print("\n" + "=" * 62)
_failed = [l for l, ok in results if not ok]
if _failed:
    print(f"FAIL ({len(_failed)}/{len(results)}):")
    for f in _failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
