#!/usr/bin/env python3
"""test_marketsense_link.py — the MarketSense read-only bridge (v59.59).

Plain-script style: check() accumulates results, exit 1 on any failure.
No network: the agent's cycle is fed a stubbed _get_json. Scrapes
producer literals where another module's strings are asserted (house
rule: a test that invents its own input can't catch a mismatch).
"""
import sys

results = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + label)


# ---- registration ----------------------------------------------------
import config

check("config: marketsense keys registered in DEFAULTS",
      all(k in config.DEFAULTS for k in
          ("marketsense_enabled", "marketsense_url", "marketsense_poll_sec")))
check("config: link enabled by default (display-only, safe)",
      config.DEFAULTS.get("marketsense_enabled") is True)

agents_src = open("agents.py").read()
check("agents: conditional import follows the NewsMacroAgent pattern",
      "from marketsense_link import MarketSenseAgent" in agents_src
      and "MarketSenseAgent = None" in agents_src)
check("agents: appended to AGENT_CLASSES conditionally",
      "if MarketSenseAgent is not None:" in agents_src)

src = open("marketsense_link.py").read()
check("bridge: read-only — no broker/order imports",
      "broker_adapter" not in src and "place_order" not in src
      and "dhanhq" not in src and "kite" not in src.lower())

# ---- behaviour with stubbed transport --------------------------------
import marketsense_link as ml


class FakeBus:
    def __init__(self):
        self.state = {}
        self.logs = []

    def set(self, k, v):
        self.state[k] = v

    def get(self, k, d=None):
        return self.state.get(k, d)

    def log(self, agent, msg):
        self.logs.append((agent, msg))


PULSE = [{"symbol": "TATASTEEL", "category": "ma", "materiality": 8,
          "sentiment": 0.5, "summary": "x", "filing_id": 1}]
SIGNALS = [
    {"symbol": "TATASTEEL", "stance": "buy", "conviction": 82.0,
     "risk_verdict": "clear", "entry": [100, 102], "target": [110, 115],
     "invalidation": 95.0, "profile": "default", "as_of": "2026-08-08"},
    {"symbol": "BADCO", "stance": "suppressed", "conviction": 0.0,
     "risk_verdict": "hard_block", "entry": None, "target": None,
     "invalidation": None, "profile": "default", "as_of": "2026-08-08"},
]


def fake_get_json(base, path, timeout=10):
    return PULSE if "/api/pulse" in path else SIGNALS


ml._get_json = fake_get_json
bus = FakeBus()
agent = ml.MarketSenseAgent(bus, {})
agent._last_poll = 0.0
agent.cycle()

check("cycle: events land on ms_events", bus.get("ms_events") == PULSE)
check("cycle: per-symbol event flag set",
      bus.get("ms_event_flag:TATASTEEL", {}).get("materiality") == 8)
check("cycle: watchlist holds conviction>=70 buys only",
      [s["symbol"] for s in bus.get("ms_watchlist", [])] == ["TATASTEEL"])
check("cycle: risk flag set for hard_block symbol",
      bus.get("ms_risk_flag:BADCO", {}).get("verdict") == "hard_block")
check("cycle: levels published for signal with entry/stop",
      bus.get("ms_levels:TATASTEEL", {}).get("stop") == 95.0)
check("cycle: link health ok", bus.get("ms_link", {}).get("ok") is True)


def broken_get_json(base, path, timeout=10):
    raise ml.urllib.error.URLError("connection refused")


ml._get_json = broken_get_json
agent._last_poll = 0.0
agent.cycle()
check("outage: keeps stale bus data rather than clearing",
      bus.get("ms_events") == PULSE)
check("outage: link marked not-ok with stale age",
      bus.get("ms_link", {}).get("ok") is False)
check("outage: summary says unreachable",
      "unreachable" in agent.summary)

failed = [l for l, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
