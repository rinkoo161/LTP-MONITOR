#!/usr/bin/env python3
"""test_gate_statistics.py — v59.66, third-eye Tier 1 fixes.

What broke and what these checks pin down:

  * live_enabled was a sign test (trades >= 15 and net > 0) while the
    statistical gate sat unreachably downstream. The gate itself now
    carries the statistics: deflated k, day-clustered sampling term,
    out-of-sample-only scoring. These checks exercise the BEHAVIOUR
    (call the functions, assert on verdicts), not the source text —
    a grep test cannot tell a formula from a comment about one.
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_gate_statistics")

import promotion_gate as pg
import trial_log

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- deflation: the bar reflects the size of the search -----------------
k0 = pg.deflation_k()
check("deflated k never sits below the pre-committed floor",
      k0 >= pg.PRECOMMITTED_K, f"k={k0:.3f}")
check("expected-max grows with the trial count",
      pg.expected_max_abs_t(10 ** 6) > pg.expected_max_abs_t(1000)
      > pg.expected_max_abs_t(10), "more trials tried => higher bar")
check("E[max|t|] of 1000 nulls is far above the single-test 2.0",
      pg.expected_max_abs_t(1000) > 3.0,
      f"{pg.expected_max_abs_t(1000):.3f} — a best-of-1000 at t=2 is noise")

# --- the free-parameter count is computed, not transcribed --------------
n_params = trial_log.free_param_count()
check("free_param_count is computed from the bounds dicts",
      n_params >= 60, f"{n_params} bounded params (the stale docstring said 37)")
check("summary() reports the search-space size",
      trial_log.summary().get("free_params") == n_params)

# --- day clustering: trades that share a day are not independent --------
# Same 60 trades, same per-trade sd. Case A: spread over 20 days.
# Case B: crammed into 4 days with the same day-level dispersion profile.
# The gate must demand more (or refuse) when the days are few.
okA, dA = pg.evaluate("x", "NIFTY", 60, 60 * 900.0, own_sd=2886,
                      n_days=20, day_sd=4000.0)
okB, dB = pg.evaluate("x", "NIFTY", 60, 60 * 900.0, own_sd=2886,
                      n_days=4, day_sd=4000.0)
check("too few independent days is DENIED, not scored",
      not okB and "independent day" in (dB.get("reason") or ""),
      dB.get("reason", ""))
check("enough days produces a scored verdict",
      dA.get("required") is not None, f"required=₹{dA.get('required')}")
check("the clustered SE uses the day as the unit",
      abs((dA["stat_margin"] / dA["k"]) ** 2
          - ((4000.0 * math.sqrt(20) / 60) ** 2
             + pg.PROXY_SD_PER_TRADE ** 2 / pg.CALIBRATION_N)) < 1e-6)
check("t_day is reported alongside t_own",
      dA.get("t_day") is not None and dA.get("t_own") is not None,
      f"t_day={dA.get('t_day'):.2f} vs t_trade={dA.get('t_own'):.2f}")
# Consistency anchor: when days are mere bundles of iid trades
# (day_sd² = trades_per_day × trade_var), the clustered SE must collapse
# to the plain per-trade SE — clustering only bites when days are MORE
# dispersed than iid bundling predicts, i.e. when trades genuinely
# share their day's regime.
_tpd = 60 / 20
_iid_day_sd = (_tpd ** 0.5) * 2886.0
_, dI = pg.evaluate("x", "NIFTY", 60, 60 * 900.0, own_sd=2886,
                    n_days=20, day_sd=_iid_day_sd)
_se_clustered = _iid_day_sd * math.sqrt(20) / 60
_se_per_trade = 2886.0 / math.sqrt(60)
check("iid-bundled days collapse to the per-trade SE",
      abs(_se_clustered - _se_per_trade) < 1e-9
      and abs(dI["stat_margin"]
              - dI["k"] * math.sqrt(_se_per_trade ** 2
                                    + pg.PROXY_SD_PER_TRADE ** 2
                                    / pg.CALIBRATION_N)) < 1e-6,
      f"SE {_se_clustered:.2f} == {_se_per_trade:.2f}")
check("day fields supplied but day_sd missing is DENIED",
      not pg.evaluate("x", "NIFTY", 60, 60 * 900.0, own_sd=2886,
                      n_days=20)[0])

# --- denial payloads carry the full key set (gate_report crash fix) -----
_need = ("headroom", "required", "stat_margin", "net_per_trade",
         "t_own", "t_day", "reason", "passes_stat_only")
_missing = []
for _, dd in (pg.evaluate("x", "NIFTY", 0, 0),
              pg.evaluate("x", "NIFTY", 60, 1000.0),          # no own_sd
              pg.evaluate("x", "NIFTY", 60, 1000.0, own_sd=100, n_days=2,
                          day_sd=50.0)):                       # too few days
    _missing += [k for k in _need if k not in dd]
check("every denial payload carries the full key set", not _missing,
      f"missing: {sorted(set(_missing))}" if _missing else "")

# --- evaluate_entry scores ONLY the out-of-sample window ----------------
full = {"trades": 300, "net_pnl": 300 * 900.0, "pnl_sd": 2886,
        "pnl_sd_day": 4000.0, "days_tested": 17}
ok1, e1 = pg.evaluate_entry("x", "NIFTY", dict(full))
check("no oos window is DENIED even when the full sample looks great",
      not ok1 and "out-of-sample" in (e1.get("reason") or ""),
      e1.get("reason", ""))
ok2, e2 = pg.evaluate_entry("x", "NIFTY",
                            dict(full, oos={"trades": 0, "window": "days after 2026-08-09"}))
check("an EMPTY oos window is DENIED",
      not ok2 and "out-of-sample" in (e2.get("reason") or ""))
good_oos = {"trades": 120, "net_pnl": 120 * 900.0, "pnl_sd": 2886,
            "pnl_sd_day": 3000.0, "days_tested": 15,
            "window": "days after 2026-07-01 (v3 adoption)"}
ok3, e3 = pg.evaluate_entry("x", "NIFTY", dict(full, oos=good_oos))
check("a strong oos window can pass", ok3,
      f"headroom {e3.get('headroom')}")
check("and the verdict is computed from the oos numbers, not the full set",
      e3.get("trades") == 120 and e3.get("window") == good_oos["window"])
bad_oos = dict(good_oos, net_pnl=120 * 5.0)
ok4, _ = pg.evaluate_entry("x", "NIFTY", dict(full, oos=bad_oos))
check("a weak oos window fails regardless of the in-sample number", not ok4)

# --- v59.72 (R2 findings M1/M2/M5/L1): the fail-open holes are closed ---
_no_days = {k: v for k, v in good_oos.items() if k != "days_tested"}
okD, dD = pg.evaluate_entry("x", "NIFTY", dict(full, oos=_no_days))
check("missing days_tested DENIES — no silent per-trade fallback (M1)",
      not okD and "day-clustered fields" in (dD.get("reason") or ""),
      dD.get("reason", ""))
okZ, dZ = pg.evaluate("x", "NIFTY", 60, 60 * 900.0, own_sd=2886,
                      n_days=20, day_sd=0.0)
check("a zero day_sd DENIES rather than zeroing the dispersion term (M2)",
      not okZ and "zero" in (dZ.get("reason") or ""), dZ.get("reason", ""))
_hand_2n = (math.sqrt(2 * math.log(2000))
            - (math.log(math.log(2000)) + math.log(4 * math.pi))
            / (2 * math.sqrt(2 * math.log(2000))))
check("E[max|t|] folds both tails (2n Gumbel form, L1)",
      abs(pg.expected_max_abs_t(1000) - _hand_2n) < 1e-9
      and pg.expected_max_abs_t(1000) > 3.25,
      f"{pg.expected_max_abs_t(1000):.4f}")
_d0 = trial_log.distinct_count()
import time as _time
_p = {"p": round(_time.time(), 3)}   # unique per run — a rerun against the
                                     # same store must not find this config
                                     # already in the distinct set
trial_log.record("x", "NIFTY", _p, {"trades": 1, "net_pnl": 0},
                 "daily_baseline")
trial_log.record("x", "NIFTY", _p, {"trades": 1, "net_pnl": 0},
                 "daily_baseline")
check("re-testing the SAME config does not inflate the deflation N (M5)",
      trial_log.distinct_count() == _d0 + 1,
      f"{_d0} -> {trial_log.distinct_count()} after 2 identical rows")

# --- metrics() emits the day-clustered field ----------------------------
import backtester as bt
m = bt.metrics([{"day": "2026-08-01", "pnl": 100.0, "reason": "target", "risk": 50},
                {"day": "2026-08-01", "pnl": -40.0, "reason": "stop", "risk": 50},
                {"day": "2026-08-02", "pnl": 500.0, "reason": "target", "risk": 50}])
check("metrics() records pnl_sd_day",
      m.get("pnl_sd_day") is not None and m.get("days_tested") == 2,
      f"pnl_sd_day={m.get('pnl_sd_day')}")
import statistics as _st
check("pnl_sd_day is the dispersion of per-DAY sums",
      abs(m["pnl_sd_day"] - round(_st.pstdev([60.0, 500.0]), 0)) < 1e-9)

# --- run_all: no path around _replay_for / trial_log --------------------
# Executable, not a grep: stub _replay_for and confirm run_all uses it
# for every strategy name, including sg_ema and ew_reversal (the two
# that replay_pa cannot dispatch and used to zero out).
called = []
_orig = bt._replay_for
def _stub(name, symbol, params, days=None, source="replay"):
    called.append((name, source))
    return [{"day": "2026-08-01", "pnl": 1.0, "reason": "x", "risk": 1}]
bt._replay_for = _stub
try:
    out = bt.run_all("NIFTY")
finally:
    bt._replay_for = _orig
import pa_strategies as pa
expected = {"bull_put_spread", "bear_call_spread", "momentum_buy"} | set(pa.PA_NAMES)
check("run_all evaluates every strategy through _replay_for",
      {n for n, _ in called} == expected,
      f"missing: {expected - {n for n, _ in called}}")
check("run_all baselines are recorded as trials (source set)",
      all(src == "daily_baseline" for _, src in called))
check("sg_ema and ew_reversal get real baselines, not zero-trade stubs",
      out["sg_ema"]["trades"] == 1 and out["ew_reversal"]["trades"] == 1)
check("every run_all result carries an oos window",
      all(isinstance(v.get("oos"), dict) and "window" in v["oos"]
          for v in out.values()))
check("oos sub-metrics drop the heavy display fields",
      all("trades_detail" not in v["oos"] and "equity_curve" not in v["oos"]
          for v in out.values()))

# --- small-n probability guard ------------------------------------------
import ai_probability_engine as ape
_sig = {"confidence": 72}
_dec = {"institutional_agreement": True, "technical_agreement": True}
_reg = {"regime": "trending"}
_trade = {"pnl": 1000, "entry_confidence": 74,
          "entry_institutional_agreement": True,
          "entry_technical_agreement": True,
          "entry_regime": "trending"}
few = [dict(_trade)] * (ape.MIN_SAMPLE - 1)
r = ape.estimate_probability(_sig, _dec, _reg, few)
check("below MIN_SAMPLE the probability is unavailable, not a number",
      r.get("unavailable") is True and r.get("probability_pct") is None,
      r.get("basis", ""))
enough = [dict(_trade)] * ape.MIN_SAMPLE
r2 = ape.estimate_probability(_sig, _dec, _reg, enough)
check("at MIN_SAMPLE it emits a (smoothed) number again",
      r2.get("unavailable") is False and r2.get("probability_pct") is not None,
      f"{r2.get('probability_pct')}% from n={r2.get('sample_size')}")

# --- Wilson bound: 5 samples cannot condemn a pattern -------------------
check("wilson upper bound: 0/5 wins does NOT rule out 35%",
      pg.wilson_upper(0, 5) >= 0.35, f"{pg.wilson_upper(0, 5):.3f}")
check("wilson upper bound: 0/10 wins DOES rule out 35%",
      pg.wilson_upper(0, 10) < 0.35, f"{pg.wilson_upper(0, 10):.3f}")
check("wilson upper bound: 30/100 at 30% cannot rule out 35% (correctly)",
      pg.wilson_upper(30, 100) >= 0.35, f"{pg.wilson_upper(30, 100):.3f}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all gate-statistics checks passed")
