#!/usr/bin/env python3
"""test_marketsense_gate.py — a second process may veto a trade, but it
may not halt the book by falling over.

2026-08-08. `marketsense_link.py` mirrors MarketSense's risk verdicts
onto the bus, and its own docstring says the gate "belongs in
RiskAgent". Wiring it hands a SEPARATE PROCESS partial control over
whether we trade, so the failure modes matter more than the happy path.

Two real defects had to be fixed in the bridge first, both of which
would have turned this gate into a permanent phantom block:

  1. STICKY FLAGS. The loop only ever SET `ms_risk_flag:{sym}`; it never
     removed one when MarketSense stopped flagging the symbol. Harmless
     while nothing read the key — a symbol blocked forever once
     something did.
  2. NO MACHINE-READABLE FRESHNESS. `ms_link["at"]` is a "%H:%M:%S"
     string that cannot be aged across midnight. On a poll failure the
     bridge deliberately returns early and keeps the last good values on
     the bus, so a hard_block set just before an outage would outlive it
     indefinitely.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_marketsense_gate")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config

HERE = os.path.dirname(os.path.abspath(__file__))


def _risk(bus_vals):
    """A RiskAgent on the REAL Bus, seeded with the given keys.

    Deliberately not a hand-rolled stand-in: a stub that invents its own
    interface cannot detect a mismatch with the producer, and the first
    attempt at this test used one and died on Bus.subscribe() — which is
    the cheap version of the same failure this codebase has had for real
    (a test whose fixture drifts from the thing it stands in for).
    """
    bus = agents.Bus()
    for k, v in bus_vals.items():
        bus.set(k, v)
    return agents.RiskAgent(bus, {})


# Shaped from the PRODUCER, not invented: StrategyAgent publishes
# {"symbol", "signal", "analysis"} (agents.py:3451), and evaluate()
# reads exactly job["analysis"], job["signal"], job["symbol"] plus the
# sig.get(...) keys below. The first version of this test supplied only
# symbol+signal and died on KeyError: 'analysis' — the same class of
# mistake as reproducing a data structure's shape instead of its
# meaning, which this codebase has been bitten by before.
JOB = {
    "symbol": "NIFTY",
    "analysis": {"spot": 24600.0, "atm": 24600.0, "strikes": []},
    "signal": {
        "signal": "buy_ce", "type": "CE", "strike": 24600,
        "confidence": 75, "_pre_decision_confidence": 75,
        "entry": 150.0, "stoploss": 105.0, "target1": 240.0,
        "strategy": "unit_test",
        "ai_decision": None, "ai_decision_notes": None,
        "ai_probability": None, "unified_probability": None,
    },
}


def _gate_result(bus_vals):
    """Return the MarketSense line from evaluate()'s checks, or None."""
    ag = _risk(bus_vals)
    try:
        _ok, checks = ag.evaluate(dict(JOB))
    except Exception as e:
        return ("ERROR", f"{type(e).__name__}: {e}")
    for c in checks:
        if "MarketSense" in c:
            return (c[0], c)
    return None


print("1) the gate is present in the one function every order passes")
AG = open(os.path.join(HERE, "agents.py")).read()
ev = AG.split("def evaluate(self, job):")[1]
ev = ev[:ev.index("\n    def ")]
check("evaluate() consults ms_risk_flag", "ms_risk_flag:" in ev,
      "CLAUDE.md: every order passes RiskAgent.evaluate(), including "
      "manual dashboard clicks via Orchestrator.manual_trade")
check("and it is gated by a config key",
      "marketsense_risk_gate_enabled" in ev,
      "so it can be switched off without a deploy")

print("\n2) a FRESH hard_block stops the order")
r = _gate_result({"ms_link": {"ok": True, "at_ts": time.time()},
                  "ms_risk_flag:NIFTY": {"verdict": "hard_block"}})
check("hard_block produces a FAILING check", r is not None and r[0] == "✗",
      str(r))

print("\n3) advisory verdicts do NOT stop the order")
for verdict in ("penalty", "suppressed"):
    r = _gate_result({"ms_link": {"ok": True, "at_ts": time.time()},
                      "ms_risk_flag:NIFTY": {"verdict": verdict}})
    check(f"{verdict!r} does not block", r is not None and r[0] == "✓",
          f"{r} — vetoing on an advisory downgrade would let a "
          f"second-opinion service halt the book")

print("\n4) a STALE hard_block does NOT stop the order (fails open)")
old = time.time() - 10_000
r = _gate_result({"ms_link": {"ok": True, "at_ts": old},
                  "ms_risk_flag:NIFTY": {"verdict": "hard_block"}})
check("stale flag does not block", r is not None and r[0] == "✓", str(r))
check("and the label SAYS it was skipped, not that it passed",
      r is not None and "skipped" in r[1].lower(),
      f"{r} — the 2026-08-03 lesson: '✓ SENSEX is on hold' read as "
      f"held-and-approved-anyway. A tick must never imply a check ran")

print("\n5) MarketSense being DOWN does not stop trading")
r = _gate_result({"ms_link": {"ok": False, "at_ts": 0, "error": "boom"},
                  "ms_risk_flag:NIFTY": {"verdict": "hard_block"}})
check("link down -> not blocked", r is not None and r[0] == "✓",
      f"{r} — an optional advisory service must never halt trading by "
      f"falling over; llm.py already follows this rule")
r = _gate_result({})          # nothing on the bus at all
check("no MarketSense data at all -> not blocked",
      r is not None and r[0] == "✓", str(r))

print("\n6) the flag is CLEARED when MarketSense stops flagging it")
ML = open(os.path.join(HERE, "marketsense_link.py")).read()
check("the bridge tracks what it flagged", "_flagged" in ML)
check("and clears symbols no longer flagged",
      'self.bus.set(f"ms_risk_flag:{_gone}", None)' in ML,
      "a set-only flag is sticky forever — a symbol blocked on a "
      "verdict withdrawn hours ago")
check("and publishes a machine-readable timestamp",
      '"at_ts"' in ML,
      'ms_link["at"] is "%H:%M:%S" and cannot be aged across midnight')

print("\n7) the config keys exist, so DEFAULTS cannot silently drop them")
for k in ("marketsense_risk_gate_enabled", "marketsense_max_flag_age_sec"):
    check(f"{k} in DEFAULTS", k in config.DEFAULTS,
          "config.save() silently drops any key not in DEFAULTS")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all MarketSense gate checks passed")
