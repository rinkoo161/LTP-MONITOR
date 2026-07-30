"""v58.7 — tests for disabling the futures SIGNAL engine (auto-deploy
and manual "Fire Now") by direct instruction after real trading data
showed every futures trade closing at a loss or exact breakeven, none
via its own profit target. Futures OI-buildup/price DATA collection
must remain completely unaffected — it's still used as a supportive
input for other strategies (MTF Confluence).

Also closes a real gap found while making this change: the manual
"Fire Now" endpoint called the pure eligibility function directly and
never checked futures_strategy_enabled at all — only the automatic
engine did. Turning the flag off in Settings would have stopped
auto-deploy but NOT the manual button.

Run:  python3 test_futures_signal_disabled.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import agents
import app
import config

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)

print("1) the default changed to off, by direct instruction from real data")
check("futures_strategy_enabled defaults to False now",
      config.DEFAULTS.get("futures_strategy_enabled") is False)

print("\n2) the automatic signal engine respects the new default")
_before = config.load().get("futures_strategy_enabled")
_before_auto = config.load().get("futures_auto_deploy")
config.save({"futures_strategy_enabled": False, "futures_auto_deploy": True})
ex = agents.ExecutionAgent(agents.Bus(), {})
real_market_open = agents.market_open
agents.market_open = lambda: True
try:
    result = ex._futures_signal_engine("NIFTY")
    check("automatic engine returns immediately (no signal proposed/placed) "
          "when the flag is off, even with auto_deploy on",
          result is None, str(result))
finally:
    agents.market_open = real_market_open

print("\n3) THE REAL GAP: the manual 'Fire Now' endpoint previously "
     "bypassed this flag entirely (called the pure eval function "
     "directly) — now closed")
app.pilot.agents = [ex]
app.pilot.running = True
try:
    r = client.post("/api/futures/manual_deploy", json={"symbol": "NIFTY"})
    check("manual endpoint now returns a clear error when the flag is "
          "off, not a silent bypass into a real trade",
          "error" in r.json() and "disabled" in r.json()["error"].lower(),
          str(r.json()))
finally:
    app.pilot.agents = []
    app.pilot.running = False

print("\n4) futures OI-buildup/price DATA collection is completely "
     "unaffected — structural check, not just a runtime guess")
src = open("agents.py").read()
m = re.search(r'class MarketDataAgent.*?\n(class \w+Agent|\Z)', src, re.S)
market_data_body = m.group(0)
check("futures_strategy_enabled does not appear anywhere in "
      "MarketDataAgent (the class that polls/classifies futures data)",
      "futures_strategy_enabled" not in market_data_body)
check("_classify_future_tick (OI-buildup classification) exists and is "
      "unrelated to the signal-engine flag",
      "def _classify_future_tick" in market_data_body)
check("_poll_futures_via_rest (the data poller) exists in the same "
      "unaffected class",
      "_poll_futures_via_rest" in market_data_body)

print("\n5) all remaining inline fallback defaults for this flag are "
     "consistent with the new registered default (no stale True "
     "fallback left anywhere)")
app_src = open("app.py").read()
check("app.py has no remaining 'futures_strategy_enabled', True) fallback",
      'futures_strategy_enabled", True)' not in app_src)
check("agents.py has no remaining 'futures_strategy_enabled', True) fallback",
      'futures_strategy_enabled", True)' not in src)

print("\n6) frontend: the Fire Now button is honestly disabled when the "
     "flag is off, not a clickable control that would just error")
h = open("static/dashboard.html").read()
check("row tracks signalEnabled from the backend's enabled field",
      "signalEnabled: (d.futures_signal||{}).enabled!==false" in h)
check("Fire Now button only renders when signalEnabled is true",
      "r.signalEnabled?" in h)
check("an honest disabled message shown otherwise, not a dead button",
      "disabled \\u2014 analysis only" in h)

print("\n" + "=" * 60)
config.save({"futures_strategy_enabled": _before, "futures_auto_deploy": _before_auto})
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
