#!/usr/bin/env python3
"""test_chart_history_window.py — v59.95.

Live report 2026-08-16: the chart showed one day and would not scroll
back. TWO independent limits caused it, on the two tiers the chart
reads from:

  DB tier    (websocket, preferred) chart_history_days_1m = 5
  REST tier  (fallback)             a hard [-350:] truncation, which at
                                    the dashboard's default 1-minute
                                    interval is exactly one session

The dangerous part of the fix is not the widening. It is that
`intraday()` is shared: agents._fetch_candles() and agents._indicators()
call it for the Technical and Regime agents, whose EMA/MACD/RSI warmup,
regime classification and S9 session-bar minimum are all computed off
that window. Widening it globally would have been a live behaviour
change wearing a display fix as a disguise.

So the whole point of these checks is the SEPARATION: the chart opts in
to a wider window, the analysis callers keep the old one, and the two
cannot collide in the response cache.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_chart_history_window")

import broker_adapter
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


BA = open("broker_adapter.py").read()
APP = open("app.py").read()

print("1) intraday() is parameterised, and its defaults are the ANALYSIS values")
check("ANALYSIS_DAYS / ANALYSIS_CAP are named constants, not literals",
      "ANALYSIS_DAYS = 4" in BA and "ANALYSIS_CAP = 350" in BA,
      "a bare 350 is what made this a one-line change nobody could see")
check("intraday() takes days/cap",
      "def intraday(self, symbol: str, interval: str = \"5\"," in BA
      and "days: int = ANALYSIS_DAYS, cap: int = ANALYSIS_CAP" in BA)
check("the truncation uses cap, not a literal", "][-cap:]" in BA)
check("no bare [-350:] survives", "[-350:]" not in BA)

print("\n2) the ANALYSIS callers are UNCHANGED — this is the whole risk")
AG = open("agents.py").read()
for fn in ("_fetch_candles", "_indicators"):
    # every intraday() call inside agents must pass no window override
    calls = re.findall(r"\.intraday\([^)]*\)", AG)
check("agents.py never passes days= or cap=",
      not re.search(r"\.intraday\([^)]*\b(days|cap)\s*=", AG),
      "the Technical/Regime agents must keep the 4-day/350 window")
check("prev_close_for also keeps the defaults",
      not re.search(r"intraday\(sym, \"15\"[^)]*\b(days|cap)\s*=", APP))

print("\n3) the CHART opts in explicitly")
check("app defines its own chart window constants",
      "CHART_HISTORY_DAYS = 30" in APP and "CHART_MAX_CANDLES" in APP)
check("the REST chart endpoint passes them",
      re.search(r"d\.intraday\(symbol, interval, days=CHART_HISTORY_DAYS", APP)
      is not None)
check("the chart WEBSOCKET fallback passes them too",
      APP.count("days=CHART_HISTORY_DAYS") == 2,
      f"{APP.count('days=CHART_HISTORY_DAYS')} site(s) — fixing only one "
      f"leaves the other serving a single session")

print("\n4) the response cache cannot mix the two windows")
# Without days/cap in the key, the chart's 30-day fetch and the agents'
# 4-day fetch collide on a 55s TTL and whichever ran first silently
# feeds the other. That would push 30 days of candles into the regime
# classifier with nothing in the diff to explain it.
m = re.search(r'key = f"candles:\{symbol\}:\{interval\}([^"]*)"', BA)
check("the cache key includes days and cap", m is not None and
      "{days}" in m.group(1) and "{cap}" in m.group(1),
      m.group(0) if m else "cache key not found")

print("\n5) the DB tier's lookback was raised, and only for 1m")
check("chart_history_days_1m is 30", config.DEFAULTS["chart_history_days_1m"] == 30,
      str(config.DEFAULTS["chart_history_days_1m"]))
check("5m is untouched at 20", config.DEFAULTS["chart_history_days_5m"] == 20)
check("15m is untouched at 60", config.DEFAULTS["chart_history_days_15m"] == 60)
check("all three survive a config save round-trip",
      all(k in config.DEFAULTS for k in
          ("chart_history_days_1m", "chart_history_days_5m",
           "chart_history_days_15m")),
      "config.save() silently drops anything not in DEFAULTS")

print("\n5b) EVERY broker client accepts the same intraday() signature")
# v59.99, found live. app.dhan_client() is named for Dhan but returns
# the ACTIVE broker, so the chart's intraday(days=, cap=) call reaches
# whichever client Settings selects. v59.95 added the kwargs to
# DhanClient only; with `broker` set to kotak the chart raised
#   TypeError: KotakNeoClient.intraday() got an unexpected keyword 'days'
# on every refresh. The suite missed it because no test drove a
# non-Dhan client through the chart path — so pin the INTERFACE, which
# is cheap and does not need a live broker.
import inspect as _insp
for _cls in ("DhanClient", "ZerodhaClient", "KotakNeoClient"):
    _c = getattr(broker_adapter, _cls, None)
    check(f"{_cls} exists", _c is not None)
    if not _c:
        continue
    _p = _insp.signature(_c.intraday).parameters
    check(f"{_cls}.intraday accepts days and cap",
          "days" in _p and "cap" in _p,
          f"params={list(_p)} — app.dhan_client() returns the ACTIVE "
          f"broker, so every client must take the chart's arguments")
    check(f"{_cls}.intraday keeps days/cap OPTIONAL",
          _p["days"].default is not _insp.Parameter.empty
          and _p["cap"].default is not _insp.Parameter.empty,
          "existing callers pass neither and must keep working")

print("\n6) the signature still works positionally, as every old caller uses it")
import inspect
sig = inspect.signature(broker_adapter.DhanClient.intraday)
p = list(sig.parameters)
check("symbol and interval remain the first two positional params",
      p[1] == "symbol" and p[2] == "interval", str(p))
check("days/cap default to the analysis values",
      sig.parameters["days"].default == 4
      and sig.parameters["cap"].default == 350,
      f"days={sig.parameters['days'].default} "
      f"cap={sig.parameters['cap'].default}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all chart-history-window checks passed")
