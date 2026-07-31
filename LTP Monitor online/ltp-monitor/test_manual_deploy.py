"""v57.1/v57.2 — tests for the three manual-deploy endpoints that
close the "no manual-deploy endpoint exists for MTF Confluence, S7, or
Futures Signal" gap: /api/futures/manual_deploy and
/api/strategies/manual_fire (sg_ema, and as of v57.2, mtf_confluence
too). All three reuse existing pure-eval functions and existing entry/
signal machinery — no new gates, no new evaluation logic duplicated.

Run:  python3 test_manual_deploy.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import agents
import app
import config

# v58.49 (roadmap B2) — this suite starves on margin whenever a PREVIOUS
# suite leaves open positions in ~/.ltp-monitor/open_state.json: two
# leftover spreads consume ₹170,000 and "insufficient margin" then fails
# checks that have nothing to do with margin. It passed or failed
# depending on what ran before it, which is the same class of defect as
# the wall-clock dependency found in test_futures_defense_zone. This
# project's own rule is that tests touching persisted files must
# snapshot and restore; this one never did.
import json as _json, os as _os, atexit as _atexit
import store as _store
# 2026-07-31 — was a hardcoded ~/.ltp-monitor path, so the snapshot and
# restore below operated on the OPERATOR'S open positions even when the
# runner had redirected the store. store.path() follows LTP_MONITOR_HOME.
_OPEN_STATE = _store.path("open_state.json")
_open_state_backup = None
if _os.path.exists(_OPEN_STATE):
    try:
        _open_state_backup = open(_OPEN_STATE).read()
    except Exception:
        pass


def _restore_open_state():
    try:
        if _open_state_backup is not None:
            open(_OPEN_STATE, "w").write(_open_state_backup)
        elif _os.path.exists(_OPEN_STATE):
            _os.remove(_OPEN_STATE)
    except Exception:
        pass


_atexit.register(_restore_open_state)
# v58.49 — the per-trade rupee cap added in v58.39 blocks this suite's
# synthetic NIFTY signal (95pt fallback stop x 75 lot = ₹7,140 against a
# ₹2,500 cap) before the manual-deploy path is reached. This suite tests
# DEPLOY, not sizing; the cap is verified on its own in
# test_futures_overhaul.py. Lifted here and restored on exit alongside
# the open-state snapshot above.
try:
    import config as _cfg_mod
    _cap_backup = _cfg_mod.load().get("futures_risk_per_trade_rupees",
                                      _cfg_mod.DEFAULTS["futures_risk_per_trade_rupees"])
    _cfg_mod.save({"futures_risk_per_trade_rupees": 0})
    _atexit.register(lambda: _cfg_mod.save(
        {"futures_risk_per_trade_rupees": _cap_backup}))
except Exception:
    pass

# Start from a clean slate so leftover margin cannot fail unrelated checks.
try:
    _os.makedirs(_os.path.dirname(_OPEN_STATE), exist_ok=True)
    _json.dump({"positions": {}, "spreads": {}}, open(_OPEN_STATE, "w"))
except Exception:
    pass
import pa_strategies as pa

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


client = TestClient(app.app)

print("1) build_pa_signal() extraction — source-level guard against a "
      "second, divergent copy")
src = open("agents.py").read()
check("build_pa_signal is a module-level function (not re-inlined per call site)",
      src.count("def build_pa_signal(") == 1)
check("PriceActionAgent.cycle() calls the shared builder rather than "
      "constructing sig inline again",
      "sig = build_pa_signal(name, ev, entry, leg, row, analysis, p, risk_pct, s7_gates)" in src)
app_src = open("app.py").read()
check("the new API endpoint calls the SAME shared builder, not a "
      "reimplementation",
      "agents.build_pa_signal(" in app_src)

print("\n2) /api/futures/manual_deploy — agents-not-running guard")
r = client.post("/api/futures/manual_deploy", json={"symbol": "NIFTY"})
check("returns a clean error, not a 500, when agents aren't running",
      r.status_code == 200 and "error" in r.json(), str(r.json()))

print("\n3) /api/futures/manual_deploy — full end-to-end with a real "
     "running ExecutionAgent")
ex = agents.ExecutionAgent(app.pilot.bus, {})
app.pilot.agents = [ex]
app.pilot.running = True
SYM = "NIFTY"
app.pilot.bus.set(f"regime:{SYM}", {
    "regime": "trending-up", "confidence": 75, "confluence": "strong-bull",
    "allowed_signals": ["BUY_CE"], "session_date": agents.now_ist().strftime("%Y-%m-%d"),
    "stale": False})
app.pilot.bus.set(f"future_oi_trend:{SYM}", "long")
app.pilot.bus.set(f"future_ohlc:{SYM}", {"close": 23800.0})
config.save({"futures_strategy_enabled": True, "dynamic_sizing_enabled": False})
real_market_open = agents.market_open
agents.market_open = lambda: True
try:
    r2 = client.post("/api/futures/manual_deploy", json={"symbol": SYM})
    check("returns ok:True with a real position on a genuinely eligible signal",
          r2.json().get("ok") is True and r2.json().get("position", {}).get("kind") == "future",
          str(r2.json()))
    check("position side/entry reflect the real eval, not fabricated",
          r2.json().get("position", {}).get("side") == "LONG", str(r2.json().get("position")))
    r3 = client.post("/api/futures/manual_deploy", json={"symbol": SYM})
    check("a second call with a position already open returns a clean "
          "error (one-position-per-symbol gate still applies)",
          "error" in r3.json(), str(r3.json()))
finally:
    agents.market_open = real_market_open
    app.pilot.agents = []
    app.pilot.running = False
    app.pilot.bus.set("futures_positions", {})

print("\n4) /api/strategies/manual_fire (sg_ema) — full end-to-end with "
     "a real firing setup")
ag2 = agents.PriceActionAgent(app.pilot.bus, {})
app.pilot.agents = [ag2]
app.pilot.running = True


def candles(closes, t0, step=60, wick=1.5):
    return [{"time": t0 + i * step, "open": c, "high": c + wick,
             "low": c - wick, "close": c} for i, c in enumerate(closes)]


def cross_up_series(n=200, base=23800.0):
    out = []
    for i in range(n - 12):
        out.append(base - i * 2 + math.sin(i / 5) * base * 0.004)
    for i in range(12):
        out.append(out[-1] + 14)
    return out


now = int(time.time())
full = cross_up_series(200)
c5 = candles([23800 + i * 3 for i in range(30)], now, step=300)
c15 = candles([23800 + i * 3 for i in range(30)], now, step=900)
c1 = None
for end in range(30, len(full)):
    w = candles(full[:end], now)
    if pa.evaluate("ema_mtf", w, c5, c15,
                  params={"fast": 5, "slow": 13, "mtf_confirm": 0, "max_trades_per_day": 99}):
        c1 = w
        break
assert c1, "sweep failed to find a firing window — fixture generator broken"

app.pilot.bus.set(f"pa_candles:{SYM}", {"c1": c1, "c5": c5, "c15": c15, "ts": time.time()})
app.pilot.bus.set(f"analysis:{SYM}", {"atm": 23800, "strikes": [
    {"strike": 23800, "ce": {"ltp": 120, "security_id": "1"},
    "pe": {"ltp": 115, "security_id": "2"}}]})
app.pilot.bus.set(f"bias:{SYM}", {"bias": "Bullish", "confidence": 60})
config.save({"strategy7_enabled": True, "paper_mode": True, "s7_mtf_confirm": 0,
            "s7_require_structure": False, "s7_require_ai_bias": False})

r4 = client.post("/api/strategies/manual_fire?name=sg_ema", json={"symbol": SYM})
check("returns ok:True with a real signal on a genuinely eligible setup",
      r4.json().get("ok") is True and r4.json().get("signal", {}).get("signal") == "BUY_CE",
      str(r4.json()))
check("the returned signal carries s7_gates (routes through the risk "
      "gate/shadow journal like the automatic path)",
      "s7_gates" in r4.json().get("signal", {}))
check("agent's own _taken bookkeeping was updated (prevents the "
      "automatic loop from double-counting this same fire)",
      ag2._taken.get(f"{SYM}:sg_ema") == 1, str(getattr(ag2, "_taken", None)))

r5 = client.post("/api/strategies/manual_fire?name=momentum_buy", json={"symbol": SYM})
check("an unsupported strategy name returns a clean error, not a crash",
      "error" in r5.json() and r5.status_code == 200, str(r5.json()))

app.pilot.agents = []
app.pilot.running = False

print("\n5) /api/strategies/manual_fire (mtf_confluence) — completes the "
     "trio (futures + sg_ema above, mtf_confluence here)")
import mtf_confluence_strategy as mcs


class _FakeDhan:
    def historical_daily(self, sym, days_back=400):
        return {"candles": [{"time": i, "open": 100, "high": 101, "low": 99,
                             "close": 100} for i in range(120)]}


app.pilot.bus.set("positions", {})
ag3 = agents.MTFConfluenceAgent(app.pilot.bus, {})
app.pilot.agents = [ag3]
app.pilot.running = True
real_dhan_client = app.dhan_client
real_evaluate = mcs.evaluate
app.dhan_client = lambda: _FakeDhan()
mcs.evaluate = lambda candles, future_buildup=None, global_sentiment=None: {
    "direction": "bullish", "confidence": 85, "reasons": ["mock confluence"],
    "daily_atr14": 50.0}
app.pilot.bus.set(f"chain:{SYM}", {"spot": 23800, "rows": [
    {"strike": 23800, "ce": {"ltp": 120, "security_id": "1"},
    "pe": {"ltp": 115, "security_id": "2"}}]})
config.save({"mtf_confluence_enabled": True, "broker": "dhan",
            "mtf_min_confidence": 70, "mtf_max_trades_per_day": 1})
try:
    r6 = client.post("/api/strategies/manual_fire?name=mtf_confluence", json={"symbol": SYM})
    check("returns ok:True with a real FIRED outcome on a genuinely "
          "eligible (mocked) confluence",
          r6.json().get("ok") is True and "FIRED" in r6.json().get("outcome", ""),
          str(r6.json()))
    check("agent's own _taken bookkeeping was updated",
          ag3._taken.get(SYM) == 1, str(getattr(ag3, "_taken", None)))
    r7 = client.post("/api/strategies/manual_fire?name=mtf_confluence", json={"symbol": SYM})
    check("a second call same day correctly hits the max-trades/day gate "
          "(the SAME gate the automatic loop enforces)",
          "max trades/day reached" in r7.json().get("error", ""), str(r7.json()))
finally:
    mcs.evaluate = real_evaluate
    app.dhan_client = real_dhan_client
    app.pilot.agents = []
    app.pilot.running = False
    app.pilot.bus.set("positions", {})

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
