#!/usr/bin/env python3
"""test_cas_freeze.py — the index freezes at 15:15; indicators must not
read the step that follows.

2026-08-04. From the NSE change effective 2026-08-03, F&O stocks stop
trading continuously at 15:15 and enter the closing call auction. Every
NIFTY/BANKNIFTY/FINNIFTY constituent IS an F&O stock, so the INDEX has
nothing left to discover from that minute: it repeats its last value
until the auction publishes the official close, which lands as one step.

Measured on the real 2026-08-04 NIFTY index series:

    before   385 bars, 24 flat, ATR(14) 10.82, largest 1-bar move 151.5
    after    360 bars,  0 flat, ATR(14)  8.60, largest 1-bar move  17.8

The phantom bar was 8.5x the largest move that actually traded that day.

This was confirmed against the BROKER, not inferred from our archive —
which could not have distinguished "the market froze" from "we stopped
collecting". Dhan returns 0 flat 1m index bars for 15:15-15:30 on
2026-07-30 and 30 of 32 on 2026-08-03.

WHAT IS DELIBERATELY NOT DONE: the bars are not dropped from storage.
They are real broker data, the official close is genuinely useful, and
the chart should show what happened. Only indicator INPUT is filtered.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_cas_freeze")

import agents
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


IST = agents.IST


def bar(h, m, price, high=None):
    t = datetime.datetime(2026, 8, 4, h, m, tzinfo=IST)
    return {"ts": int(t.timestamp()), "o": price, "h": high or price,
            "l": price, "c": high or price}


print("1) the freeze window is dropped, everything before it is kept")
series = ([bar(9, 15 + i, 24400 + i) for i in range(30)]      # morning, real
          + [bar(15, 10, 24463.5, 24466.0)]                   # 15:10 real
          + [bar(15, 14, 24463.5, 24466.0)]                   # 15:14 real
          + [bar(15, 15 + i, 24463.45) for i in range(13)]     # frozen
          + [bar(15, 28, 24463.45, 24614.9)]                  # the step
          + [bar(15, 29 + i, 24614.9) for i in range(9)])      # frozen
kept = agents.strip_cas_frozen(series)
check("bars before 15:15 survive", len(kept) == 32, f"{len(kept)} of {len(series)}")
last = datetime.datetime.fromtimestamp(kept[-1]["ts"], IST)
check("the last kept bar is 15:14", last.strftime("%H:%M") == "15:14",
      last.strftime("%H:%M"))
check("no flat bar survives",
      not any(b["o"] == b["h"] == b["l"] == b["c"] for b in kept[-2:]),
      "flat bars are the fingerprint of the freeze")

print("\n2) the 150-point phantom step never reaches an indicator")
moves_before = max(abs(series[i]["c"] - series[i - 1]["c"])
                   for i in range(1, len(series)))
moves_after = max(abs(kept[i]["c"] - kept[i - 1]["c"])
                  for i in range(1, len(kept)))
check("the step is present in the raw series", moves_before > 100,
      f"largest raw 1-bar move {moves_before:.1f}")
check("and absent after filtering", moves_after < 100,
      f"largest filtered move {moves_after:.1f} — ATR, MACD and the "
      f"ZigZag all read this number")

print("\n3) the cutoff is CONFIGURABLE, not hardcoded")
# The exchange has already revised these times once; this is the second
# session-boundary constant added for that reason.
check("cas_freeze_time is a registered default",
      "cas_freeze_time" in config.DEFAULTS,
      "config.save() silently drops unregistered keys")
check("it defaults to 15:15", config.DEFAULTS["cas_freeze_time"] == "15:15")
_saved = config.load().get("cas_freeze_time")
try:
    config.save({"cas_freeze_time": "14:00"})
    early = agents.strip_cas_frozen(series)
    check("moving the cutoff earlier drops more", len(early) < len(kept),
          f"{len(early)} at 14:00 vs {len(kept)} at 15:15")
finally:
    config.save({"cas_freeze_time": _saved or "15:15"})

print("\n4) STORAGE is untouched — this is a filter, not a delete")
HERE = os.path.dirname(os.path.abspath(__file__))
HIST = open(os.path.join(HERE, "history.py")).read()
# Scope this to the WRITE function, not the whole file. history.py also
# holds the REPLAY read helper, which legitimately filters — a first
# version scanned the file and failed the moment that was added.
_w = HIST.split("def upsert_candles")[1]
_w = _w[:_w.index("\ndef ")] if "\ndef " in _w else _w
check("the candle WRITE path has no CAS filter",
      "strip_cas_frozen" not in _w and "cas_freeze" not in _w,
      "the bars are real broker data; the chart and archive keep them")
AG = open(os.path.join(HERE, "agents.py")).read()
check("_build_candle does not filter either",
      "strip_cas_frozen" not in AG.split("def _build_candle")[1][:1500],
      "storage and indicator input are different questions")

print("\n5) it is wired to ALL THREE timeframes the strategies read")
_seg = AG.split("c5_today, session_date = self._session_only")[1][:600]
for tf in ("c5_today", "c1_today", "c15_today"):
    check(f"{tf} is filtered", f"{tf} = strip_cas_frozen({tf})" in _seg,
          "pa_candles feeds PriceAction, S7, S8, S9 and MTF")

print("\n5b) REPLAY matches LIVE — the backtester's door is filtered too")
# v59.17 filtered the live indicator path only. The backtester's three
# replay call sites, the backtest UI endpoints and news_validation all
# read through history.day_index_candles() and never touch RegimeAgent,
# so a strategy that could not see the frozen bars live was still being
# replayed against them — including the ~150-point step that v59.17's own
# note warned a backtest would "discover".
import history as _hist
_src = open(os.path.join(HERE, "history.py")).read()
_body = _src.split("def day_index_candles")[1]
_body = _body[:_body.index("\ndef ")] if "\ndef " in _body else _body
check("the replay helper can apply the filter",
      "strip_cas_frozen" in _body and "for_compute" in _body,
      "backtests must not compute on bars the live path cannot see")
_BT = open(os.path.join(HERE, "backtester.py")).read()
check("and the backtester ASKS for it at every replay site",
      _BT.count("day_index_candles(symbol, day, for_compute=True)") == 3
      and "day_index_candles(symbol, day)" not in _BT,
      "an unfiltered call site would silently replay the frozen bars")
check("but it defaults OFF so the backtest CHART still shows everything",
      "for_compute=False" in _body,
      "test_backtest_chart_data caught a first version that filtered "
      "unconditionally and lost seeded bars from the endpoint")
check("the CHART helpers do NOT filter",
      "strip_cas_frozen" not in _src.split("def candles_before")[1][:800]
      and "strip_cas_frozen" not in _src.split("def candles_since")[1][:800],
      "the chart shows what actually happened; only computation is filtered")
_real = _hist.day_index_candles("NIFTY", "2026-08-04", for_compute=True)
if len(_real) > 30:
    _mx = max(abs(_real[i]["close"] - _real[i - 1]["close"])
              for i in range(1, len(_real)))
    check("the real 2026-08-04 replay series has no 150pt step",
          _mx < 100, f"largest 1-bar move {_mx:.1f} (was 151.5 unfiltered)")
else:
    print(f"  SKIP  no archived 2026-08-04 index series here ({len(_real)} bars)")

print("\n6) degenerate input is safe")
check("empty list", agents.strip_cas_frozen([]) == [])
check("None", agents.strip_cas_frozen(None) == [])
check("a bar with no ts is kept rather than dropped",
      len(agents.strip_cas_frozen([{"o": 1, "h": 1, "l": 1, "c": 1}])) == 1,
      "dropping on missing data would silently thin the series")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all CAS-freeze checks passed")
