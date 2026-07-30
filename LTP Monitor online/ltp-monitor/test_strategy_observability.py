"""v58.37 — three gaps found from live screenshots + a session log.

1. STRATEGY 8 WAS NEVER EVALUATED. The `s8_auto_deploy` gate sat
   BEFORE evaluation, so with it off (the default, and how S8 has
   shipped since v58.28) the strategy never ran: no eligibility, no
   Shadow Journal, no detector counts. Proven by the live log — every
   other PA strategy reported 4 no-setup counts per cycle (one per
   symbol) while ew_reversal reported 0:

     (no-setup by strategy: {'ema_mtf': 4, 'momentum_confluence': 4,
      'orb': 4, 'sg_ema': 4, 'vwap_pullback': 4, 'ew_reversal': 0})

   This is the SAME mistake diagnosed and fixed for Strategy 9 in
   v58.32 and left uncorrected in S8. Gates must govern TRADING, never
   observation.

2. ew_reversal WAS PERMANENTLY "not eligible" on the Strategies page,
   and ta_elliott had no row at all. The page routes PA strategies
   through `pa.evaluate()`, which deliberately does not dispatch
   ew_reversal — so it fell to a bare `return None`. Exactly what the
   file's own sg_ema comment warns about.

3. THE BACKTEST CHART OMITTED DAYS THAT PRODUCED NO TRADE. The day
   dropdown was built from days with trades, so 27/28/29 July were
   absent — indistinguishable from the backtest not having run, which
   is how it was reported.

Run:  python3 test_strategy_observability.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


AG = open("agents.py").read()
APP = open("app.py").read()
HTML = open("static/dashboard.html").read()

print("1) Strategy 8 is evaluated whether or not it may trade")
i_enabled = AG.index('if not cfg.get("strategy8_enabled", True):')
i_eval = AG.index("ev, s7_gates = ew_reversal.evaluate(")
i_auto = AG.index('if not cfg.get("s8_auto_deploy", False):\n                        continue      # observed')
check("master switch still gates everything", i_enabled < i_eval)
check("EVALUATION happens BEFORE the auto_deploy gate", i_eval < i_auto,
      "this is the whole bug: it used to be the other way round")
check("eligibility is published every cycle",
      's8_eligibility:{sym}' in AG)
i_pub = AG.index('self.bus.set(f"s8_eligibility:{sym}"')
check("publish also happens before the auto_deploy gate", i_pub < i_auto)
check("paper-mode gate still guards trading only",
      AG.index('continue      # paper-only on introduction') > i_pub)

print("\n2) Strategies page shows S8 and S9")
check("ew_reversal excluded from the generic pa.evaluate() loop",
      'if n not in ("sg_ema", "ew_reversal")' in APP,
      "pa.evaluate() does not dispatch it - it would always read 'not eligible'")
check("ew_reversal preview reads the published bus verdict",
      'pilot.bus.get(f"s8_eligibility:{sym}")' in APP)
check("ta_elliott gets a preview row at all",
      'pa_previews["ta_elliott"]' in APP,
      "it is not in PA_NAMES, so it never appeared on the page")
check("ta_elliott preview reads its published state",
      'pilot.bus.get(f"ta_state:{sym}")' in APP)
check("detector summary helper exists", "_s8_detector_summary" in APP)

import pa_strategies as pa
check("pa.evaluate() still refuses ew_reversal (one code path only)",
      pa.evaluate("ew_reversal", [{"time": i, "open": 100, "high": 101,
                                   "low": 99, "close": 100, "volume": 0}
                                  for i in range(80)]) is None)
sys.path.insert(0, ".")
import app as _app
check("_s8_detector_summary renders a mixed detector map",
      "FIRED" in _app._s8_detector_summary({"ending_diagonal": True,
                                            "hs": False,
                                            "failed_hs": "skipped (off)"}))
check("_s8_detector_summary degrades on empty input",
      _app._s8_detector_summary(None) == "no detector data")
check("_s8_detector_summary distinguishes 'no pattern' from 'skipped'",
      "no pattern" in _app._s8_detector_summary({"hs": False})
      and "skipped" in _app._s8_detector_summary({"hs": "skipped (off)"}))

print("\n3) Backtest chart spans every archived day, not only trade days")
check("range-candles endpoint exists",
      '@app.get("/api/backtest/range-candles")' in APP)
check("range-candles concatenates and sorts",
      'out.sort(key=lambda c: c["time"])' in APP)
check("range-candles reports day boundaries", '"day_boundaries"' in APP)
check("range-candles reports days requested vs days with data",
      '"days_requested"' in APP and '"days_with_data"' in APP,
      "so an empty day is visible rather than silently dropped")
check("status endpoint returns the archived day LIST, not just a count",
      '"archive": archive_days' in APP)
check("frontend merges archived days with trade days",
      "days=Array.from(new Set(arch.concat(tradeDays))).sort()" in HTML)
check("frontend defaults to the continuous view", 'sel.value="__all__"' in HTML)
check("frontend labels days that produced no trade",
      '(no trade)' in HTML)
check("frontend tells the user they can scroll",
      "scroll left for earlier days" in HTML)
check("continuous mode calls range-candles",
      'range-candles?symbol=' in HTML)
check("single-day mode still available", 'day-candles?symbol=' in HTML)

print("\n3b) Continuous mode actually shows the trades (v58.37b)")
check("markers are NOT filtered to one day in continuous mode",
      'const shown = isAll ? btSelection.trades' in HTML,
      "filter(t=>t.day===day) matched nothing when day was '__all__'")
check("day separators are drawn so no-trade days are visibly present",
      'd.day_boundaries||[]).forEach' in HTML)
check("a day with no trade is labelled on the chart itself",
      '" no trade"' in HTML)
check("view opens on the most recent session, not fitContent",
      'setVisibleLogicalRange' in HTML,
      "fitContent would squash 30 sessions into one screen")
check("fitContent kept as the fallback and for single-day mode",
      HTML.count('fitContent()') >= 2)
check("legend reports trades across ALL days in continuous mode",
      "trade(s) across '+scope" in HTML)
check("legend names how many days were backtested with no setup",
      "day(s) backtested with no setup" in HTML)
# The marker array must stay sorted ascending — a hard LWC requirement
# this project has already had to fix once for the main chart.
i_push_sep = HTML.index("d.day_boundaries||[]).forEach")
i_sort = HTML.index("markers.sort(function(a,b){return a.time-b.time;});", i_push_sep)
check("separators are added BEFORE the ascending sort", i_push_sep < i_sort,
      "LWC requires markers sorted ascending by time")

print("\n4) Nothing else changed")
import config
check("s8_auto_deploy still defaults OFF",
      config.DEFAULTS["s8_auto_deploy"] is False,
      "observation is now unconditional; TRADING is still opt-in")
check("strategy8_enabled still defaults ON",
      config.DEFAULTS["strategy8_enabled"] is True)
check("ta_auto_deploy still defaults OFF",
      config.DEFAULTS["ta_auto_deploy"] is False)
check("ew_reversal still registered in PA_NAMES", "ew_reversal" in pa.PA_NAMES)
check("ta_elliott still NOT in PA_NAMES (it has its own agent)",
      "ta_elliott" not in pa.PA_NAMES)
for legacy in ("orb", "vwap_pullback", "ema_mtf", "sg_ema", "momentum_confluence"):
    check(f"'{legacy}' untouched in PA_NAMES", legacy in pa.PA_NAMES)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
