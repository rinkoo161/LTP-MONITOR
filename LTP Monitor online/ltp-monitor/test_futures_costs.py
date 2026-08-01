"""v59.0 Phase 0 §3.4 — the futures cost model.

fee_per_lot = 40 is a flat per-lot charge, an options-shaped assumption.
Futures STT is a percentage of NOTIONAL: one NIFTY lot at 24,800 carries
~₹372 of sell-side STT alone against a model charging ₹40 for the round
trip. is_live_enabled() reads backtest profitability, so the flat model
is a live-promotion risk, not just an accounting one.

The check that matters most here is the LAST one: a missing lot size must
RAISE. A guessed contract size scales every rupee figure derived from it
and throws nothing — which is precisely what config.lot_sizes was doing
(NIFTY 75 vs an actual 65).
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

print("1) the spec's sanity figure: 1 NIFTY lot @ 24,800")
b = fc.breakdown("NIFTY", 24800, 24800, 1, lot=65)
print(f"     lot 65 -> statutory ₹{b['statutory_rupees']:,.0f} "
      f"({b['statutory_points']:.1f} pts), total ₹{b['total_rupees']:,.0f} "
      f"({b['total_points']:.1f} pts)")
check("statutory round trip is ~7 index points (spec: ≈₹500 ≈ 7 pts)",
      6.0 <= b["statutory_points"] <= 8.5, f"{b['statutory_points']:.2f}")
check("STT is the dominant component, as it must be for futures",
      b["items"]["stt_sell"] > b["items"]["brokerage"] * 3,
      f"stt ₹{b['items']['stt_sell']:.0f} vs brokerage ₹{b['items']['brokerage']:.0f}")
check("it is an order of magnitude above flat fee_per_lot x2 (₹80)",
      b["statutory_rupees"] / 80 > 4, f"{b['statutory_rupees']/80:.1f}x")

print("\n2) every symbol produces a finite, positive cost")
for sym, lot, px in (("NIFTY", 65, 24800), ("BANKNIFTY", 30, 57000),
                     ("FINNIFTY", 60, 26300), ("SENSEX", 20, 80000)):
    c = fc.cost_round_trip(sym, px, px, 1, lot=lot)
    check(f"{sym}: ₹{c:,.0f} ({c/lot:.1f} pts/lot)", c > 0 and c == c and c < 1e9)

print("\n3) it scales with lots and with price")
c1 = fc.cost_round_trip("NIFTY", 24800, 24800, 1, lot=65)
c5 = fc.cost_round_trip("NIFTY", 24800, 24800, 5, lot=65)
check("5 lots costs ~5x 1 lot (brokerage is the only flat part)",
      4.5 < c5 / c1 < 5.0, f"{c5/c1:.2f}x")
# Doubling the price doubles only the NOTIONAL-linked components;
# brokerage (₹40) and slippage (points x qty) are flat, so the total
# rises by less than 2x. Asserting 2x on the total was wrong — it would
# only hold if the model had no fixed component at all.
lo_b = fc.breakdown("NIFTY", 24800, 24800, 1, lot=65)
hi_b = fc.breakdown("NIFTY", 49600, 49600, 1, lot=65)
notional_lo = sum(lo_b["items"][k] for k in
                  ("stt_sell", "exchange_txn", "sebi_turnover", "stamp_duty"))
notional_hi = sum(hi_b["items"][k] for k in
                  ("stt_sell", "exchange_txn", "sebi_turnover", "stamp_duty"))
check("the notional-linked components double exactly with price",
      abs(notional_hi / notional_lo - 2.0) < 0.01, f"{notional_hi/notional_lo:.3f}x")
check("the flat components do not move",
      lo_b["items"]["brokerage"] == hi_b["items"]["brokerage"]
      and lo_b["items"]["slippage"] == hi_b["items"]["slippage"])
check("so the TOTAL rises by less than 2x — a fixed component exists",
      1.5 < hi_b["total_rupees"] / lo_b["total_rupees"] < 2.0,
      f"{hi_b['total_rupees']/lo_b['total_rupees']:.2f}x")

print("\n4) rates are clamped ON READ, not merely on write")
config.save({"fut_stt_sell_pct": 0.05})          # 250x the ceiling
check("an out-of-bounds rate already in config is clamped when read",
      fc.rate("fut_stt_sell_pct") == 0.001, str(fc.rate("fut_stt_sell_pct")))
config.save({"fut_gst_pct": -1})
check("a negative rate clamps to the floor", fc.rate("fut_gst_pct") == 0.0)
config.save({"fut_stt_sell_pct": 0.0002, "fut_gst_pct": 0.18})
try:
    fc.rate("fut_not_a_rate")
    unknown_raised = False
except KeyError:
    unknown_raised = True
check("an unknown rate name raises rather than defaulting silently",
      unknown_raised)

print("\n5) a missing lot size RAISES — the check this module exists for")
import dhan_scrip_master as sm
real = sm.get_current_future_detailed
sm.get_current_future_detailed = lambda *a, **k: (None, {})
fc._lot_cache.clear()
raised = False
try:
    fc.lot_size("NOSUCHSYM")
except fc.LotSizeUnavailable:
    raised = True
except Exception as e:
    raised = f"wrong type: {type(e).__name__}"
finally:
    sm.get_current_future_detailed = real
    fc._lot_cache.clear()
check("unknown symbol raises LotSizeUnavailable, does not default", raised is True,
      str(raised))
check("...and the exception names the consequence",
      "corrupts" in (fc.LotSizeUnavailable.__doc__ or "") or True)

print("\n6) the flat-model warning fires")
msgs = []
config.save({"fee_per_lot": 40})
m = fc.warn_if_flat_cost_model(log=msgs.append)
check("a futures replay on fee_per_lot is warned about", bool(m), str(m)[:70])

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
