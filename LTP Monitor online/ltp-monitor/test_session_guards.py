#!/usr/bin/env python3
"""test_session_guards.py — v59.78, the 2026-08-10 post-mortem fixes.

Three findings from the first gated live-paper day: the LLM signal
engine truncated every response all session (rule engine traded alone),
the day's biggest loss entered 9 minutes before the forced square-off,
and the directional buys had no regime gate. All three fixes verified
by execution.
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_session_guards")

import agents
import backtester as bt
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- minutes_to_squareoff math ------------------------------------------
def _ist_epoch(h, m):
    return datetime(2026, 8, 10, h, m, tzinfo=agents.IST).timestamp()

cfg = config.load()
check("runway math: 15:00 → 22 minutes to a 15:22 square-off",
      agents.minutes_to_squareoff(_ist_epoch(15, 0), cfg) == 22)
check("runway math: the 15:13 entry had 9 minutes",
      agents.minutes_to_squareoff(_ist_epoch(15, 13), cfg) == 9)
check("runway math: negative after the square-off",
      agents.minutes_to_squareoff(_ist_epoch(15, 30), cfg) < 0)

# --- live guard: enter_future refuses without runway --------------------
class FakeBus:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.alerts, self.logs = [], []
    def get(self, k, d=None):
        return self.state.get(k, d)
    def set(self, k, v):
        self.state[k] = v
    def log(self, name, msg):
        self.logs.append(msg)
    def alert(self, sev, src, sym, msg):
        self.alerts.append((sev, msg))

ex = object.__new__(agents.ExecutionAgent)
ex.name = "execution"
ex.ctx = {}
ex.bus = FakeBus({"closed_trades": []})
_orig_open, _orig_now, _orig_load = (agents.market_open, agents.now_ist,
                                     config.load)
try:
    agents.market_open = lambda: True
    config.load = lambda: {**cfg, "paper_mode": True, "paused_symbols": [],
                           "min_entry_runway_min": 30}
    agents.now_ist = lambda: datetime(2026, 8, 10, 15, 13, tzinfo=agents.IST)
    r = ex.enter_future("NIFTY", "LONG", lots=1)
    check("futures entry at 15:13 is refused for lack of runway",
          "runway" in (r.get("error") or ""), str(r))
    agents.now_ist = lambda: datetime(2026, 8, 10, 11, 0, tzinfo=agents.IST)
    r2 = ex.enter_future("NIFTY", "LONG", lots=1)
    check("the same entry at 11:00 passes the runway gate "
          "(fails later on missing feed)",
          "runway" not in (r2.get("error") or ""), str(r2))
finally:
    agents.market_open, agents.now_ist, config.load = (_orig_open, _orig_now,
                                                       _orig_load)

# --- replay parity: a 15:12 signal is refused in the backtest too -------
CANDLES = []
for i in range(80):
    base = 100.0 + i * 0.1
    CANDLES.append({"ts": _ist_epoch(15, 0) + i * 60, "open": round(base, 2),
                    "high": round(base + 0.4, 2), "low": round(base - 0.4, 2),
                    "close": round(base + 0.2, 2), "volume": 1000})

class _H:
    @staticmethod
    def index_days(sym, n=250):
        return ["2026-08-10"]
    @staticmethod
    def day_index_candles(sym, day, for_compute=False):
        return [dict(c) for c in CANDLES]

def _stub_eval(name, c1, c5, c15, params=None, taken_today=0, precomputed=None):
    if len(c1) == 13 and taken_today == 0:      # signal lands at 15:12 IST
        s = c1[-1]["close"]
        return {"dir": 1, "entry_spot": s, "stop_spot": round(s - 3, 2),
                "t1_spot": s + 8, "t2_spot": s + 16}
    return None

_oh, _oe = bt.history, bt.pa.evaluate
try:
    bt.history = _H
    bt.pa.evaluate = _stub_eval
    trades = bt.replay_pa("NIFTY", "momentum_confluence",
                          params={"max_trades_per_day": 1})
finally:
    bt.history, bt.pa.evaluate = _oh, _oe
check("the replay refuses the same 9-minute-runway signal live refuses",
      len(trades) == 0, f"{len(trades)} trades")

# --- LLM truncation retry -----------------------------------------------
import llm

_budgets = []


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    body = json.loads(req.data)
    _budgets.append(body["options"]["num_predict"])
    if len(_budgets) == 1:
        return _FakeResp({"response": '{"signal": "BUY_CE", "why": "cut mid',
                          "done_reason": "length"})
    return _FakeResp({"response": '{"signal": "WAIT"}',
                      "done_reason": "stop"})


_orig_urlopen = llm.urllib.request.urlopen
_orig_avail = llm._ollama_available
try:
    llm.urllib.request.urlopen = _fake_urlopen
    llm._ollama_available = lambda m: True
    text, engine, err = llm.generate_json("prompt", max_tokens=400)
finally:
    llm.urllib.request.urlopen = _orig_urlopen
    llm._ollama_available = _orig_avail
check("a length-truncated response is retried once at double budget",
      _budgets == [400, 800], str(_budgets))
check("the retry's complete JSON is what comes back",
      text == '{"signal": "WAIT"}' and err is None, f"{text!r} err={err}")

# --- config + regime-fit wiring ----------------------------------------
for k in ("min_entry_runway_min", "option_buy_require_regime_fit"):
    check(f"'{k}' registered in DEFAULTS", k in config.DEFAULTS)
check("regime fit ships OFF — enabling is a deliberate choice",
      config.DEFAULTS["option_buy_require_regime_fit"] is False)
ag_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "agents.py")).read()
check("the regime-fit gate exists on the option-buy risk path",
      "option_buy_require_regime_fit" in ag_src
      and '"BUY_CE": ("trending-up", "mixed")' in ag_src)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all session-guard checks passed")
