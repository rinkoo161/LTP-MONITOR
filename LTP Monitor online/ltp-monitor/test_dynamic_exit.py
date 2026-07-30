"""v58.47 — momentum_confluence's MACD-histogram early exit.

The ONE documented simplification in this module's Pine port, carried
since the port was written. The original exits the moment the MACD
histogram's slope reverses; the port used a fixed risk-reward target
as an explicit stand-in.

pa_strategies' own note was accurate about WHY: "there is no existing
mechanism for 'keep evaluating an index-level condition on every future
candle for an open position'." Every other PA strategy expresses its
exit as fixed stop/target PRICES computed once at signal time. A
histogram turn is a condition on FUTURE candles, not a level. That
mechanism is what this release actually builds.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import pa_strategies as pa, agents, config

def series(vals):
    return [{"close": v, "high": v, "low": v, "open": v} for v in vals]

RISE = [100 + i * 0.6 for i in range(60)]
FALL = RISE + [RISE[-1] - i * 1.5 for i in range(1, 6)]

print("1) The condition FIRES — not merely 'does not crash'")
r = pa.macd_hist_turn_exit(series(FALL), +1)
check("long exits when the histogram turns down", r is not None, str(r))
check("reason quotes the actual histogram values",
      r and "->" in r, str(r))
check("short does NOT exit on the same series",
      pa.macd_hist_turn_exit(series(FALL), -1) is None,
      "a falling histogram favours a short")
check("long does not exit while the histogram still rises",
      pa.macd_hist_turn_exit(series(RISE), +1) is None)

print("\n2) Guards")
check("too few candles -> None", pa.macd_hist_turn_exit(series(RISE[:20]), +1) is None)
check("empty input -> None", pa.macd_hist_turn_exit([], +1) is None)
check("never raises on malformed candles",
      pa.macd_hist_turn_exit([{"close": None}] * 80, +1) is None)

print("\n3) Confirmation bars actually gate it")
one_bar = RISE + [RISE[-1] - 1.5]
strict = pa.macd_hist_turn_exit(series(one_bar), +1, {"hist_turn_confirm_bars": 3})
loose = pa.macd_hist_turn_exit(series(one_bar), +1, {"hist_turn_confirm_bars": 1})
check("1 bar of reversal is enough at confirm=1", loose is not None, str(loose))
check("1 bar is NOT enough at confirm=3", strict is None,
      "an exit tripping on one bar surrenders every position to chop")
check("param registered",
      "hist_turn_confirm_bars" in pa.PA_DEFAULTS["momentum_confluence"])
check("tunable within bounds",
      pa.PA_BOUNDS["momentum_confluence"]["hist_turn_confirm_bars"][:2] == (1, 4))
check("relaxing means MORE confirmation, i.e. exiting later",
      pa.PA_BOUNDS["momentum_confluence"]["hist_turn_confirm_bars"][2] == +1)

print("\n4) The missing mechanism now exists")
AG = open("agents.py").read()
check("dynamic_exit_reason exists", "def dynamic_exit_reason" in AG)
check("it is generic, keyed on a named condition", '"dynamic_exit"' in AG)
check("only momentum_confluence sets it today",
      'if name == "momentum_confluence" else None' in AG)
check("master switch respected", "dynamic_exits_enabled" in AG)
check("registered", "dynamic_exits_enabled" in config.DEFAULTS)

class Bus:
    def __init__(s, d): s.d = d
    def get(s, k, default=None): return s.d.get(k, default)

CFG = dict(config.DEFAULTS)
pos = {"symbol": "NIFTY", "leg": "ce", "dynamic_exit": "macd_hist_turn"}
bus = Bus({"pa_candles:NIFTY": {"c1": series(FALL)}})
check("fires end-to-end through the mechanism",
      agents.dynamic_exit_reason(pos, bus, CFG) is not None)
check("a position without the tag is unaffected",
      agents.dynamic_exit_reason({"symbol": "NIFTY", "leg": "ce"}, bus, CFG) is None,
      "every other PA strategy must be untouched")
check("MISSING candles skip rather than force an exit",
      agents.dynamic_exit_reason(pos, Bus({}), CFG) is None,
      "absent data must never trigger a close")
check("master switch off restores fixed-levels-only behaviour",
      agents.dynamic_exit_reason(pos, bus,
                                 dict(CFG, dynamic_exits_enabled=False)) is None)
check("PE positions evaluate the opposite direction",
      agents.dynamic_exit_reason({"symbol": "NIFTY", "leg": "pe",
                                  "dynamic_exit": "macd_hist_turn"},
                                 bus, CFG) is None,
      "a falling histogram should not close a short")

print("\n5) Exit precedence")
i_stop = AG.index("_rpf_option = rupee_profit_floor")
i_dyn = AG.index("elif _dyn_exit:")
# Search AFTER the dynamic-exit line: there are two `time stop` blocks
# (spreads and options) in different functions, and the first
# occurrence in the file is the SPREAD one — matching it compared two
# unrelated code paths and reported a precedence failure that did not
# exist.
i_time = AG.index('reason = (f"time stop', i_dyn)
check("evaluated once per cycle", AG.count("_dyn_exit = dynamic_exit_reason") == 1)
check("sits BEFORE the OPTION time stop", i_dyn < i_time,
      "an edge signal must pre-empt the blunt clock, not the reverse")
check("sits AFTER the profit floor", i_stop < i_dyn,
      "it is a strategy signal, not a capital backstop")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
