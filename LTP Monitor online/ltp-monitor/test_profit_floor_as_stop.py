"""v58.73 — an armed profit floor must be a PRICE, not a per-cycle P&L test.

From the journal, four futures exits reported the level they were
protecting while booking a large loss:

    "gave back to ₹2310 of peak ₹4200"  ->  booked gross -₹3,000
    "gave back to ₹495  of peak  ₹900"  ->  booked gross -₹2,340
    "gave back to ₹1551 of peak ₹2820"  ->  booked gross -₹1,980
    "gave back to ₹825  of peak ₹1500"  ->  booked gross -₹1,500

Nothing was broken in the arithmetic. `rupee_profit_floor` fires when
`pnl <= floor` and says NOTHING about how far below it landed, and it is
consulted once per monitor cycle — so a fast reversal exits at whatever
P&L exists by the time the cycle runs, and the reason string quotes the
floor. Two defects, one visible and one not:

  1. the floor could not act between cycles (a P&L level has no
     representation in the market);
  2. the message could not report its own failure.

Fixed by translating an armed floor into a stop PRICE — the stop is
checked first in the exit chain and exits AT the level — and by making
the message state the realised P&L and the shortfall.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes config and opens paper futures positions")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents, config

CFG = {"rupee_profit_floor_enabled": True, "rupee_profit_floor_arm_rupees": 750,
       "rupee_profit_floor_keep_pct": 60, "rupee_profit_floor_min_rupees": 300,
       "rupee_profit_floor_as_stop": True, "paper_mode": True}
config.save(CFG)

print("1) the ratchet itself still behaves")
st = {}
cfg = config.load()
# Read the FUTURES keep-pct from config rather than restating 60: the
# class override is 55, which is why the journal shows a ₹2310 floor on
# a ₹4200 peak. A test that hardcodes the global constant disagrees with
# the code the moment a per-class setting exists — the same mistake as
# the fee_per_lot 40-vs-30 failure.
KEEP = cfg.get("rupee_profit_floor_keep_pct_futures",
               cfg.get("rupee_profit_floor_keep_pct", 60)) / 100.0
FLOOR = round(4200 * KEEP, 0)
print(f"     (futures keep_pct = {KEEP*100:.0f}% → floor on a ₹4,200 peak = ₹{FLOOR:.0f})")
check("below the arm level nothing happens",
      agents.rupee_profit_floor(st, 500, cfg, "futures") is None)
check("no floor is set yet", not st.get("rpf_floor"))
agents.rupee_profit_floor(st, 4200, cfg, "futures")     # peak
check("arms and records the peak", st.get("rpf_peak") == 4200, str(st.get("rpf_peak")))
check("floor = keep_pct of peak", st.get("rpf_floor") == FLOOR, str(st.get("rpf_floor")))
agents.rupee_profit_floor(st, 3000, cfg, "futures")
check("floor never falls when P&L dips", st.get("rpf_floor") == FLOOR, str(st.get("rpf_floor")))

print("\n2) the message reports the FILL, not the level it hoped for")
r = agents.rupee_profit_floor(dict(st), -3000, cfg, "futures")
check("it fires", bool(r))
check("it states the realised P&L", "-3,000" in r or "₹-3,000" in r, str(r)[:80])
check("it names the shortfall rather than implying success",
      "MISSED the floor" in r, str(r)[:110])
r2 = agents.rupee_profit_floor(dict(st), FLOOR, cfg, "futures")
check("a clean exit AT the floor reports no shortfall",
      r2 and "MISSED" not in r2, str(r2)[:90])

print("\n3) an armed floor becomes a stop price on the position")
def pos(side="LONG", entry=24000.0, lots=8, lot_size=75, sl=None):
    sign = 1 if side == "LONG" else -1
    return {"symbol": "NIFTY", "kind": "future", "side": side, "lots": lots,
            "lot_size": lot_size, "entry": entry,
            "sl": sl if sl is not None else entry - sign * 40,
            "target": entry + sign * 100, "pnl": 0.0, "peak": entry,
            "mae": 0.0, "mfe": 0.0}

bus = agents.Bus()
ex = agents.ExecutionAgent(bus, {"get_chain": lambda s: None,
                                 "orders_factory": lambda: None})
p = pos()
qty = p["lots"] * p["lot_size"]                       # 600
# drive the ratchet to a ₹4,200 peak -> floor ₹2,520 -> 4.2 pts above entry
agents.rupee_profit_floor(p, 4200, cfg, "futures")
bus.set("futures_positions", {"NIFTY": p})
bus.set("future_ohlc:NIFTY", {"close": p["entry"] + 4200 / qty})
real_open, agents.market_open = agents.market_open, lambda: True
try:
    ex._monitor_futures()
finally:
    agents.market_open = real_open
p2 = (bus.get("futures_positions") or {}).get("NIFTY") or p
expect = round(p["entry"] + FLOOR / qty, 2)
check("the stop is ratcheted to the floor price",
      abs(p2["sl"] - expect) < 0.02, f"sl={p2['sl']} expected={expect}")
check("which is ABOVE entry for a long — a locked profit, not a loss stop",
      p2["sl"] > p2["entry"], f"sl={p2['sl']} entry={p2['entry']}")
check("and it was logged", any("profit floor → stop" in l for l in bus.feed),
      next((l[-58:] for l in bus.feed if "profit floor" in l), "NOT LOGGED"))

print("\n4) once ratcheted, a fallback exits AT the floor — the whole point")
p3 = dict(p2)
bus2 = agents.Bus()
ex2 = agents.ExecutionAgent(bus2, {"get_chain": lambda s: None, "orders_factory": lambda: None})
bus2.set("futures_positions", {"NIFTY": p3})
# price slips back below the locked level
bus2.set("future_ohlc:NIFTY", {"close": p3["entry"] + 1000 / qty})
real_open, agents.market_open = agents.market_open, lambda: True
try:
    ex2._monitor_futures()
finally:
    agents.market_open = real_open
p4 = (bus2.get("futures_positions") or {}).get("NIFTY")
check("the position is closed rather than left to run back to a loss", p4 is None,
      str(p4)[:60])
check("and it exited on the STOP at the floor level, not a P&L guess",
      any("stoploss" in l and "NIFTY" in l for l in bus2.feed),
      next((l[-64:] for l in bus2.feed if "stoploss" in l), "no stop exit logged"))
_closed = [t for t in (bus2.get("closed_trades") or []) if t.get("symbol") == "NIFTY"]
if _closed:
    check("the booked P&L is the locked profit, not a loss",
          float(_closed[-1].get("pnl", 0)) > 0, str(_closed[-1].get("pnl")))

print("\n5) a SHORT locks below entry, and the switch is honoured")
ps = pos(side="SHORT")
agents.rupee_profit_floor(ps, 4200, cfg, "futures")
b3 = agents.Bus()
e3 = agents.ExecutionAgent(b3, {"get_chain": lambda s: None, "orders_factory": lambda: None})
b3.set("futures_positions", {"NIFTY": ps})
b3.set("future_ohlc:NIFTY", {"close": ps["entry"] - 4200 / qty})
real_open, agents.market_open = agents.market_open, lambda: True
try:
    e3._monitor_futures()
finally:
    agents.market_open = real_open
ps2 = (b3.get("futures_positions") or {}).get("NIFTY") or ps
check("SHORT floor sits BELOW entry", ps2["sl"] < ps2["entry"],
      f"sl={ps2['sl']} entry={ps2['entry']}")

config.save({"rupee_profit_floor_as_stop": False})
cfg_off = config.load()
p5 = pos(); agents.rupee_profit_floor(p5, 4200, cfg_off, "futures")
sl_before = p5["sl"]
b4 = agents.Bus()
e4 = agents.ExecutionAgent(b4, {"get_chain": lambda s: None, "orders_factory": lambda: None})
b4.set("futures_positions", {"NIFTY": p5})
b4.set("future_ohlc:NIFTY", {"close": p5["entry"] + 4200 / qty})
real_open, agents.market_open = agents.market_open, lambda: True
try:
    e4._monitor_futures()
finally:
    agents.market_open = real_open
p6 = (b4.get("futures_positions") or {}).get("NIFTY") or p5
check("with the switch off the stop is untouched", abs(p6["sl"] - sl_before) < 0.01,
      f"{sl_before} -> {p6['sl']}")
config.save({"rupee_profit_floor_as_stop": True})

print("\n6) registered, or config.save() drops it silently")
check("rupee_profit_floor_as_stop is in DEFAULTS",
      "rupee_profit_floor_as_stop" in config.DEFAULTS)
check("and survives a save round-trip",
      config.save({"rupee_profit_floor_as_stop": True}).get(
          "rupee_profit_floor_as_stop") is True)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
