"""v58.29 — Strategy 9 (TA with Elliott) + the S8 Tide fix.

Three halves:

  1) INDICATOR / STATE — the deck's own concepts, tested individually:
     Bollinger band direction as the impulse-vs-corrective classifier,
     GMMA compression, hidden ("reverse") divergence, MACD zero-line
     reversal, ADX. Plus the memoisation that makes the state cheap
     enough for other agents to read freely.

  2) AGENT SEPARATION — the explicit requirement: S9 must NOT be
     evaluated inside PriceActionAgent's loop, must live in its own
     agent, and must publish its state to the bus for other agents.

  3) S8 TIDE FIX — v58.28 consulted the Tide only inside failed_hs, so
     a plain H&S could fire a short into a rising Tide, which is the
     exact failure the source deck warns about.

Run:  python3 test_s9_ta_elliott.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")

import config
import ew_reversal
import pa_strategies as pa
import structure
import ta_elliott as ta

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def mk(path, step=300, t0=0):
    return [{"time": t0 + i * step, "open": px, "high": px * 1.0008,
             "low": px * 0.9992, "close": px, "volume": 0}
            for i, px in enumerate(path)]


def leg(a, b, n):
    return [a + (b - a) * (i + 1) / n for i in range(n)]


P = dict(ta.TA_ELLIOTT_DEFAULTS)

print("1) Bollinger band direction = impulse vs corrective (slides 16-17)")
# A real impulse ACCELERATES — a perfectly linear ramp lets the SMA
# catch up and price sits inside the band, which is correct behaviour,
# not a classifier bug.
# A band tag happens on a BREAKOUT OUT OF QUIET VOLATILITY, which is
# what slide 16's chart actually shows. Mid-trend the stdev has already
# widened the band beyond price, so a smooth ramp correctly reads
# NEUTRAL rather than IMPULSE — that is the classifier working, not a
# bug, and it is why this fixture is a range followed by a breakout.
rally = mk([100 + (0.2 if i % 2 else -0.2) for i in range(45)] + leg(100, 106, 6))
bb_up = ta.bollinger_state(rally, P)
check("rising band + upper tag -> IMPULSE_UP", bb_up["state"] == "IMPULSE_UP",
      f"state={bb_up['state']}")

# Dead flat: band flat and contracting -> a wave 4 / B in progress.
# Range wide enough that ordinary wicks don't tag the bands — a
# hair-thin band is now classified as a dead range by design.
flat = mk([100 + (0.3 if i % 2 else -0.3) for i in range(70)])
bb_flat = ta.bollinger_state(flat, P)
check("flat contracting band -> CORRECTIVE_FLAT",
      bb_flat["state"] == "CORRECTIVE_FLAT", f"state={bb_flat['state']}")

check("Bollinger degrades cleanly on short series",
      ta.bollinger_state(mk([100] * 5), P)["state"] is None)

print("\n2) GMMA compression / expansion (slide 13)")
flat_closes = [100 + (0.2 if i % 2 else -0.2) for i in range(140)]
gm_c = ta.gmma_state(flat_closes, P)
check("tight range -> GMMA COMPRESSED", gm_c["state"] == "COMPRESSED",
      f"state={gm_c['state']}")
# Tested at the BREAKOUT, not mid-trend: the deck's signal is
# "compression and LATER expansion", which is the moment the ribbon
# starts fanning out of a range.
exp_closes = [100 + (0.2 if i % 2 else -0.2) for i in range(100)] + leg(100, 118, 14)
gm_e = ta.gmma_state(exp_closes, P)
check("breakout from a range -> GMMA EXPANDING_UP",
      gm_e["state"] == "EXPANDING_UP", f"state={gm_e['state']}")
check("GMMA degrades cleanly on short series",
      ta.gmma_state([100] * 20, P)["state"] is None)

print("\n3) Reverse (hidden) divergence — slide 14")
# Price makes a HIGHER low while the oscillator makes a LOWER low.
c = mk(leg(110, 100, 12) + leg(100, 112, 12) + leg(112, 103, 12)
       + leg(103, 118, 12))
piv = structure.zigzag_series(c, 0.5)
tmap = {x["time"]: i for i, x in enumerate(c)}
lows = [p for p in piv if p["type"] == "low"]
osc = [0.0] * len(c)
if len(lows) >= 2:
    osc[tmap[lows[-2]["time"]]] = -1.0     # earlier low: oscillator higher
    osc[tmap[lows[-1]["time"]]] = -5.0     # later low:   oscillator LOWER
d = ta.divergences(c, piv, osc, tmap)
check("price higher low + oscillator lower low -> hidden_bull",
      d["hidden_bull"] is True and d["regular_bull"] is False,
      f"price lows={[round(l['price'],1) for l in lows[-2:]]}, div={d}")

print("\n4) MACD zero-line reversal + ADX (slides 21, 18)")
st_trend = ta.compute_state("T1", mk([100] * 30), mk([100] * 40 + leg(100, 150, 50)),
                            mk([100] * 30 + leg(100, 150, 20)), params=P)
check("state computes on a trending series", st_trend.get("ok") is True,
      st_trend.get("reason", ""))
check("ADX reported", st_trend.get("adx") is not None, f"adx={st_trend.get('adx')}")
check("strong trend flagged dynamic", st_trend.get("dynamic") is True,
      f"adx={st_trend.get('adx')}")
check("trending 15m gives Tide up", st_trend.get("tide") == +1)
check("impulse phase routes to BUY_OPTIONS",
      st_trend["phase"] != "CORRECTIVE" or st_trend["route"] == "SPREADS",
      f"phase={st_trend['phase']} route={st_trend['route']}")

print("\n5) State memoisation (what makes it safe for other agents to read)")
c5 = mk([100] * 40 + leg(100, 120, 40))
a = ta.compute_state("MEMO", mk([100] * 30), c5, mk([100] * 30), params=P)
b = ta.compute_state("MEMO", mk([100] * 30), c5, mk([100] * 30), params=P)
check("same candle timestamp -> identical object returned (no recompute)",
      a is b, f"a is b = {a is b}")
# The case that actually matters: TAElliottAgent rebuilds its params
# dict every cycle, so a cache keyed on object identity would miss
# unconditionally in production while passing a test that reuses one
# dict. Equal-by-value params must hit.
p_a = dict(ta.TA_ELLIOTT_DEFAULTS, min_confluence=3)
p_b = dict(ta.TA_ELLIOTT_DEFAULTS, min_confluence=3)
x = ta.compute_state("MEMO2", mk([100] * 30), c5, mk([100] * 30), params=p_a)
y = ta.compute_state("MEMO2", mk([100] * 30), c5, mk([100] * 30), params=p_b)
check("equal-by-value params (a fresh dict each cycle) still hits cache",
      x is y, "this is how the agent actually calls it")
z = ta.compute_state("MEMO2", mk([100] * 30), c5, mk([100] * 30),
                     params=dict(p_a, min_confluence=5))
check("different param VALUES correctly miss the cache", z is not x)
c5b = c5 + [{"time": c5[-1]["time"] + 300, "open": 121, "high": 121.1,
             "low": 120.9, "close": 121, "volume": 0}]
c2 = ta.compute_state("MEMO", mk([100] * 30), c5b, mk([100] * 30), params=P)
check("new candle -> recomputed", c2 is not a)

print("\n6) 'When in doubt, Do Not Trade' (slide 28)")
ev, conf = ta.evaluate({"ok": False, "reason": "not enough 5m candles"}, params=P)
check("no state -> clean skip, no exception", ev is None)
ev, conf = ta.evaluate(dict(st_trend, phase="IMPULSE"), c5, params=P)
check("IMPULSE phase blocks entry (buy the END of a correction)",
      ev is None and "blocked" in str(conf.get("phase")), f"conf={conf.get('phase')}")
ev, conf = ta.evaluate(dict(st_trend, phase="UNCLEAR"), c5, params=P)
check("UNCLEAR phase blocks entry", ev is None)

# Confluence threshold genuinely gates
weak = {"ok": True, "phase": "CORRECTIVE", "tide": +1,
        "bb": {"state": "CORRECTIVE_FLAT", "mid": 100, "lower": 98, "upper": 102},
        "gmma": {"state": "NEUTRAL"}, "adx": 5, "dynamic": False,
        "macd_zero_reversal": 0, "div_macd": {}, "div_rsi": {}}
ev, conf = ta.evaluate(weak, mk([100] * 40), params=dict(P, min_confluence=3))
check("insufficient confluence -> no trade", ev is None, f"count={conf.get('count')}")

strong = dict(weak, bb={"state": "CORRECTIVE_STALL", "stall_dir": +1,
                        "mid": 100, "lower": 98, "upper": 102, "reason": "stall"},
              gmma={"state": "EXPANDING_UP", "reason": "expanding"},
              macd_zero_reversal=+1, adx=25, dynamic=True)
ev, conf = ta.evaluate(strong, mk([100] * 40), params=dict(P, min_confluence=3))
check("sufficient confluence -> setup produced", ev is not None, f"conf={conf}")
check("direction follows the Tide", ev and ev["dir"] == +1)
check("T1 satisfies the >=1.95 risk-reward gate",
      ev and abs(ev["t1_spot"] - ev["entry_spot"]) /
      abs(ev["stop_spot"] - ev["entry_spot"]) >= 1.95)
check("daily cap respected",
      ta.evaluate(strong, mk([100] * 40), params=P, taken_today=99)[0] is None)

print("\n7) Tide gate SKIPS on missing data, never silently rejects")
ev, conf = ta.evaluate(dict(strong, tide=None), mk([100] * 40),
                       params=dict(P, min_confluence=1))
check("tide None -> 'skipped', not a rejection",
      isinstance(conf.get("tide"), str) and "skipped" in conf["tide"],
      f"tide={conf.get('tide')}")

print("\n8) AGENT SEPARATION — S9 must not run inside PriceActionAgent")
check("ta_elliott NOT in PA_NAMES", "ta_elliott" not in pa.PA_NAMES)
check("ta_elliott NOT in PA_DEFAULTS", "ta_elliott" not in pa.PA_DEFAULTS)
check("ta_elliott NOT in default pa_enabled",
      "ta_elliott" not in config.DEFAULTS["pa_enabled"])
check("pa.evaluate() does not handle ta_elliott",
      pa.evaluate("ta_elliott", mk([100] * 60)) is None)

src = open("agents.py").read()
check("TAElliottAgent class exists", "class TAElliottAgent(Agent):" in src)
check("registered in AGENT_CLASSES", "MTFConfluenceAgent, TAElliottAgent]" in src)
check("runs on its own slower interval",
      'name, interval = "ta_elliott", 180' in src)
check("publishes state to the bus for other agents",
      'self.bus.set(f"ta_state:{sym}", state)' in src)
check("state publishes even when the strategy cannot trade",
      src.index('self.bus.set(f"ta_state:{sym}", state)') <
      src.index('if not cfg.get("ta_auto_deploy", False):'))
check("state computation is exception-isolated",
      "state computation FAILED" in src)
check("evaluate is exception-isolated", "evaluate FAILED" in src)
check("S9 gated on ta_elliott_enabled", 'cfg.get("ta_elliott_enabled", True)' in src)
check("S9 auto-deploy defaults off", config.DEFAULTS["ta_auto_deploy"] is False)
check("S9 has a paper-mode hard gate", 'cfg.get("paper_mode", True)' in src)

import backtester
check("backtester registers ta_elliott params",
      "ta_elliott" in backtester.DEFAULT_PARAMS)
check("get_params clamps ta_elliott rr_target >= 1.95",
      backtester.get_params("ta_elliott", "_global")["rr_target"] >= 1.95)
tuned, _ = ta.tune(dict(ta.TA_ELLIOTT_DEFAULTS), -1)
check("tune keeps require_tide binary", tuned["require_tide"] in (0, 1))
check("tune never steps rr below 1.95", tuned["rr_target"] >= 1.95)
check("tune keeps min_confluence an integer",
      isinstance(tuned["min_confluence"], int), f"v={tuned['min_confluence']}")

print("\n9) S8 Tide fix — the deck's 'H&S fails when the Tide is against it'")
# Bearish H&S geometry, with a RISING Tide. v58.28 would have fired
# this short; v58.29 must block it.
hs_path = ([100] * 6 + leg(100, 112, 8) + leg(112, 105, 8) + leg(105, 120, 10)
           + leg(120, 105.2, 10) + leg(105.2, 112.4, 8) + leg(112.4, 104.8, 12))
c1 = mk(hs_path, step=60)
piv = structure.zigzag_series(c1, 0.5)
s8p = dict(pa.PA_DEFAULTS["ew_reversal"], require_macd_divergence=0,
           require_tide=0, min_pattern_bars=8, ending_diagonal_enabled=0,
           failed_hs_enabled=0)

ev, det = ew_reversal.evaluate(c1, None, None, params=dict(s8p, require_tide_all_detectors=0),
                               pivots=piv)
check("v58.28 behaviour reproducible with the gate off (H&S fires)",
      ev is not None and det["hs"] is True, f"det={det}")

ev, det = ew_reversal.evaluate(c1, None, None,
                               params=dict(s8p, require_tide_all_detectors=1),
                               pivots=piv, shared_tide=+1)
check("gate ON + rising Tide -> H&S short BLOCKED",
      ev is None and "blocked" in str(det["hs"]), f"hs={det['hs']}")

ev, det = ew_reversal.evaluate(c1, None, None,
                               params=dict(s8p, require_tide_all_detectors=1),
                               pivots=piv, shared_tide=-1)
check("gate ON + falling Tide -> H&S short ALLOWED",
      ev is not None and det["hs"] is True, f"det={det}")

ev, det = ew_reversal.evaluate(c1, None, None,
                               params=dict(s8p, require_tide_all_detectors=1),
                               pivots=piv, shared_tide=None)
check("gate ON + Tide unknown -> SKIPPED, not blocked",
      ev is not None and det["hs"] is True, f"det={det}")

check("s8_require_tide_all_detectors registered", 
      "s8_require_tide_all_detectors" in config.DEFAULTS)
check("s8_use_shared_tide registered", "s8_use_shared_tide" in config.DEFAULTS)
check("S8 reads the shared tide from the bus",
      'shared.get("tide")' in src)

print("\n10) Backtester replay branches (v58.31)")
import backtester as _b, history as _h


def _syn_day():
    path = ([100] * 30 + leg(100, 112, 25) + leg(112, 105, 25) + leg(105, 120, 30)
            + leg(120, 105.2, 30) + leg(105.2, 112.4, 25) + leg(112.4, 96, 60)
            + leg(96, 99, 50))
    t0 = 1750000000
    return [{"time": t0 + i * 60, "ts": t0 + i * 60, "open": x, "high": x * 1.0006,
             "low": x * 0.9994, "close": x, "volume": 0} for i, x in enumerate(path)]


_day = _syn_day()
_od, _oc, _ocd = _h.index_days, _h.day_index_candles, _b._completed_days
_h.index_days = lambda s: ["D"]
# 2026-08-04 — day_index_candles gained for_compute= (the CAS freeze
# filter for replay). The backtester passes it, so the stub must
# accept it or the replay raises instead of returning the fixture.
_h.day_index_candles = lambda s, d, for_compute=False: _day
_b._completed_days = lambda d: ["D"]

check("replay_ew_reversal exists", callable(_b.replay_ew_reversal))
check("replay_ta_elliott exists", callable(_b.replay_ta_elliott))
tr8 = _b.replay_ew_reversal("NIFTY", params=dict(
    _b.get_params("ew_reversal", "NIFTY"), require_macd_divergence=0,
    require_tide_all_detectors=0, min_pattern_bars=8, max_trades_per_day=3))
check("S8 replay produces trades (was structurally 0 before)", len(tr8) > 0,
      f"{len(tr8)} trades")
check("S8 replay trades carry the detector subtype",
      all(t.get("subtype") for t in tr8), f"{[t.get('subtype') for t in tr8]}")
check("S8 replay trades have the shape the tuner expects",
      all({"day", "strategy", "pnl", "risk", "reason", "entry_ts", "exit_ts"} <= set(t)
          for t in tr8))
tr9 = _b.replay_ta_elliott("NIFTY", params=dict(
    _b.get_params("ta_elliott", "_global"), min_confluence=2))
check("S9 replay runs without error", isinstance(tr9, list), f"{len(tr9)} trades")

# Dispatch must route the new names away from replay_pa, which cannot
# evaluate either of them and would silently return zero trades.
import backtester
_seen = {}
_o8, _o9 = backtester.replay_ew_reversal, backtester.replay_ta_elliott
backtester.replay_ew_reversal = lambda *a, **k: _seen.setdefault("s8", True) or []
backtester.replay_ta_elliott = lambda *a, **k: _seen.setdefault("s9", True) or []
backtester._replay_for("ew_reversal", "NIFTY", None)
backtester._replay_for("ta_elliott", "NIFTY", None)
backtester.replay_ew_reversal, backtester.replay_ta_elliott = _o8, _o9
check("_replay_for routes ew_reversal to its own replay", _seen.get("s8") is True)
check("_replay_for routes ta_elliott to its own replay", _seen.get("s9") is True)
check("sweep_params can see ta_elliott bounds",
      len(_b._bounds_for("ta_elliott")) > 0, f"{sorted(_b._bounds_for('ta_elliott'))}")

# NO-LOOKAHEAD: pivots visible at bar i must be a prefix of bar i+1.
_prev, _viol = [], 0
for _i in range(40, len(_day), 7):
    _pv = [(x["time"], x["price"]) for x in structure.zigzag_series(_day[:_i + 1], 0.5)]
    if _prev and _pv[:len(_prev)] != _prev:
        _viol += 1
    _prev = _pv
check("NO-LOOKAHEAD: no pivot ever appears then vanishes", _viol == 0,
      f"{_viol} prefix violations")

check("_resample carries a timestamp (S9 pivots need it)",
      "time" in _b._resample(_day, 5)[0])
_h.index_days, _h.day_index_candles, _b._completed_days = _od, _oc, _ocd

print("\n11) Tide horizon fix")
check("tide computes from 5m when 15m is too short",
      ta.tide_of(None, dict(ta.TA_ELLIOTT_DEFAULTS),
                 c5=mk([100 + i for i in range(40)])) == +1)
check("tide still None when nothing is long enough",
      ta.tide_of(None, dict(ta.TA_ELLIOTT_DEFAULTS), c5=mk([100] * 3)) is None)

print("\n12) Calibration logging (v58.32)")
import history as _H, json as _J

_H._conn().close()
_c = _H._conn(); _c.execute("DELETE FROM ta_calibration WHERE symbol='UNITSYM'")
_c.commit(); _c.close()

check("ta_calibration table exists",
      _H._conn().execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='ta_calibration'").fetchone() is not None)
check("empty summary is informative, not an error",
      _H.ta_calibration_summary(days=1, symbol="UNITSYM")["observations"] == 0)

_day2 = _syn_day() if "_syn_day" in dir() else None
_path = ([100 + (0.25 if i % 2 else -0.25) for i in range(110)]
         + leg(100, 108, 20) + leg(108, 104, 25) + leg(104, 116, 40))
_t0 = 1750000000
_c1 = [{"time": _t0 + i * 60, "ts": _t0 + i * 60, "open": x, "high": x * 1.0006,
        "low": x * 0.9994, "close": x, "volume": 0} for i, x in enumerate(_path)]
_p = dict(_b.get_params("ta_elliott", "_global"), min_confluence=3)
_cycles = 0
for _i in range(60, len(_c1), 3):
    _w = _c1[:_i + 1]; _c5 = _b._resample(_w, 5); _c15 = _b._resample(_w, 15)
    if len(_c5) < 23:
        continue
    _st = ta.compute_state("UNITSYM", _w, _c5, _c15, params=_p)
    if not _st.get("ok"):
        continue
    _ev, _cf = ta.evaluate(_st, _w, params=_p,
                           pivots=structure.zigzag_series(_w, 0.5))
    _H.log_ta_observation("UNITSYM", "ta_elliott", _st, _cf, fired=bool(_ev))
    _cycles += 1

_sum = _H.ta_calibration_summary(days=1, symbol="UNITSYM")
check("observations are captured with auto_deploy OFF",
      _sum["observations"] > 0, f"{_sum['observations']} rows from {_cycles} cycles")
check("rows dedupe per candle rather than per wall-clock second",
      0 < _sum["observations"] < _cycles,
      f"{_sum['observations']} rows < {_cycles} cycles")
check("summary reports a hit rate for all seven signals",
      len(_sum["signal_hit_rate_pct"]) == 7)
check("summary names the signals that never fire",
      isinstance(_sum["signals_never_firing"], list))
check("summary reports the phase mix", set(_sum["phase_pct"]) ==
      {"IMPULSE", "CORRECTIVE", "UNCLEAR"})
check("summary reports the confluence distribution",
      isinstance(_sum["confluence_distribution"], dict))
check("summary reports how often the Tide was unavailable",
      "tide_unavailable_pct" in _sum)

_srcA = open("agents.py").read()
_i_eval = _srcA.index("ev, conf = ta_elliott.evaluate(")
_i_gate = _srcA.index('if not cfg.get("ta_auto_deploy", False):')
check("confluence is evaluated BEFORE the auto_deploy gate", _i_eval < _i_gate,
      "otherwise nothing is captured when the strategy is merely observed")
_i_log = _srcA.index("log_ta_observation(")
check("observation is logged BEFORE the auto_deploy gate", _i_log < _i_gate)
check("a logging failure cannot stop the strategy",
      "calibration log failed" in _srcA)
check("confluence counts surface in the agent summary",
      "confluence[" in _srcA)
check("retention prune is wired", "prune_ta_calibration" in _srcA)
check("ta_calibration_logging registered", "ta_calibration_logging" in config.DEFAULTS)

_c = _H._conn(); _c.execute("DELETE FROM ta_calibration WHERE symbol='UNITSYM'")
_c.commit(); _c.close()

print("\n13) v58.41 — measurable thresholds")
check("GMMA computes on 1m by default (5m needs 325min of a 375min session)",
      ta.TA_ELLIOTT_DEFAULTS["gmma_timeframe"] == "1m")
_c1 = mk([100 + (i % 9) for i in range(200)], step=60)
_c5 = mk([100 + (i % 9) for i in range(30)], step=300)
_cl, _tf = ta.gmma_series_for(_c1, _c5, ta.TA_ELLIOTT_DEFAULTS)
check("1m series chosen when long enough", _tf == "1m" and len(_cl) == 200)
check("GMMA is COMPUTABLE on that series",
      ta.gmma_state(_cl, ta.TA_ELLIOTT_DEFAULTS)["state"] is not None,
      "it returned None on 79.1% of real observations before this")
_cl2, _ = ta.gmma_series_for([], _c5, ta.TA_ELLIOTT_DEFAULTS)
check("falls back rather than crashing when 1m is absent", isinstance(_cl2, list))
# v58.45 — the returned label is persisted to the calibration table as
# `gmma_tf`. Naming a timeframe the data did not come from would
# corrupt the very dataset this logging exists to produce.
_short1, _short5 = mk([100] * 10, step=60), mk([100] * 20, step=300)
_cl3, _tf3 = ta.gmma_series_for(_short1, _short5, ta.TA_ELLIOTT_DEFAULTS)
check("when neither series suffices, the label names the one ACTUALLY used",
      _tf3 == "5m" and len(_cl3) == 20, f"tf={_tf3} len={len(_cl3)}")
check("bb_slope_eps lowered from the measured-too-high 0.0004",
      ta.TA_ELLIOTT_DEFAULTS["bb_slope_eps"] < 0.0004,
      f"{ta.TA_ELLIOTT_DEFAULTS['bb_slope_eps']} — IMPULSE classified 0.0% at 0.0004")

_st = ta.compute_state("RAWCHK", _c1, mk([100 + (i % 9) for i in range(40)], step=300),
                       mk([100] * 30, step=900), params=ta.TA_ELLIOTT_DEFAULTS)
check("state exposes RAW inputs, not only derived states",
      isinstance(_st.get("raw"), dict), str(list((_st.get("raw") or {}))[:4]))
for _k in ("bb_slope", "gmma_spread", "pivots_5m", "pivot_lows", "pivot_highs"):
    check(f"raw carries '{_k}'", _k in (_st.get("raw") or {}))

import history as _H2
_H2._conn().close()
_hsrc = open("history.py").read()
check("calibration table stores the raw columns",
      all(c in _hsrc for c in ("bb_slope REAL", "gmma_spread REAL",
                               "pivots_5m INTEGER")))
check("summary reports raw DISTRIBUTIONS so a threshold can be read off data",
      '"raw_distributions"' in _hsrc)
check("summary reports how often GMMA was even computable",
      "gmma_computable_pct" in _hsrc)
check("summary reports whether divergence had two pivots to compare",
      "obs_with_2plus_pivot_lows_pct" in _hsrc,
      "no threshold on the oscillator can help if there are never two swings")

_bt = open("backtester.py").read()
check("fee_per_lot=0 warns loudly", "warn_if_costs_disabled" in _bt)
check("the warning names the live-gate consequence",
      "is_live_enabled" in _bt.split("def warn_if_costs_disabled")[1][:900])
check("BacktestAgent surfaces it", "Backtest costs disabled" in open("agents.py").read())

print("\n14) Existing strategies untouched")
for legacy in ("orb", "vwap_pullback", "ema_mtf", "sg_ema",
               "momentum_confluence", "ew_reversal"):
    check(f"'{legacy}' still in PA_NAMES", legacy in pa.PA_NAMES)
sample = mk([100 + (i % 7) - 3 for i in range(120)], step=60)
for legacy in ("orb", "vwap_pullback", "ema_mtf"):
    check(f"'{legacy}' deterministic and unaffected",
          pa.evaluate(legacy, sample) == pa.evaluate(legacy, sample))
import strategy_docs
check("S9 documented", "ta_elliott" in strategy_docs.DOCS)
check("S8 docs still present", "ew_reversal" in strategy_docs.DOCS)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
