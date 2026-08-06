#!/usr/bin/env python3
"""test_symbol_hold.py — per-symbol HOLD: data continues, entries stop.

Measured across 292 closed trades on 2026-08-06:

    by symbol, all time        n     total    win%
    BANKNIFTY                 61   -40,781    28%
    FINNIFTY                  82   -20,569    51%
    NIFTY                     72   -10,285    53%
    SENSEX                    77    -4,167    47%

    since the per-trade caps (2026-08-01)
    BANKNIFTY                  3      -768     0%

BANKNIFTY held on explicit instruction. The mechanism matters as much
as the decision:

  * A hold is NOT "drop it from the bus symbols list". That list drives
    market data, analysis, regime, chain snapshots and the archive as
    well as trading — dropping a name there would stop collecting the
    evidence needed to decide whether the hold was right.

  * It must cover ALL THREE entry paths. Spreads reach the book via
    _auto_spreads() -> enter_spread() WITHOUT passing through
    RiskAgent.evaluate() — a documented hole in this codebase. Guarding
    only the risk gate would stop BANKNIFTY options while leaving its
    spreads trading.

  * EXITS ARE NEVER BLOCKED. A pause that could strand an open position
    would be worse than the losses it prevents. That is the check this
    file exists to defend.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_symbol_hold")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config

HELD, FREE = "BANKNIFTY", "NIFTY"

print("1) the predicate itself")
cfg = config.load()
cfg["paused_symbols"] = [HELD]
config.save(cfg)
check("a held symbol reads as paused", agents.symbol_paused(HELD))
check("another symbol does not", not agents.symbol_paused(FREE))
check("matching is case-insensitive", agents.symbol_paused("banknifty"),
      "a lowercase entry in Settings must not silently disable the hold")
check("an empty list holds nothing",
      not agents.symbol_paused(HELD, {"paused_symbols": []}))
check("None/blank symbols are safe", not agents.symbol_paused(None))
check("the key is registered in DEFAULTS", "paused_symbols" in config.DEFAULTS,
      "config.save() silently drops unregistered keys — the hold would "
      "vanish on the next save")

# 2026-08-06, shipped and caught live: the risk-gate label read
# "{sym} is on hold (paused_symbols)", so an APPROVED order rendered as
# "✓ SENSEX is on hold (paused_symbols)" — held and approved anyway.
# Every other gate on that line names the PASSING state. Same class of
# dishonest label as the 2026-08-03 "stoploss" that was really a
# profitable trail exit.
HERE0 = os.path.dirname(os.path.abspath(__file__))
_ag = open(os.path.join(HERE0, "agents.py")).read()
_gate = _ag.split("    def evaluate(self, job):")[1]
_gate = _gate[:_gate.index("\n    def ")]
# CODE lines only — the first version of this check also matched the
# comment above it, so a comment alone could have satisfied it.
_lbl = [l.strip() for l in _gate.splitlines()
        if not l.strip().startswith("#") and 'f"' in l
        and ("not on hold" in l or "is on hold" in l)]
check("a gate label about the hold exists in code", len(_lbl) == 1, str(_lbl))
check("and it names the PASSING state",
      _lbl and "not on hold" in _lbl[0] and "is on hold" not in _lbl[0],
      f"{_lbl} — a ✓ beside 'is on hold' reads as held-and-approved-anyway")

print("\n2) ALL THREE entry paths are guarded, not just the risk gate")
HERE = os.path.dirname(os.path.abspath(__file__))
AG = open(os.path.join(HERE, "agents.py")).read()


def body(marker, endmark="\n    def "):
    b = AG.split(marker)[1]
    return b[:b.index(endmark)] if endmark in b else b


for marker, name, why in (
    ("    def evaluate(self, job):", "RiskAgent.evaluate (options)",
     "the normal options path"),
    ("    def enter_spread(self, spread):", "enter_spread",
     "_auto_spreads() calls this DIRECTLY, bypassing RiskAgent.evaluate"),
    ("    def enter_future(self, symbol, side", "enter_future",
     "futures are where the -Rs 96,978 actually came from"),
):
    check(f"{name} checks the hold", "symbol_paused" in body(marker),
          why)

print("\n3) EXITS ARE NEVER BLOCKED — the property that must not break")
for marker, name in (("    def exit(self, reason=", "exit"),
                     ("    def _monitor_one(self", "_monitor_one")):
    if marker not in AG:
        print(f"  SKIP  {name} not found by that anchor")
        continue
    check(f"{name} does NOT consult the hold",
          "symbol_paused" not in body(marker),
          "a hold that can strand an open position is worse than the "
          "losses it prevents")

print("\n4) driven, not just grepped — a held symbol is refused an ENTRY")
bus = agents.Bus()
bus.set("symbols", [FREE, HELD])
ex = agents.ExecutionAgent(bus, {"orders_factory": lambda: None,
                                 "get_chain": lambda s: None})
r_held = ex.enter_future(HELD, "LONG", lots=1)
check("a held symbol's futures entry is refused",
      isinstance(r_held, dict) and "on hold" in str(r_held.get("error", "")),
      str(r_held)[:120])
r_spread = ex.enter_spread({"symbol": HELD, "name": "bull_put_spread",
                            "short_strike": 57800.0, "long_strike": 57700.0,
                            "credit": 40.0, "lots": 1})
check("a held symbol's SPREAD entry is refused",
      isinstance(r_spread, dict) and "on hold" in str(r_spread.get("error", "")),
      f"{str(r_spread)[:120]} — this is the path that bypasses the risk gate")

print("\n5) the hold is a HOLD, not a delete — data keeps flowing")
check("the held symbol is still in the bus symbols list",
      HELD in (bus.get("symbols") or []),
      "market data, analysis, regime, chain snapshots and the archive "
      "all key off this list; removing it would stop the evidence "
      "needed to decide whether to resume")

print("\n6) releasing the hold restores trading")
cfg2 = config.load()
cfg2["paused_symbols"] = []
config.save(cfg2)
check("with the list emptied, the symbol is no longer paused",
      not agents.symbol_paused(HELD),
      "a hold that cannot be lifted is a deletion")
r_after = ex.enter_future(HELD, "LONG", lots=1)
check("and its entry is no longer refused for being on hold",
      not (isinstance(r_after, dict)
           and "on hold" in str(r_after.get("error", ""))),
      str(r_after)[:120])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all symbol-hold checks passed")
