"""v59.0 Phase 0 §3.4 — the seven cost keys survive a config round trip.

config.save() silently drops any key not in DEFAULTS, and SettingsIn
governs what can arrive from the Settings page at all. A key registered
in one place and not the other is unreachable from the UI that exists to
set it — which has now happened twice in this repo (auth_enabled in
v58.74, the whole auth block in v58.76). Both places, or the key is
decoration.

Clamping is asserted ON READ, not on write: a 15x-levered instrument is
the last place to trust a value that was only validated when it was
stored. A stale or hand-edited config.json must not leak a bad rate into
a cost number.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import config, futures_costs as fc

KEYS = list(fc.RATES)

print("1) registered in BOTH places")
app_src = open("app.py").read()
for k in KEYS:
    check(f"{k} in config.DEFAULTS", k in config.DEFAULTS)
for k in KEYS:
    check(f"{k} declared on SettingsIn", f"{k}: float | None = None" in app_src)

print("\n2) survives a save() round trip")
sent = {k: fc.RATES[k][0] for k in KEYS}
got = config.save(sent)
for k in KEYS:
    check(f"{k} round-trips", got.get(k) == sent[k], f"{got.get(k)} vs {sent[k]}")

print("\n3) clamped ON READ, from whatever is already stored")
for k, (default, lo, hi) in fc.RATES.items():
    config.save({k: hi * 1000})
    check(f"{k} above its ceiling clamps to {hi}", fc.rate(k) == hi, str(fc.rate(k)))
    config.save({k: -abs(hi) - 1})
    check(f"{k} below its floor clamps to {lo}", fc.rate(k) == lo, str(fc.rate(k)))
    config.save({k: default})

print("\n4) a junk value does not crash the cost path")
config.save({"fut_gst_pct": "not a number"})
check("a non-numeric rate falls back to its default rather than raising",
      fc.rate("fut_gst_pct") == fc.RATES["fut_gst_pct"][0],
      str(fc.rate("fut_gst_pct")))
config.save({"fut_gst_pct": 0.18})
check("and the cost model still returns a number",
      fc.cost_round_trip("NIFTY", 24800, 24800, 1, lot=65) > 0)

print("\n5) defaults match the spec table")
for k, expect in (("fut_brokerage_per_order", 20.0), ("fut_stt_sell_pct", 0.0002),
                  ("fut_exchange_txn_pct", 0.0000173),
                  ("fut_sebi_turnover_pct", 0.000001),
                  ("fut_stamp_duty_pct", 0.00002), ("fut_gst_pct", 0.18),
                  ("fut_slippage_points", 1.0)):
    check(f"{k} default = {expect}", config.DEFAULTS[k] == expect,
          str(config.DEFAULTS[k]))

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
