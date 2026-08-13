#!/usr/bin/env python3
"""test_signal_dedup.py — v59.83, signal repeat suppression.

The 13 Aug journal published NIFTY BUY_PE 24350 at 09:17, 09:21,
09:28, 09:39 and 09:42 — one trade, five risk evaluations. Neither
existing brake caught it:

  * the 120s cooldown is SHORTER than every one of those gaps;
  * the 15-min backoff only arms on HARD reject reasons (daily loss
    limit / halted / cooldown), and these were soft confidence and
    confluence rejections.

`_recent_signals = deque(maxlen=6)` had been declared for this in v58
and never referenced again.

The end-to-end case below carries its OWN control: the same two cycles
are replayed with signal_dedup_enabled=False and must produce two
publishes. That isolates the suppression as the cause of the
difference, rather than asserting a count that some unrelated guard
could also produce.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_signal_dedup")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


CFG = {**config.DEFAULTS}
NOW = 1_000_000.0

# The repeat from the journal, as the setup that must be suppressed.
SIG = {"signal": "BUY_PE", "strike": 24350, "confidence": 65,
       "entry": 100.0, "stoploss": 60.0, "target1": 180.0, "target2": 220.0}
PREV = {"key": ("NIFTY", "BUY_PE", 24350), "ts": NOW - 240,
        "confidence": 65, "spot": 24350.0, "entry": 100.0,
        "stoploss": 60.0, "target1": 180.0, "regime": "rangebound"}


def changed(sig=None, spot=24350.0, regime="rangebound", prev=None, cfg=None):
    return agents.signal_materially_changed(
        prev or PREV, sig or SIG, spot, regime, cfg or CFG, now_ts=NOW)


print("1) an identical repeat inside the window is suppressed")
_c, _w = changed()
check("unchanged setup is NOT re-published", _c is False, _w)

print("\n2) every material change re-opens it")
_c, _w = changed(sig={**SIG, "confidence": 72})
check("confidence moving 65 -> 72 re-opens", _c is True, _w)
_c, _w = changed(sig={**SIG, "confidence": 67})
check("confidence moving 65 -> 67 does NOT (below the 5-point delta)",
      _c is False, _w)
_c, _w = changed(spot=24350.0 * 1.003)
check("a 0.30% spot move re-opens", _c is True, _w)
_c, _w = changed(spot=24350.0 * 1.0005)
check("a 0.05% spot move does NOT", _c is False, _w)
_c, _w = changed(regime="trending-bull")
check("a regime change re-opens", _c is True, _w)
for _f, _v in (("entry", 130.0), ("stoploss", 80.0), ("target1", 230.0)):
    _c, _w = changed(sig={**SIG, _f: _v})
    check(f"a large {_f} move re-opens", _c is True, _w)
_c, _w = changed(prev={**PREV, "ts": NOW - 1000})
check("the window elapsing re-opens (1000s > 900s)", _c is True, _w)

print("\n3) a DIFFERENT trade is never suppressed — it keys differently")
_k = agents.signal_repeat_key("NIFTY", SIG)
check("key is (symbol, direction, strike)", _k == ("NIFTY", "BUY_PE", 24350), str(_k))
check("flipping direction changes the key",
      agents.signal_repeat_key("NIFTY", {**SIG, "signal": "BUY_CE"}) != _k)
check("changing strike changes the key",
      agents.signal_repeat_key("NIFTY", {**SIG, "strike": 24400}) != _k)
check("changing symbol changes the key",
      agents.signal_repeat_key("BANKNIFTY", SIG) != _k)

print("\n4) it fails OPEN — an impossible comparison publishes")
_c, _w = changed(spot=None)
check("no spot -> publish", _c is True, _w)
_c, _w = changed(sig={**SIG, "target1": 0})
check("missing target1 -> publish", _c is True, _w)
_c, _w = changed(prev={k: v for k, v in PREV.items() if k != "entry"})
check("no previous entry -> publish", _c is True, _w)

print("\n5) the buffer is actually wired up, and big enough")
SRC = open("agents.py").read()
check("_recent_signals is referenced more than once",
      SRC.count("_recent_signals") > 1,
      f"{SRC.count('_recent_signals')} references — 1 means dead again")
_m = re.search(r"_recent_signals = deque\(maxlen=(\d+)\)", SRC)
check("maxlen located", _m is not None)
if _m:
    _need = CFG["signal_repeat_window_sec"] / 120.0
    check("maxlen exceeds the signals that fit in one repeat window",
          int(_m.group(1)) > _need,
          f"maxlen={_m.group(1)} vs ~{_need:.0f} publishable in the window")

print("\n6) the settings are operator-controlled")
for k in ("signal_dedup_enabled", "signal_repeat_window_sec",
          "signal_repeat_conf_delta", "signal_repeat_spot_move_pct",
          "signal_repeat_geometry_pct"):
    check(f"{k} is in config.DEFAULTS", k in config.DEFAULTS)
APP = open("app.py").read()
_body = re.search(r"class SettingsIn\(BaseModel\):(.*?)\n\n\n", APP, re.S)
check("SettingsIn located", _body is not None)
if _body:
    for k in ("signal_dedup_enabled", "signal_repeat_window_sec",
              "signal_repeat_conf_delta", "signal_repeat_spot_move_pct",
              "signal_repeat_geometry_pct"):
        check(f"{k} is declared in SettingsIn", f"{k}:" in _body.group(1),
              "pydantic drops undeclared fields before config.save() sees them")

print("\n7) end-to-end through StrategyAgent.cycle, with its own control")


class _Bus:
    def __init__(self):
        self._d = {}
        self.published = []

    def get(self, k, d=None):
        return self._d.get(k, d)

    def set(self, k, v):
        self._d[k] = v

    def log(self, *a, **k):
        pass

    def alert(self, *a, **k):
        pass

    def subscribe(self, *a, **k):
        pass

    def publish(self, topic, msg):
        self.published.append((topic, msg))


def run_two_cycles(dedup_on):
    """Same setup offered twice, >120s apart. Returns publish count."""
    bus = _Bus()
    bus.set("analysis:NIFTY", {"spot": 24350.0, "atm": 24350, "strikes": []})
    bus.set("regime:NIFTY", {"regime": "rangebound"})
    bus.set("active_symbol", "NIFTY")
    bus.set("positions", {})

    ag = agents.StrategyAgent.__new__(agents.StrategyAgent)
    ag.bus = bus
    ag.name = "strategy"
    ag.summary = ""
    ag._pending = agents.deque()
    ag._recent_signals = agents.deque(maxlen=32)
    ag._last_signal_ts = 0
    ag._backoff_until = 0

    _real_open, _real_sig, _real_cfg = (
        agents.market_open, agents.ai_signal, config.load)
    agents.market_open = lambda *a, **k: True
    agents.ai_signal = lambda *a, **k: dict(SIG)
    config.load = lambda: {**CFG, "signal_dedup_enabled": dedup_on,
                           "ai_active_only": True}
    try:
        for _ in range(2):
            ag._last_signal_ts = 0          # simulate a >120s gap
            ag._pending.append({"symbol": "NIFTY"})
            ag.cycle()
    finally:
        agents.market_open, agents.ai_signal, config.load = (
            _real_open, _real_sig, _real_cfg)
    return len([p for p in bus.published if p[0] == "signal"]), ag.summary


_on, _sum_on = run_two_cycles(True)
_off, _sum_off = run_two_cycles(False)
check("with dedup ON, the identical repeat publishes ONCE", _on == 1,
      f"{_on} publishes — {_sum_on}")
check("the CONTROL: with dedup OFF the same input publishes TWICE",
      _off == 2,
      f"{_off} publishes — proves the suppression caused the difference")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
