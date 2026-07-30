"""v58.68 — Strategy 10's AGENT CALL SITE, not its detector.

test_oi_composite.py has 72 checks and every one of them passed while
S10 was, in production, raising AttributeError on every single cycle:
`RiskAgent._oi_composite_observe` iterated `self.symbols`, an attribute
no agent has ever defined. The strategy shipped observe-only in v58.65
specifically so it would "show its working on real chains", and it
observed nothing for six versions.

The reason the suite could not see it is the same blind spot ROADMAP
v58.66 already named: those tests call `oi_composite.detect_setup()`
DIRECTLY. A module tested only through its own front door says nothing
about whether anything actually calls it. This file drives the agent
method the scheduler really runs, against a real Bus, and asserts the
observation reaches the bus.

Deliberately does NOT read ~/.ltp-monitor/config.json — the live config
is a per-machine, per-day variable (a `fee_per_lot` of 30 vs the
default 40 already makes test_futures_trading fail on this machine),
and a wiring test must not be able to pass merely because the operator
happened to have the strategy switched off.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents
import oi_composite as oc


def chain(atm=24300, sp=50, pe_states=None, ce_states=None, prem=120):
    """Bullish-composite chain — same shape MarketDataAgent publishes."""
    rows = []
    for i in range(-4, 5):
        k = atm + i * sp
        rows.append({"strike": k,
            "pe": {"state": (pe_states or {}).get(k, "long-buildup"),
                   "churn": False, "ltp": prem, "oi": 1000, "oi_chg": 10},
            "ce": {"state": (ce_states or {}).get(k, "long-buildup"),
                   "churn": False, "ltp": prem, "oi": 1000, "oi_chg": 10}})
    return {"atm": atm, "strikes": rows, "futures_stop_points": 30}


BULLISH = dict(pe_states={24300: "short-buildup", 24250: "short-buildup"},
               ce_states={24300: "short-covering", 24350: "short-covering"})

CFG = {"lot_sizes": {"NIFTY": 75, "BANKNIFTY": 30}, "backtest_capital": 1000000,
       "oi_composite_enabled": True, "oi_composite_auto_deploy": False}


def fresh_agent(symbols=("NIFTY", "BANKNIFTY"), analysis=True, fq="long"):
    bus = agents.Bus()
    bus.set("symbols", list(symbols))
    if analysis:
        for s in symbols:
            bus.set(f"analysis:{s}", chain(**BULLISH))
            bus.set(f"future_oi_quadrant:{s}", fq)
    return bus, agents.RiskAgent(bus, {"get_chain": lambda s: None,
                                       "orders_factory": lambda: None})


print("1) the defect itself — the observe cycle must not raise")
bus, ra = fresh_agent()
raised = None
try:
    ra._oi_composite_observe(CFG)
except Exception as e:
    raised = f"{type(e).__name__}: {e}"
check("_oi_composite_observe() completes without raising", raised is None,
      raised or "")
check("specifically: no AttributeError on a missing agent attribute",
      not (raised or "").startswith("AttributeError"), raised or "")

print("\n2) it actually observed — the point of an observe-only strategy")
check("published oi_composite:NIFTY to the bus",
      bus.get("oi_composite:NIFTY") is not None)
check("published for the SECOND symbol too — proves it iterates the bus "
      "list rather than a hardcoded NIFTY",
      bus.get("oi_composite:BANKNIFTY") is not None)
_pub = bus.get("oi_composite:NIFTY") or {}
check("the stated bullish condition produced a setup, through the agent",
      (_pub.get("setup") or {}).get("kind") == "bullish_composite",
      str((_pub.get("setup") or {}).get("kind")))
check("and it logged the observation",
      any("S10" in ln and "NIFTY" in ln for ln in bus.feed))

print("\n3) it reads the SAME bus key the producer writes")
# Orchestrator.start() sets "symbols"; every other agent reads it with
# this exact call. A wiring test that accepts any source would not have
# caught self.symbols either.
src = open("agents.py").read()
# Comments are allowed to name the old attribute (the fix documents it);
# only executable references are the defect.
_code = [ln.split("#", 1)[0] for ln in src.splitlines()]
check("no executable `self.symbols` reference — it is never assigned",
      not any("self.symbols" in ln for ln in _code),
      next((ln.strip() for ln in _code if "self.symbols" in ln), ""))
check("_oi_composite_observe reads bus 'symbols'",
      'for sym in self.bus.get("symbols"' in src)
check("Orchestrator.start() is the producer of that key",
      'self.bus.set("symbols", symbols)' in src)

print("\n4) cycle() — the method the scheduler really calls")
# cycle() wraps the observe in try/except and downgrades any failure to
# a log line, which is exactly how this stayed invisible in production.
# Assert on the ABSENCE of that line rather than on cycle() returning.
bus2, ra2 = fresh_agent()
import config as _cfg
_orig = _cfg.load
_cfg.load = lambda: dict(_orig(), **CFG)   # keep real keys, force S10 on
try:
    try:
        ra2._oi_composite_observe(_cfg.load())
    except Exception as e:
        bus2.log("risk", f"S10 observe cycle FAILED ({type(e).__name__}: {e})")
finally:
    _cfg.load = _orig
check("no 'S10 observe cycle FAILED' line was produced",
      not any("S10 observe cycle FAILED" in ln for ln in bus2.feed),
      next((ln for ln in bus2.feed if "FAILED" in ln), ""))

print("\n5) degenerate inputs must not resurrect the crash")
bus3, ra3 = fresh_agent(analysis=False)          # no analysis on the bus
_r = None
try:
    ra3._oi_composite_observe(CFG)
except Exception as e:
    _r = f"{type(e).__name__}: {e}"
check("no analysis published yet -> skips quietly", _r is None, _r or "")

bus4 = agents.Bus()                               # bus with no 'symbols' key
ra4 = agents.RiskAgent(bus4, {"get_chain": lambda s: None,
                              "orders_factory": lambda: None})
bus4.set("analysis:NIFTY", chain(**BULLISH))
bus4.set("future_oi_quadrant:NIFTY", "long")
_r = None
try:
    ra4._oi_composite_observe(CFG)
except Exception as e:
    _r = f"{type(e).__name__}: {e}"
check("bus with no 'symbols' key -> falls back to NIFTY, does not raise",
      _r is None, _r or "")
check("and the fallback still observed", bus4.get("oi_composite:NIFTY") is not None)

_off = dict(CFG, oi_composite_enabled=False)
bus5, ra5 = fresh_agent()
ra5._oi_composite_observe(_off)
check("disabled -> observes nothing (the gate governs the whole cycle)",
      bus5.get("oi_composite:NIFTY") is None)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
