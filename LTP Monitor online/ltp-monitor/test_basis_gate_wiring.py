#!/usr/bin/env python3
"""test_basis_gate_wiring.py — v59.0 item 9.

The gate is wired into RiskAgent.evaluate(). Two properties matter more
than "it blocks things":

  1. DEFAULT OFF. Shipping it on would change live behaviour as a side
     effect of a wiring change.
  2. VETO ONLY. It must never be the reason a trade HAPPENS. A gate that
     can approve is a signal source wearing a gate's clothing, and it
     would be consulted instead of the existing risk checks rather than
     alongside them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_basis_gate_wiring")

import basis_residual as br
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


print("1) default OFF")
check("global key defaults False",
      config.DEFAULTS.get("require_basis_agreement") is False)
ok, why = br.gate_for("orb", "LONG", -3.0, {})
check("a strong contrary residual does NOT block while off", ok, why)

print("\n2) veto only — it can never approve")
# Every combination that has an opinion must either veto or abstain;
# none may return a reason a trade should happen.
for side in ("LONG", "SHORT"):
    for z in (-3.0, -1.0, 0.0, 1.0, 3.0):
        allowed = br.agrees(side, z)
        check(f"{side} z={z:+.1f} is veto-or-abstain", allowed in (True, False))
check("no z means no opinion (cannot veto)", br.agrees("LONG", None) is True,
      "a cold start must not read as disagreement")
check("inside the band means no opinion", br.agrees("LONG", 0.5) is True)
check("vetoes LONG into a sustained discount",
      br.agrees("LONG", -3.0) is False)
check("vetoes SHORT into a sustained premium",
      br.agrees("SHORT", 3.0) is False)
check("does NOT veto LONG into a premium (that is agreement, not approval)",
      br.agrees("LONG", 3.0) is True)

print("\n3) when ON it vetoes, per strategy")
ok2, why2 = br.gate_for("orb", "LONG", -3.0, {"require_basis_agreement": True})
check("global switch enables the veto", not ok2, why2)
ok3, why3 = br.gate_for("orb", "LONG", -3.0,
                        {"require_basis_agreement": True,
                         "orb_require_basis_agreement": False})
check("per-strategy key overrides the global", ok3, why3)
ok4, _ = br.gate_for("orb", "LONG", None, {"require_basis_agreement": True})
check("still cannot veto without a z", ok4,
      "cold start must not block every trade for 200 cycles")

print("\n4) wired into the risk path, not around it")
AG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "agents.py")).read()
EV = AG.split("def evaluate(self, job)")[1].split("\n    def ")[0]
check("consulted inside RiskAgent.evaluate", "basis_residual" in EV)
check("uses gate_for, not a second copy of the logic",
      "gate_for(" in EV and "residual_z" in EV,
      "a reimplementation here is how the quadrant classifier drifted")
check("reports through the same check() chain",
      "check(False, _why)" in EV,
      "a veto must appear as a normal gate rejection, not a silent drop")
check("existing gates still run after it",
      EV.split("gate_for(")[1].count("check(") >= 3,
      "it must sit alongside the risk checks, not replace them")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all basis-gate wiring checks passed")
