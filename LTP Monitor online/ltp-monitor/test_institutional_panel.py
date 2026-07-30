"""v57 — tests for the Institutional & Smart Money dashboard panel.
Confirms zero backend changes were needed (both consumed endpoints,
/api/institutional/{symbol} and /api/analysis/{symbol}, already
existed and already return everything the new page needs) and that
the graceful "not yet computed" path works before any data exists.

Run:  python3 test_institutional_panel.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import app
import analyzer
import institutional_engine as ie

results = []
SYM = "NIFTY"


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)


def build_chain(spot=23800, n=10):
    rows = []
    for i in range(-n, n + 1):
        strike = spot + i * 50
        rows.append({
            "strike": strike,
            "ce": {"ltp": max(5, 200 - i * 15) + (i % 3), "oi": 50000 + abs(i) * 1000,
                  "volume": 10000 + abs(i) * 500, "iv": 14 + abs(i) * 0.2,
                  "delta": 0.5 - i * 0.04, "gamma": 0.001, "theta": -2.0, "vega": 8.0,
                  "security_id": str(10000 + i)},
            "pe": {"ltp": max(5, 200 + i * 15) - (i % 3), "oi": 48000 + abs(i) * 900,
                  "volume": 9000 + abs(i) * 400, "iv": 14.5 + abs(i) * 0.2,
                  "delta": -0.5 + i * 0.04, "gamma": 0.001, "theta": -2.0, "vega": 8.0,
                  "security_id": str(20000 + i)}})
    return {"symbol": SYM, "spot": spot, "rows": rows, "expiry": "2026-01-29", "timestamp": 0}


print("1) graceful empty state before any data exists")
app.pilot.bus.set(f"institutional:{SYM}", None)
r = client.get(f"/api/institutional/{SYM}")
check("returns 200, not a 404/500", r.status_code == 200)
check("clear 'not available' shape, not a bare error",
      r.json().get("available") is False and "reason" in r.json(), str(r.json()))

print("\n2) real analyze() output has every field the new page reads")
analysis = analyzer.analyze(build_chain())
check("strikes present with per-leg institutional_activity",
      any(s["ce"].get("institutional_activity") is not None or
         s["pe"].get("institutional_activity") is not None
         for s in analysis["strikes"]))
check("strikes carry strike_strength (score/pct/color)",
      any(s["ce"].get("strike_strength") is not None for s in analysis["strikes"]))
check("strikes carry iv_greeks-derived iv/delta on the leg itself",
      all("iv" in s["ce"] and "delta" in s["ce"] for s in analysis["strikes"]))
check("narrative is a real list (may be empty, must be a list)",
      isinstance(analysis.get("narrative"), list))
check("smart_money has all expected event keys",
      set(analysis.get("smart_money", {}).keys()) >= {
          "strong_call_writing", "strong_put_writing", "support_shift",
          "resistance_shift", "oi_migration", "volume_breakout",
          "aggressive_buyers", "aggressive_writers"})

print("\n3) institutional_engine output has every field the summary card reads")
inst = ie.institutional_output({"bias": "Bullish", "confidence": 62}, analysis,
                               spot=23800, vwap=23800,
                               regime={"regime": "trending-up"},
                               future_oi_trend="long")
required = ("institutional_score", "institutional_bias", "money_flow",
           "money_flow_state", "participation_strength", "breakout_status",
           "breakdown_status", "confidence_pct", "ai_commentary")
check("all summary-card fields present", all(k in inst for k in required),
      str([k for k in required if k not in inst]))

print("\n4) end-to-end through the REAL HTTP endpoints (not just the "
     "Python functions in isolation)")
app.pilot.bus.set(f"analysis:{SYM}", analysis)
app.pilot.bus.set(f"institutional:{SYM}", inst)
app.pilot.bus.set(f"chain_ts:{SYM}", time.time())
r1 = client.get(f"/api/institutional/{SYM}")
check("institutional endpoint returns available=True with real data",
      r1.json().get("available") is True and r1.json().get("institutional_score") is not None,
      str(r1.json().get("institutional_score")))
r2 = client.get(f"/api/analysis/{SYM}")
d2 = r2.json()
check("analysis endpoint returns real strikes + narrative + smart_money",
      len(d2.get("strikes", [])) > 0 and "narrative" in d2 and "smart_money" in d2,
      f"strikes={len(d2.get('strikes', []))}")

print("\n5) frontend page registered correctly")
h = open("static/dashboard.html").read()
check("new nav rail button exists", 'rail-inst' in h)
check("new view container exists", 'id="view-inst"' in h)
check("showView dispatch includes 'inst'",
      re.search(r'\[\"dash\",.*\"inst\".*\]\.forEach', h) is not None)
check("loadInstitutionalPage fetches BOTH existing endpoints, no new "
      "backend endpoint was needed",
      "/api/institutional/" in h and "/api/analysis/" in h and
      "async function loadInstitutionalPage" in h)
# 2026-07-27 — renamed from loadInstitutional() to loadInstitutionalPage()
# after discovering it collided with a PRE-EXISTING same-named Feature #5
# panel loader elsewhere in the file — the later declaration was
# silently shadowing the earlier one, so the ORIGINAL panel's own
# refresh logic never ran again once this page's loader was added.
check("auto-refresh registered for the new view (renamed function, "
      "post name-collision fix)",
      'currentView==="inst")loadInstitutionalPage()' in h)
import re as _re2
check("no collision remains: the page loader and the older Feature #5 "
      "panel loader are two distinctly-named functions, not two "
      "declarations sharing one name",
      len(_re2.findall(r'^async function loadInstitutional\(', h, _re2.M)) == 1 and
      len(_re2.findall(r'^async function loadInstitutionalPage\(', h, _re2.M)) == 1)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
