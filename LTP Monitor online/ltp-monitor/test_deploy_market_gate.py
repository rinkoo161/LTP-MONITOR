#!/usr/bin/env python3
"""test_deploy_market_gate.py — a deploy endpoint must refuse a closed
market, not rely on the button being greyed out.

2026-08-03, from the live log:

    23:10  [regime] broker returned no candles (market closed)
           — using 400 persisted bars from the local DB instead
    23:13  POST /api/strategies/deploy -> 200 OK
    23:13  PAPER SPREAD bull_put_spread NIFTY ... credit 36.65 x 65
    23:13  SPREAD CLOSED — market closed — forced square-off — net -120

The button was pressed; the SERVER should have refused. The endpoint
already carried a guard whose comment says a deploy "must never be
evaluated against the last session's regime… enforced here rather than
only by disabling the button, because this endpoint is directly
callable" — the right instinct, covering the wrong failure. It tests
`if not regime`, and out of hours RegimeAgent REBUILDS a present-looking
regime from persisted bars, so the guard passes and nothing behind it
checks the clock.

Two different failures — a STALE regime and a CLOSED market — and only
one was covered. `_auto_spreads` had the market check all along
(`or not market_open()`); only the manual door was open. 4 of 173 spread
opens ever were out of hours.

The test drives the real endpoint through TestClient rather than reading
the source, because "the string is present" is what the previous guard
would also have passed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_deploy_market_gate")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import app as appmod
from fastapi.testclient import TestClient

client = TestClient(appmod.app)
_real_open = agents.market_open
_real_running = appmod.pilot.running


def deploy():
    return client.post("/api/strategies/deploy",
                       json={"symbol": "NIFTY", "name": "bull_put_spread"})


print("1) with the market CLOSED the endpoint refuses")
try:
    appmod.pilot.running = True
    agents.market_open = lambda: False
    r = deploy()
    body = r.json()
    check("it responds without raising", r.status_code == 200, str(r.status_code))
    check("and returns an error rather than deploying",
          "error" in body, str(body)[:120])
    check("the error names the market being closed",
          "closed" in str(body.get("error", "")).lower(),
          str(body.get("error"))[:110])
    # The specific regression: a PRESENT-but-out-of-hours regime must not
    # satisfy the gate. Publishing one is exactly what RegimeAgent does
    # out of hours, from persisted bars.
    appmod.pilot.bus.set("regime:NIFTY", {"regime": "trending-up",
                                          "session_date": "2026-08-03",
                                          "atr_pct": 0.08})
    appmod.pilot.bus.set("analysis:NIFTY", {"spot": 24774.3, "strikes": []})
    r2 = deploy()
    b2 = r2.json()
    check("a populated regime does NOT unlock it out of hours",
          "error" in b2 and "closed" in str(b2.get("error", "")).lower(),
          str(b2)[:120] + "  <- the exact 23:13 path")

    print("\n2) with the market OPEN the gate is not what stops it")
    agents.market_open = lambda: True
    r3 = deploy()
    b3 = r3.json()
    err = str(b3.get("error", ""))
    check("the market-closed error is gone",
          "market is closed" not in err.lower(),
          err[:110] or "(no error — it proceeded past the gate)")
finally:
    agents.market_open = _real_open
    appmod.pilot.running = _real_running

print("\n3) the auto path keeps its own long-standing gate")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "agents.py")).read()
_auto = SRC.split("def _auto_spreads")[1][:1200]
check("_auto_spreads still checks market_open()", "market_open()" in _auto,
      "this one was never the problem and must not be disturbed")

print("\n4) the regime guard is still there — it was necessary, "
      "just not sufficient")
ASRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "app.py")).read()
# Slice the FUNCTION, not a byte count. The first version of this test
# used [:2600] and failed against correct code the moment the guard's
# own comment block pushed the asserted line past the window — the same
# brittleness fixed in test_futures_oi_archive.py earlier the same day,
# repeated here within hours of writing that fix down.
_after = ASRC.split("def api_strategies_deploy")[1]
_ep = _after[:_after.index("\n@app.")] if "\n@app." in _after else _after
check("the stale-regime guard survives", "regime_last_session:" in _ep,
      "a deploy must not be evaluated against last session's regime")
check("and the market check sits BEFORE the work",
      _ep.index("agents.market_open()") < _ep.index("regime_last_session:"),
      "fail fast, before analysis/eligibility is computed")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all deploy market-gate checks passed")
