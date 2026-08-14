#!/usr/bin/env python3
"""test_journal_review_fixes.py — v59.87.

Covers the three findings from the 14-Aug journal review that were
valid, plus the coupling trap that renaming exposed.

The review's headline (a BANKNIFTY per-trade risk-cap bypass) was NOT
valid and is deliberately not tested here: that trade entered at
14:03:49, over four hours after the cap was raised 2,000 -> 10,000 at
09:46:51, and its ₹3,936 stop-risk was inside the cap actually in
force. The reconstruction assumed a cap that had not applied since
mid-morning. What IS tested is the invariant the review asked for —
one authoritative risk calculation — which lives in
test_planned_lots_sync.py.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_journal_review_fixes")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


SRC = open("agents.py").read()

print("1) the daily budget label and its matcher cannot drift apart")
# StrategyAgent's 15-minute backoff arms by MATCHING the risk agent's
# ✗ text. Renaming the label without renaming the matcher would
# silently stop the backoff arming for the reason that fires most
# often, and nothing would report it.
check("DAILY_BUDGET_LABEL is a single shared constant",
      "DAILY_BUDGET_LABEL = " in SRC)
check("the risk gate builds its label from the constant",
      'f"{DAILY_BUDGET_LABEL}' in SRC)
check("the backoff matcher uses the constant, not a copy of the text",
      "hard_reasons = (DAILY_BUDGET_LABEL," in SRC,
      "a literal here would drift the moment the label is reworded")
check("no stale 'daily loss limit' label remains in the check text",
      'f"daily loss limit' not in SRC,
      "the control is unchanged; only the NAME was wrong")

print("\n2) confidence states its failure in its own words")


class _Bus:
    def __init__(self):
        self._d = {}

    def get(self, k, d=None):
        return self._d.get(k, d)

    def set(self, k, v):
        self._d[k] = v

    def log(self, *a, **k):
        pass

    def alert(self, *a, **k):
        pass


def confidence_line(conf):
    r = agents.RiskAgent.__new__(agents.RiskAgent)
    r.bus, r.halted, r.last_loss_ts = _Bus(), False, 0
    r.consecutive_losses, r.daily_pnl, r.name = 0, 0.0, "risk"
    job = {"symbol": "NIFTY",
           "signal": {"signal": "BUY_CE", "strike": 24300, "confidence": conf,
                      "entry": 100.0, "stoploss": 90.0, "target1": 120.0},
           "analysis": {"atm": 24300, "strikes": [], "spot": 24300}}
    _ok, checks = r.evaluate(job)
    return next((c for c in checks if "confidence" in c), "")


MIN = config.DEFAULTS["min_confidence"]
line = confidence_line(MIN - 5)
check(f"a failing confidence reads as a failure, not a requirement",
      "<" in line and "required" in line, line)
check("...and is marked ✗", line.startswith("✗"), line)
line_ok = confidence_line(MIN + 5)
check("a passing confidence is marked ✓", line_ok.startswith("✓"), line_ok)

print("\n   boundary cases the review asked for:")
for conf, want_pass in ((MIN - 0.1, False), (MIN, True), (MIN + 0.1, True)):
    ln = confidence_line(conf)
    got = ln.startswith("✓")
    check(f"   confidence {conf} -> {'pass' if want_pass else 'fail'}",
          got == want_pass, ln)
try:
    ln = confidence_line(0)
    check("   confidence 0 -> fail, and does not raise", ln.startswith("✗"), ln)
except Exception as e:
    check("   confidence 0 does not raise", False, repr(e))

print("\n3) the shadow stats separate conclusive outcomes from timeouts")
APP = open("app.py").read()
check("rejected_conclusive is exposed", '"rejected_conclusive"' in APP,
      "the review read 76 'resolved' when 38 were conclusive and 38 "
      "timed out — because the field counting timeouts was named "
      "'resolved'")
_i = APP.find('"risk_agent_accuracy_pct"')
_j = APP.find('"rejected_conclusive"')
check("it sits with the accuracy it is the denominator of",
      _i != -1 and _j != -1 and abs(_i - _j) < 900)
H = open("static/dashboard.html").read()
check("the dashboard says accuracy is rejections-only, not system P&L",
      "rejections only" in H and "NOT system" in H,
      "92.1% accuracy on a day that lost ₹3,518 is the framing to kill")
check("the dashboard shows the conclusive count",
      "rejected_conclusive" in H)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
