#!/usr/bin/env python3
"""test_no_trade_guard.py — v59.82, the non-actionable signal allowlist.

`enforce_signal_invariants` stamps sig["signal"] = "NO_TRADE" when a
signal's entry premium fails the band check (analyzer.py). That value
was WRITTEN in one place and READ in none, so it did not stop the
trade — it travelled:

    StrategyAgent   `if sig["signal"] != "WAIT"`   -> denylist, passes
    evaluate()      direction gates are if/elif with NO else
                                                   -> SKIPPED, not failed
    _enter          duplicate-entry lock is a CE/PE allowlist
                                                   -> lock DISABLED
    _place          `leg = "CE" if ... == "BUY_CE" else "PE"`
                                                   -> silently books PE

...while orders.place() used the security_id stamped by
_attach_security_id, which runs BEFORE the invariant layer and so names
the ORIGINAL leg. A CE order booked as a PE position, its P&L tracked
off the PE row: precisely the wrong-instrument fill the band check
exists to prevent.

The literals here are SCRAPED from the producer, not invented — a test
that makes up its own "NO_TRADE" string cannot detect the producer
renaming it.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_no_trade_guard")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


ANALYZER_SRC = open("analyzer.py").read()
AGENTS_SRC = open("agents.py").read()

# ---------------------------------------------------------------- producer
# Scrape the literal the invariant layer actually assigns. If someone
# renames it, this test must follow the producer, not its own memory.
_produced = re.findall(r'sig\["signal"\]\s*=\s*"([A-Z_]+)"', ANALYZER_SRC)
check("analyzer still stamps a non-actionable signal value",
      bool(_produced), f"found {_produced}")
NON_ACTIONABLE = _produced[0] if _produced else "NO_TRADE"
check("the stamped value is not an actionable one",
      NON_ACTIONABLE not in ("BUY_CE", "BUY_PE"), NON_ACTIONABLE)

# ------------------------------------------------------- layer 1: strategy
# The publish-side filter must be an ALLOWLIST. A denylist admits every
# value nobody thought of, which is how NO_TRADE got through.
_strat = re.search(r'if sig\["signal"\](.{0,60}?) and \\', AGENTS_SRC)
check("StrategyAgent filter located", _strat is not None)
if _strat:
    _expr = _strat.group(1)
    check("StrategyAgent uses an allowlist, not `!= WAIT`",
          "in (" in _expr and "!=" not in _expr, _expr.strip())
    check("StrategyAgent allowlist names both actionable legs",
          "BUY_CE" in _expr and "BUY_PE" in _expr, _expr.strip())

# --------------------------------------------------------- layer 2: risk
# A non-actionable signal must be REJECTED with a stated reason, not
# silently skip the direction gates.
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

    def publish(self, *a, **k):
        pass

    def subscribe(self, *a, **k):
        pass


_risk = agents.RiskAgent.__new__(agents.RiskAgent)
_risk.bus = _Bus()
_risk.halted = False
_risk.last_loss_ts = 0
_risk.consecutive_losses = 0
_risk.daily_pnl = 0.0
_risk.name = "risk"

_job = {"symbol": "NIFTY",
        "signal": {"signal": NON_ACTIONABLE, "strike": 24350, "confidence": 90,
                   "entry": 100.0, "stoploss": 60.0, "target1": 180.0},
        "analysis": {"atm": 24350, "strikes": [], "spot": 24350}}

try:
    _ok, _checks = _risk.evaluate(_job)
    # NOT discriminating on its own — verified against the pre-fix tree,
    # where it still passed because this fixture also trips `market
    # open` and the per-trade cap. Kept to document intent; the check
    # BELOW is the one that actually detects the regression, so do not
    # take a green here as evidence the value gate exists.
    check(f"evaluate() REJECTS a {NON_ACTIONABLE} job", _ok is False)
    _fails = [c for c in _checks if c.startswith("✗")]
    check("the refusal names the offending value",
          any(NON_ACTIONABLE in c for c in _fails),
          "; ".join(_fails[:3]) or "no ✗ lines at all")
except Exception as e:                                    # pragma: no cover
    check(f"evaluate() handles a {NON_ACTIONABLE} job", False, repr(e))

# A well-formed BUY_CE must still reach the LATER checks — proving the
# new gate rejects on the value alone and has not become a blanket deny.
_job_ok = {**_job, "signal": {**_job["signal"], "signal": "BUY_CE"}}
try:
    _ok2, _checks2 = _risk.evaluate(_job_ok)
    check("a BUY_CE job is not refused by the signal-value gate",
          not any("actionable signal" in c and c.startswith("✗")
                  for c in _checks2))
    check("a BUY_CE job still runs the downstream gates",
          len(_checks2) > 3, f"{len(_checks2)} checks ran")
except Exception as e:                                    # pragma: no cover
    check("evaluate() handles a BUY_CE job", False, repr(e))

# ------------------------------------------------------ layer 3: execution
# The guard must sit ABOVE the leg mapping, or it guards nothing.
_pl = AGENTS_SRC.index("def _place(self, job, manual=False):")
_body = AGENTS_SRC[_pl:_pl + 6000]
check("_place body sliced", len(_body) > 1000, f"{len(_body)} chars")
_guard = _body.find('not in ("BUY_CE", "BUY_PE")')
_legmap = _body.find('leg = "CE" if')
check("_place has a non-actionable refusal", _guard != -1)
check("_place has the leg mapping this protects", _legmap != -1)
check("the refusal precedes the leg mapping",
      _guard != -1 and _legmap != -1 and _guard < _legmap,
      f"guard@{_guard} legmap@{_legmap}")

_ret = _body.find("return", _guard) if _guard != -1 else -1
check("the refusal RETURNS rather than falling through",
      _ret != -1 and (_ret - _guard) < 200, f"return at +{_ret - _guard}")

# It must run before any state is mutated: the re-entry cooldown stamp,
# the capital draw and the symbol claim all come after.
_cooldown = _body.find("option_reentry_cooldown_sec")
check("the refusal runs before the re-entry cooldown read",
      _guard != -1 and _cooldown != -1 and _guard < _cooldown,
      f"guard@{_guard} cooldown@{_cooldown}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
