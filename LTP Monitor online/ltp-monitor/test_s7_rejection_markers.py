"""v58.9 (part 7, item 11 — half 1) — tests for Strategy 7 rejected-
signal markers: the client's `lwSignalMarkers` layer was declared since
v51 with "entries/exits/rejections" in its own comment, and the
`s7_show_rejected_markers` Settings toggle already existed — but
confirmed NEITHER side actually wired a rejection through (genuinely
dead code on both ends, not "client ready, server not wired" as an
earlier roadmap note assumed).

A "rejection" worth marking is specifically a real EMA cross (the
`cross` gate passed) that a LATER gate (mtf/structure/ai_bias) then
explicitly blocked — not the routine "no cross at all" case, which is
most cycles and isn't a near-miss worth flagging.

Uses the SAME transition-based "only mark a genuine new occurrence,
not every cycle the same blocked state persists" approach already
proven for the False Breakout marker fix (item 4) — the mistake that
fix corrected is exactly the mistake this feature could have shipped
with if built without that lesson in mind.

Run:  python3 test_s7_rejection_markers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
import app

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def simulate_gate_cycle(pa, sym, s7_gates, spot):
    """Mirrors the exact inline logic added to PriceActionAgent.cycle()"""
    blocked_gate = next((g for g in ("mtf", "structure", "ai_bias")
                        if s7_gates.get(g) is False), None)
    rej_key = f"{sym}:s7_rejection"
    was_active = pa._s7_rejection_state.get(rej_key, False)
    is_active = bool(s7_gates.get("cross")) and blocked_gate is not None
    pa._s7_rejection_state[rej_key] = is_active
    if is_active and not was_active:
        pa._record_s7_rejection(sym, blocked_gate, spot)


print("1) a real EMA cross blocked by a later gate is recorded")
bus = agents.Bus()
pa = agents.PriceActionAgent(bus, {})
pa._s7_rejection_state = {}
simulate_gate_cycle(pa, "NIFTY", {"cross": True, "mtf": True, "structure": False,
                                  "ai_bias": True}, 23900)
events = bus.get("s7_rejected_events:NIFTY", [])
check("one rejection recorded", len(events) == 1, str(events))
check("the correct blocked gate (structure) is identified",
      events[0]["gate"] == "structure", str(events[0]))

print("\n2) the SAME persistent blocked state across multiple cycles "
     "records ONLY ONCE — the exact mistake item 4's False Breakout "
     "fix corrected, being guarded against here from the start")
for spot in (23901, 23902, 23903, 23904):
    simulate_gate_cycle(pa, "NIFTY", {"cross": True, "mtf": True,
                                     "structure": False, "ai_bias": True}, spot)
events2 = bus.get("s7_rejected_events:NIFTY", [])
check("still exactly 1 event after 4 more cycles of the SAME block",
      len(events2) == 1, str(len(events2)))

print("\n3) the condition clearing then a GENUINE new rejection (on a "
     "different gate) correctly records a second, distinct event")
simulate_gate_cycle(pa, "NIFTY", {"cross": False, "mtf": "not evaluated (no cross)",
                                  "structure": "not evaluated (no cross)",
                                  "ai_bias": "not evaluated (no cross)"}, 23905)
simulate_gate_cycle(pa, "NIFTY", {"cross": True, "mtf": True, "structure": True,
                                  "ai_bias": False}, 23910)
events3 = bus.get("s7_rejected_events:NIFTY", [])
check("now exactly 2 distinct events", len(events3) == 2, str(len(events3)))
check("the second event correctly identifies ai_bias as the blocker",
      events3[1]["gate"] == "ai_bias", str(events3[1]))

print("\n4) a routine 'no cross at all' cycle (the vast majority of "
     "cycles in practice) never records anything — only a REAL cross "
     "that then got blocked counts as a rejection worth marking")
bus2 = agents.Bus()
pa2 = agents.PriceActionAgent(bus2, {})
pa2._s7_rejection_state = {}
simulate_gate_cycle(pa2, "FINNIFTY", {"cross": False,
                                     "mtf": "not evaluated (no cross)",
                                     "structure": "not evaluated (no cross)",
                                     "ai_bias": "not evaluated (no cross)"}, 26000)
check("no rejection recorded for a routine no-cross cycle",
      bus2.get("s7_rejected_events:FINNIFTY", []) == [])

print("\n5) a fully-passing cross (no gate blocked) never records "
     "anything either — this is a genuine trade, not a rejection")
bus3 = agents.Bus()
pa3 = agents.PriceActionAgent(bus3, {})
pa3._s7_rejection_state = {}
simulate_gate_cycle(pa3, "SENSEX", {"cross": True, "mtf": True, "structure": True,
                                    "ai_bias": True}, 76800)
check("no rejection recorded when every gate actually passed",
      bus3.get("s7_rejected_events:SENSEX", []) == [])

print("\n6) server-side conversion to Lightweight Charts markers uses "
     "ONLY valid LWC shapes (circle/square/arrowUp/arrowDown — "
     "'diamond' is NOT supported and was caught before shipping)")
sample_events = [{"time": 1785154566, "gate": "structure", "spot": 23900,
                  "day": "2026-07-27"}]
markers = app._s7_rejections_to_markers(sample_events)
check("produces exactly one marker", len(markers) == 1, str(markers))
check("uses a valid LWC shape",
      markers[0]["shape"] in ("circle", "square", "arrowUp", "arrowDown"),
      markers[0]["shape"])
check("the marker text identifies which gate blocked it",
      "structure" in markers[0]["text"], markers[0]["text"])

print("\n7) source-level guards: both sides of the previously entirely "
     "dead client<->server wiring are now actually connected")
h = open("static/dashboard.html").read()
check("client now actually ASSIGNS lwSignalMarkers from server data "
      "(previously declared but never populated)",
      "lwSignalMarkers=(msg.s7_markers||[]).map(" in h)
app_src = open("app.py").read()
check("server sends s7_markers as a distinct field in the signals message",
      '"s7_markers": s7_markers' in app_src)
agents_src = open("agents.py").read()
check("PriceActionAgent actually calls _record_s7_rejection on a "
      "genuine transition, not every cycle",
      "if is_active and not was_active:" in agents_src and
      "self._record_s7_rejection(sym, blocked_gate, spot)" in agents_src)

print("\n8) POSITION-CAP POLICY (the second half of item 11): confirmed "
     "S7 shares the exact same account-wide position cap as every "
     "other directional strategy, via a real end-to-end RiskAgent."
     "evaluate() call — no special exemption exists anywhere for "
     "sg_ema specifically")
import config as _cfg

bus_pc = agents.Bus()
risk_pc = agents.RiskAgent(bus_pc, {})
bus_pc.set("symbols", ["NIFTY"])
bus_pc.set("positions", {"NIFTY": {"source": "momentum_buy", "entry": 100}})
bus_pc.set("trades_today", 0)
_before_cfg = _cfg.load()
_cfg.save({"paper_mode": True})
real_market_open_pc = agents.market_open
agents.market_open = lambda: True
try:
    sig_pc = {"signal": "BUY_CE", "confidence": 90, "entry": 100, "stoploss": 70,
             "target1": 160, "target2": 180, "reasons": [], "source": "sg_ema"}
    job_pc = {"symbol": "NIFTY", "signal": sig_pc, "analysis": {"spot": 24000}}
    ok_pc, checks_pc = risk_pc.evaluate(job_pc)
    check("an S7 signal is correctly BLOCKED when the same symbol "
          "already has an open position from a DIFFERENT strategy "
          "(momentum_buy) — the shared cap has no S7-specific carve-out",
          ok_pc is False and
          any("no open position on NIFTY" in c and c.startswith("\u2717") for c in checks_pc),
          str(checks_pc))
finally:
    agents.market_open = real_market_open_pc
    _cfg.save(_before_cfg)

check("architectural confirmation: `positions` is keyed by symbol "
      "only (agents.py), not (symbol, strategy) — this is WHY the "
      "cap is shared, not a separate S7 mechanism that happens to "
      "agree with it",
      'job["symbol"] not in positions' in agents_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
