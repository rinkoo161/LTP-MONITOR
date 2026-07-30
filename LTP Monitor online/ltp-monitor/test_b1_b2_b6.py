"""v58.49 — roadmap items B1, B2, B6.

B1  Futures had no chart markers while options and S7 both did — so the
    one instrument class losing money was also the only one invisible on
    the chart.
B2  Two suites passed or failed depending on external state rather than
    on the code: test_manual_deploy starved on margin after any suite
    leaving positions in open_state.json, and test_chart_indicators
    failed 15m/overlays whenever the local archive held fewer candles
    than EMA50 needs.
B6  Two INDEPENDENT cooldowns guarded the same Dhan quote surface, so
    one path backed off while the other kept hammering the endpoint that
    had just refused it.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import rate_limit as rl

print("1) B1 — futures chart markers")
AG = open("agents.py").read()
check("entry records a marker", 'self._record_chart_event(symbol, "entry"' in AG)
check("exit records a marker", '_record_chart_event(symbol, _k' in AG)
check("exit reason is classified like options",
      '"target_hit"' in AG and '"stop_hit"' in AG)
check("marker uses the INDEX spot, not the futures price",
      '(self.bus.get(f"analysis:{symbol}") or {}).get("spot")' in AG,
      "the chart plots the index; futures trade at a basis to it")
check("label identifies it as futures", 'f"FUT {side} x{lots}"' in AG)
i_e = AG.index('self._record_chart_event(symbol, "entry"')
i_pos = AG.index('pos = {"symbol": symbol, "kind": "future"', i_e - 2000)
check("entry marker is recorded before the position dict is built", i_e < i_pos)
check("reuses the generic recorder, no parallel mechanism",
      AG.count("def _record_chart_event") == 1)

print("\n2) B6 — one shared cooldown")
rl.reset()
check("clean at start", not rl.is_limited("quote"))
is429, secs = rl.note_failure("429 Too Many Requests", "quote")
check("429 detected", is429)
check("429 earns a long backoff", secs == rl.BACKOFF_429, f"{secs}s")
check("resource is now limited", rl.is_limited("quote"))
rl.note_failure("connection reset", "quote", otherwise=5)
check("a SHORTER backoff cannot undo a longer one", rl.remaining("quote") > 200,
      f"{rl.remaining('quote'):.0f}s left")
check("a different resource is unaffected", not rl.is_limited("chain"))
_, s2 = rl.note_failure("timeout", "chain")
check("non-429 gets the short backoff", s2 == rl.BACKOFF_OTHER, f"{s2}s")
check("snapshot lists active cooldowns", set(rl.snapshot()) == {"quote", "chain"})
check("reason is recorded for diagnostics", "429" in (rl.why("quote") or ""))
rl.reset("quote")
check("reset by name clears one", not rl.is_limited("quote") and rl.is_limited("chain"))
rl.reset()
check("reset() clears all", rl.snapshot() == {})

APP = open("app.py").read()
BA = open("broker_adapter.py").read()
check("app defers to the shared registry", 'rate_limit.is_limited("quote")' in APP)
check("broker_adapter defers to it too", 'rate_limit.is_limited("quote")' in BA)
check("broker_adapter reports its failures into it",
      'rate_limit.note_failure(' in BA)
check("app's named reset clears the shared registry too",
      'rate_limit.reset("quote")' in APP,
      "tests reset by name, not by poking globals")

print("\n3) B2 — tests no longer depend on external state")
MD = open("test_manual_deploy.py").read()
check("manual_deploy snapshots open_state.json", "_open_state_backup" in MD)
check("it restores on exit", "_atexit.register(_restore_open_state)" in MD)
check("it starts from a clean slate", '"positions": {}, "spreads": {}' in MD)
check("the v58.39 rupee cap is lifted and restored",
      "_cap_backup" in MD and "futures_risk_per_trade_rupees" in MD)
i_block = MD.index("roadmap B2")
i_first_save = MD.index("config.save(")
check("the isolation block runs BEFORE the suite's own config writes",
      i_block < i_first_save,
      "an earlier version landed after them and did nothing")

CI = open("test_chart_indicators.py").read()
check("chart test is candle-count aware", "SKIPPED (only" in CI)
check("it states the reason rather than passing silently", "needs {_need}" in CI)
check("a genuine regression still fails",
      "if not got and not _skip:" in CI,
      "pane missing WITH enough bars must still be an error")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
