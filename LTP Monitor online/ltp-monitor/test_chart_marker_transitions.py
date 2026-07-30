"""v58.9 (part 2) — tests for the REAL cause behind "False Breakout
stays visible all day," found after re-investigating since the earlier
anchor-timestamp fix (2026-07-27, part 1) turned out not to be the
whole story.

Two compounding bugs, both fixed:
1. _institutional_to_markers()/_smart_money_to_markers() re-emitted a
   marker on EVERY cycle a condition stayed "active" (their own
   docstrings already said "current-state flags recomputed each
   cycle") — visually indistinguishable from "the same marker is
   stuck," since a fresh one kept appearing at the current candle
   every cycle, following price forward all day.
2. Fixing #1 by only firing on a genuine OFF->ON transition created a
   NEW problem: the client fully REPLACES its marker array on every
   "signals" message rather than accumulating — so a marker that only
   fires once would flash for a few seconds and then vanish on the
   very next message, instead of persisting like a real historical
   event should. Fixed the same way trade events already work
   correctly: persist newly-fired markers to a bus key, day-pruned,
   and resend the full accumulated history every cycle.

Run:  python3 test_chart_marker_transitions.py
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


print("1) _institutional_to_markers: a continuously-active condition "
     "fires exactly once, not every cycle")
institutional = {"events_detail": {"false_breakout": {"active": True}}}
state = {}
total = 0
for cycle in range(5):
    markers, state = app._institutional_to_markers(institutional, anchor_ts=1000 + cycle,
                                                    prev_active=state)
    total += len(markers)
check("exactly 1 marker across 5 cycles of a persistently-active condition",
      total == 1, str(total))

print("\n2) _institutional_to_markers: a genuine OFF->ON->OFF->ON "
     "sequence re-fires on each REAL transition")
sequence = [True, True, False, False, True, True, False, True]
state2 = {}
fire_counts = []
for i, active in enumerate(sequence):
    inst = {"events_detail": {"false_breakout": {"active": active}}}
    markers, state2 = app._institutional_to_markers(inst, anchor_ts=1000 + i, prev_active=state2)
    fire_counts.append(len(markers))
check("fires on cycles 0, 4, 7 (the three real OFF->ON transitions), "
      "silent otherwise",
      fire_counts == [1, 0, 0, 0, 1, 0, 0, 1], str(fire_counts))

print("\n3) _smart_money_to_markers has the exact same fix, keyed by "
     "composite (event type + strike) since it carries a LIST of "
     "entries per category, not a single flag")
sm = {"strong_call_writing": [{"strike": 23900, "oi_chg_pct": 15}]}
sm_state = {}
sm_total = 0
for cycle in range(4):
    markers, sm_state = app._smart_money_to_markers(sm, anchor_ts=1000 + cycle,
                                                     prev_active=sm_state)
    sm_total += len(markers)
check("exactly 1 marker across 4 cycles of a persistent strike-level event",
      sm_total == 1, str(sm_total))

print("\n4) a DIFFERENT strike appearing while the first is still active "
     "correctly fires its own new marker (composite keying works)")
sm2 = {"strong_call_writing": [{"strike": 23900, "oi_chg_pct": 15},
                               {"strike": 24000, "oi_chg_pct": 20}]}
markers2, sm_state2 = app._smart_money_to_markers(sm2, anchor_ts=2000, prev_active=sm_state)
check("the new strike (24000) fires while the old one (23900) doesn't "
      "re-fire",
      len(markers2) == 1 and "24000" in markers2[0]["text"], str(markers2))

print("\n5) THE COMPOUNDING BUG: a marker that fires once must still "
     "PERSIST across subsequent cycles (the client replaces its whole "
     "marker array per message — a marker only sent once would flash "
     "and vanish, not stay on the chart like a real event should)")
symbol = "NIFTYMARKERTEST"
app.pilot.bus.set(f"institutional_events:{symbol}", [])
today_str = agents.now_ist().date().isoformat()


def simulate_cycle(inst, active_state):
    inst_markers, active_state = app._institutional_to_markers(
        inst, anchor_ts=1000, prev_active=active_state)
    if inst_markers:
        persisted = app.pilot.bus.get(f"institutional_events:{symbol}", [])
        persisted = [m for m in persisted if m.get("day") == today_str]
        for m in inst_markers:
            persisted.append(dict(m, day=today_str))
        app.pilot.bus.set(f"institutional_events:{symbol}", persisted[-30:])
    persisted_today = app.pilot.bus.get(f"institutional_events:{symbol}", [])
    sent = [{k: v for k, v in m.items() if k != "day"}
           for m in persisted_today if m.get("day") == today_str]
    return sent, active_state


inst = {"events_detail": {"false_breakout": {"active": True}}}
inst_state = {}
sent0, inst_state = simulate_cycle(inst, inst_state)
check("cycle 0: the marker is sent (genuine first occurrence)",
      len(sent0) == 1, str(sent0))
sent1, inst_state = simulate_cycle(inst, inst_state)
check("cycle 1: the SAME marker is STILL sent (persisted), not vanished "
      "just because no new transition fired this cycle",
      len(sent1) == 1, str(sent1))
sent2, inst_state = simulate_cycle(inst, inst_state)
check("cycle 2: still persists, not duplicated (still exactly 1, not "
      "growing without bound either)",
      len(sent2) == 1, str(sent2))

print("\n6) day-pruning: an event from a PRIOR day doesn't leak into "
     "today's persisted list (same class of bug as the original "
     "anchor-timestamp fix, guarded against here too)")
app.pilot.bus.set(f"institutional_events:{symbol}", [
    {"time": 900, "position": "aboveBar", "color": "#d29922", "shape": "circle",
    "text": "False Breakout", "day": "2020-01-01"}])
sent3, _ = simulate_cycle({"events_detail": {}}, {})
check("a stale prior-day entry is pruned, not carried forward forever",
      all(True for m in sent3) and len(sent3) == 0, str(sent3))

# cleanup
app.pilot.bus.set(f"institutional_events:{symbol}", [])

print("\n9) THE REAL REPORTED BUG: 'False Breakout count increases on "
     "every refresh.' Root cause was the transition-tracking state "
     "being a PER-CONNECTION local variable that reset to {} on every "
     "new websocket connection (i.e. every page refresh), while the "
     "persisted marker list it feeds survives across connections —")
symbol_refresh = "REFRESHTEST"
today_str_refresh = agents.now_ist().date().isoformat()
inst_state_key = f"institutional_active_state:{symbol_refresh}"
events_key = f"institutional_events:{symbol_refresh}"
app.pilot.bus.set(inst_state_key, {})
app.pilot.bus.set(events_key, [])
institutional_refresh = {"events_detail": {"false_breakout": {"active": True}}}


def simulate_cycle_via_bus():
    """Mirrors exactly what the fixed websocket handler now does —
    reading/writing transition state via the bus, not a local variable
    that a new connection would reset."""
    wrapper = app.pilot.bus.get(inst_state_key, {})
    prev_state = wrapper.get("state", {}) if wrapper.get("day") == today_str_refresh else {}
    inst_markers, new_state = app._institutional_to_markers(
        institutional_refresh, anchor_ts=1000, prev_active=prev_state)
    app.pilot.bus.set(inst_state_key, {"day": today_str_refresh, "state": new_state})
    if inst_markers:
        persisted = app.pilot.bus.get(events_key, [])
        persisted = [m for m in persisted if m.get("day") == today_str_refresh]
        for m in inst_markers:
            persisted.append(dict(m, day=today_str_refresh))
        app.pilot.bus.set(events_key, persisted[-30:])
    return inst_markers


m1 = simulate_cycle_via_bus()
check("cycle 1 (genuine first occurrence) fires exactly once",
      len(m1) == 1, str(len(m1)))
m2 = simulate_cycle_via_bus()
check("cycle 2, simulating a page refresh with the condition still "
      "active, does NOT fire again — the bus-persisted state survives "
      "the 'reconnect', unlike the old per-connection variable would have",
      len(m2) == 0, str(len(m2)))
m3 = simulate_cycle_via_bus()
check("a second simulated refresh also does not re-fire",
      len(m3) == 0, str(len(m3)))
final_persisted = app.pilot.bus.get(events_key, [])
check("exactly 1 marker total persisted across 3 cycles including 2 "
      "simulated refreshes — NOT 3, which is what the actual reported "
      "bug looked like",
      len(final_persisted) == 1, str(len(final_persisted)))

print("\n10) source-level guard: the fix actually moved this state to "
     "the bus, not just renamed a local variable")
app_src_check = open("app.py").read()
check("state is read from the bus with a day-aware wrapper",
      'sm_state_wrapper = pilot.bus.get(sm_state_key, {})' in app_src_check and
      'inst_state_wrapper = pilot.bus.get(inst_state_key, {})' in app_src_check)
check("state is written back to the bus after each cycle",
      'pilot.bus.set(sm_state_key,' in app_src_check and
      'pilot.bus.set(inst_state_key,' in app_src_check)
check("no local per-connection state variable remains for this purpose",
      "institutional_active_state = {}\n" not in app_src_check)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
