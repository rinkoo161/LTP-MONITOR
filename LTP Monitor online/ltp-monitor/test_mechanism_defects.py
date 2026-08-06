#!/usr/bin/env python3
"""test_mechanism_defects.py — phase 2.

Two defects that between them cost 78% of the directional-option loss
since the per-trade caps came in (-Rs 1,541 of -Rs 1,982 over 27
positions), while the trades that exited on real stops and targets ran
at roughly break-even.

DEFECT 1 — spot_invalidation set inside tick noise.

    strike     inv        distance    outcome
    78800   78604.0      196 pts     trailing stop      -523
    24650   24541.8      108 pts     stoploss          -1714
    78800   78752.6       47 pts     profit floor       +366
    78900   78859.8       40 pts     AI advisory         -68
    78800   78791.6        8 pts     SPOT INVALIDATION   -60  (same second)
    24650   24650.0        0 pts     SPOT INVALIDATION  -226  (AT the strike)
    24650   24648.05       2 pts     SPOT INVALIDATION   -79  (1-second hold)

    all-time: 12 closes on this exit, -Rs 2,368, median hold 82s, 0 wins

Every trade that died on it came from the tight group. The floor is ONE
ATR — a reason ("inside one ATR, one ordinary bar invalidates it"), not
a threshold fitted to the losers above. config.py:147 records why that
distinction matters.

DEFECT 2 — the AI auto-exit closed positions on trends that FAVOURED
them, five times out of five, across three sessions and both
directions: every PE because the market was falling, every CE because
it was rising.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_mechanism_defects")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import analyzer
import config

CFG = dict(config.DEFAULTS)
CFG.update(signal_min_rr=2.0, signal_entry_tolerance_pct=10.0,
           signal_entry_rescale_max_pct=40.0,
           signal_invalidation_min_pct=0.15)


def _an(spot=24650.0, atr_pct=None, ltp=150.0):
    a = {"spot": spot, "atm": 24650.0,
         "strikes": [{"strike": 24650.0,
                      "ce": {"ltp": ltp}, "pe": {"ltp": ltp}}]}
    if atr_pct is not None:
        a["atr_pct"] = atr_pct
    return a


def _sig(direction="BUY_CE", inv=24650.0, entry=150.0):
    return {"signal": direction, "strike": 24650.0, "entry": entry,
            "stoploss": 105.0, "target1": 240.0, "target2": 300.0,
            "spot_invalidation": inv, "confidence": 80}


print("1) DEFECT 1 — the live cases are repaired")
for label, direction, inv in (
        ("CE, level AT the strike (0 pts)", "BUY_CE", 24650.0),
        ("CE, 2 pts away", "BUY_CE", 24648.05),
        ("PE, 2 pts away", "BUY_PE", 24652.0)):
    out, reps = analyzer.enforce_signal_invariants(
        _sig(direction, inv), _an(), cfg=CFG)
    got = out["spot_invalidation"]
    dist = (24650.0 - got) if direction == "BUY_CE" else (got - 24650.0)
    check(f"{label:34} -> widened", dist >= 24650.0 * 0.15 / 100 - 0.01,
          f"now {dist:.1f} pts away ({got})")
    check(f"{label:34}    direction kept",
          (got < 24650.0) if direction == "BUY_CE" else (got > 24650.0),
          f"{got} — a CE invalidates BELOW spot, a PE ABOVE")

print("\n2) a SENSIBLE level is left alone")
out, reps = analyzer.enforce_signal_invariants(
    _sig("BUY_CE", 24541.8), _an(), cfg=CFG)
check("108 pts away is untouched", out["spot_invalidation"] == 24541.8,
      f"{out['spot_invalidation']} — that trade lived to be judged on "
      f"direction; a repair firing on it would be a bug")

print("\n3) the floor is ATR when ATR is LARGER, not a fixed number")
# atr_pct 0.5% on 24650 = 123 pts, well beyond the 0.15% floor (37 pts)
out, _ = analyzer.enforce_signal_invariants(
    _sig("BUY_CE", 24600.0), _an(atr_pct=0.5), cfg=CFG)
d = 24650.0 - out["spot_invalidation"]
check("a 50-pt level is widened to one ATR", d >= 122,
      f"{d:.1f} pts — inside one ATR means one ordinary bar invalidates it")
# and when ATR is tiny the floor still applies
out2, _ = analyzer.enforce_signal_invariants(
    _sig("BUY_CE", 24649.0), _an(atr_pct=0.01), cfg=CFG)
d2 = 24650.0 - out2["spot_invalidation"]
check("with a tiny ATR the floor still binds", d2 >= 36,
      f"{d2:.1f} pts — the floor is what stops a 0.01% ATR passing "
      f"a 1-point level")

print("\n4) the repair says what it did")
out3, reps3 = analyzer.enforce_signal_invariants(
    _sig("BUY_CE", 24650.0), _an(), cfg=CFG)
check("a repair line is recorded",
      any("spot_invalidation" in r for r in reps3), str(reps3)[:150])

print("\n5) the prompt no longer tells the model to emit spot")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "analyzer.py")).read()
# CODE lines only. The first version of this check matched the
# explanatory COMMENT that quotes the old placeholder, and failed
# against a prompt that was already fixed. Second comment-matching slip
# of the day, after test_symbol_hold's gate-label check.
_code = [l for l in SRC.split("\n") if not l.strip().startswith("#")]
check('the placeholder is not "<spot>"',
      not any('"spot_invalidation":<spot>' in l for l in _code),
      "that placeholder reads as an instruction to output the spot price, "
      "which is exactly what the model did")
check("and it now describes what the field MEANS",
      any("index level that invalidates this trade" in l for l in _code),
      "every other placeholder describes its value; this one said <spot>")

print("\n6) DEFECT 2 — an EXIT citing a move that FAVOURS the position")
f = agents.ai_exit_contradicts_position
check("CE + market UP is contradictory", f("CE", "UP"),
      "a rising index is what a call wants")
check("PE + market DOWN is contradictory", f("PE", "DOWN"),
      "a falling index is what a put wants")
check("CE + market DOWN is a legitimate exit", not f("CE", "DOWN"))
check("PE + market UP is a legitimate exit", not f("PE", "UP"))
for d in ("FLAT", "", None, "sideways"):
    check(f"no opinion on {d!r} -> does not block", not f("CE", d),
          "absent or unparseable direction must not silently block every "
          "exit — that would be a different outage")

print("\n7) the guard is WIRED, and blocks only the AUTOMATIC exit")
AG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
body = AG.split("    def _option_ai_check(self, p, sym, ltp):")[1]
body = body[:body.index("\n    def ")]
check("the check is called", "ai_exit_contradicts_position(" in body)
i_alert = body.index('AI suggests exiting')
i_guard = body.index("ai_exit_contradicts_position(")
check("the ADVISORY alert still fires first", i_alert < i_guard,
      "a human can weigh 'market trending up, consider exiting your "
      "call'; an automatic exit acting on it cannot")
check("a blocked auto-exit is logged", "auto-exit BLOCKED" in body)
check("the model is asked for market_dir as a FIELD",
      'market_dir' in body,
      "regexing prose would have to guess at negation and hedging the "
      "model was never constrained on")
check("and the prompt states the CE/PE relationship",
      "A CALL (CE) GAINS when the index RISES" in body,
      "it never did — which is why the model treated any trend as a "
      "reason to exit")

print("\n8) minimum hold before an AUTOMATIC exit")
check("a minimum hold is enforced", "option_ai_min_hold_sec" in body)
check("and it is registered in DEFAULTS",
      "option_ai_min_hold_sec" in config.DEFAULTS,
      "config.save() silently drops unregistered keys")
check("a deferred auto-exit is logged", "auto-exit DEFERRED" in body,
      "FINNIFTY 26900 CE opened 15:01:33 and closed 15:01:34 on "
      "'shows no profit' — vacuous one second in")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all mechanism-defect checks passed")
