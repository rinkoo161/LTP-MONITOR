"""Tests for the 2026-07-26 change: RegimeAgent must still classify
the LAST AVAILABLE session while the market is closed, instead of
going idle and leaving the Regime panel on "waiting for enough
candles".

Critically, this also tests the safety guards, because the naive
version of this change is dangerous:

  1. During market hours with today's candles present, behaviour must
     be BIT-IDENTICAL to before (today-only slices). Silently
     substituting an older session is exactly the confluence data-
     freshness bug that was fixed earlier.
  2. A stale read must NOT reach `regime:{sym}`, since 14 call sites
     read that key and several gate trades.
  3. `pa_candles:{sym}` must be withheld when stale — PriceActionAgent
     assumes index 0 is today's 9:15 open, so last session's bars would
     produce a real-looking ORB signal from the wrong day.
  4. RiskAgent must not gate on a regime from another session.

Run:  python3 test_regime_closed_market.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents
from agents import RegimeAgent, Bus

SYM = "NIFTY"
IST = agents.IST


def make_candles(day_offset, n, step_min, base=23800.0, trend=1.0):
    """Candles for a given IST calendar day, 9:15 onward."""
    day = agents.now_ist().replace(hour=9, minute=15, second=0,
                                   microsecond=0)
    day = day - agents.timedelta(days=day_offset) \
        if hasattr(agents, "timedelta") else day
    from datetime import timedelta
    day = agents.now_ist().replace(hour=9, minute=15, second=0,
                                   microsecond=0) - timedelta(days=day_offset)
    start = int(day.timestamp())
    out = []
    px = base
    for i in range(n):
        px = base + i * trend
        o = round(px, 2)
        c = round(px + trend * 0.8, 2)
        out.append({"time": start + i * step_min * 60, "open": o,
                    "high": round(max(o, c) + 2, 2),
                    "low": round(min(o, c) - 2, 2), "close": c})
    return out


class FakeBroker:
    """Returns a fixed multi-day candle set per timeframe, exactly the
    way Dhan's intraday endpoint spans ~3 prior trading days."""

    def __init__(self, include_today):
        self.include_today = include_today
        self.calls = []

    def intraday(self, sym, tf):
        self.calls.append(tf)
        tf_min = int(tf)
        n = {1: 200, 5: 90, 15: 40}[tf_min]
        candles = []
        # two prior days always present (indicator warm-up history)
        for off in (3, 2):
            candles += make_candles(off, n, tf_min, base=23600.0, trend=0.4)
        if self.include_today:
            candles += make_candles(0, n, tf_min, base=23800.0, trend=1.2)
        return {"candles": candles}


def build_agent(include_today=True):
    bus = Bus()
    bus.set("symbols", [SYM])
    broker = FakeBroker(include_today)
    ag = RegimeAgent(bus, {"dhan_client": lambda: broker})
    # no real sleeping in tests
    ag._fetch_candles = lambda d, s, tf: d.intraday(s, tf)["candles"]
    return ag, bus, broker


def run_cycle(ag, market_open_value):
    real = agents.market_open
    agents.market_open = lambda: market_open_value
    try:
        ag.cycle()
    finally:
        agents.market_open = real


results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) MARKET CLOSED, no candles for today -> last session read")
ag, bus, _ = build_agent(include_today=False)
run_cycle(ag, market_open_value=False)
last = bus.get(f"regime_last_session:{SYM}")
live = bus.get(f"regime:{SYM}")
today_str = agents.now_ist().strftime("%Y-%m-%d")
check("a regime was computed from the older dataset", bool(last),
      f"regime={ (last or {}).get('regime') }")
check("it is tagged stale=True", bool(last and last.get("stale") is True))
check("it names the session it describes",
      bool(last and last.get("session_date")
           and last["session_date"] != today_str),
      f"session_date={(last or {}).get('session_date')}")
check("it did NOT land on regime:{sym} (trade-facing key)", live is None,
      f"regime:{SYM}={live!r}")
check("pa_candles withheld so PriceActionAgent can't trade on it",
      bus.get(f"pa_candles:{SYM}") is None)
check("agent summary says market closed", "market closed" in (ag.summary or ""),
      ag.summary)

print("\n2) MARKET OPEN, today's candles present -> unchanged live behaviour")
ag, bus, _ = build_agent(include_today=True)
run_cycle(ag, market_open_value=True)
live = bus.get(f"regime:{SYM}")
check("regime published to the trade-facing key", bool(live))
check("marked NOT stale", bool(live and live.get("stale") is False))
check("session_date is today", bool(live and live.get("session_date") == today_str),
      f"session_date={(live or {}).get('session_date')}")
pa = bus.get(f"pa_candles:{SYM}")
check("pa_candles published for the price-action strategies", bool(pa))
if pa:
    from datetime import datetime
    days = {datetime.fromtimestamp(c["time"], IST).strftime("%Y-%m-%d")
            for c in pa["c5"]}
    check("pa_candles contain ONLY today's bars (no prior-day blend)",
          days == {today_str}, f"days={sorted(days)}")

print("\n3) REGRESSION GUARD: market OPEN but today's candles missing")
print("   (must report warmup, NOT silently substitute an older session)")
ag, bus, _ = build_agent(include_today=False)
run_cycle(ag, market_open_value=True)
check("no regime published to the trade-facing key",
      bus.get(f"regime:{SYM}") is None)
check("no stale substitute published either",
      bus.get(f"regime_last_session:{SYM}") is None,
      "substituting here would reintroduce the confluence-freshness bug")
check("summary reports warmup", "warmup" in (ag.summary or ""), ag.summary)

print("\n4) RiskAgent must not gate on a regime from another session")
import config
risk = agents.RiskAgent(Bus(), {})
risk.bus.set("symbols", [SYM])
risk.bus.set("trades_today", 0)
risk.bus.set("positions", {})
# A prior-session regime that WOULD block a BUY_CE if it were honoured:
# trending-down allows only BUY_PE, and strong-bear fails CE confluence.
stale_regime = {"regime": "trending-down", "confluence": "strong-bear",
                "allowed_signals": ["BUY_PE"], "atr_pct": 0.4,
                "session_date": "2026-07-24", "stale": True}
sig = {"signal": "BUY_CE", "confidence": 90, "entry": 100.0,
       "stoploss": 85.0, "target1": 130.0, "target2": 145.0,
       "strike": 23800}
job = {"symbol": SYM, "signal": dict(sig)}

def run_gate(regime_payload):
    risk.bus.set(f"regime:{SYM}", regime_payload)
    real_mo = agents.market_open
    agents.market_open = lambda: True
    try:
        ok, checks = risk.evaluate({"symbol": SYM, "signal": dict(sig),
                                "analysis": {"atm": 23800, "spot": 23800.0}})
    finally:
        agents.market_open = real_mo
    return ok, " | ".join(checks)

ok_stale, joined_stale = run_gate(stale_regime)
check("gate notes the regime is from another session",
      "not today" in joined_stale,
      next((c for c in joined_stale.split(" | ") if "not today" in c), "-"))
check("no allowed_signals block applied from the prior session",
      "avoid directional buys" not in joined_stale)
check("no confluence block applied from the prior session",
      "timeframe confluence for CE" not in joined_stale)

# Control: the SAME payload dated today MUST still block, proving the
# gate itself still works and this isn't just disabling the check.
fresh_blocking = dict(stale_regime)
fresh_blocking["session_date"] = agents.now_ist().strftime("%Y-%m-%d")
fresh_blocking["stale"] = False
ok_fresh, joined_fresh = run_gate(fresh_blocking)
check("CONTROL: same regime dated today still blocks the BUY_CE",
      ok_fresh is False and "\u2717" in joined_fresh,
      next((c for c in joined_fresh.split(" | ") if c.startswith("\u2717")), "-"))

print("\n" + "=" * 64)
failed = [l for l, ok, _ in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("   - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
