#!/usr/bin/env python3
"""test_portfolio_replay.py — the replay must share ONE slot pool and
ONE capital pool across all symbols and strategies, the way live does.

The road here, because each step's number is the reason for the next:

    v59.36  exits shared   FINNIFTY replay -3,477  vs LIVE +4,702
    v59.37  entries mirrored, but PER PAIR:
              LIVE            7.0 spreads/day across the WHOLE book
              per-pair replay ~17/day for FINNIFTY bull_put ALONE
    v59.38  portfolio replay: 2.9/day, max 2 concurrent (live: 2)

`max_concurrent_spreads` and `max_spread_capital_pct` are PORTFOLIO
caps. A per-pair replay applies them as though that pair owned the
entire book, which is how it took ten times the setups live could.

What this file defends is that the pools are genuinely SHARED — a test
that only checked "it produces trades" would pass on a replay that
still ran each pair in isolation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_portfolio_replay")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import backtester
import config
import strategies as slib

HERE = os.path.dirname(os.path.abspath(__file__))
BT = open(os.path.join(HERE, "backtester.py")).read()
body = BT.split("def replay_portfolio(")[1]
body = body[:body.index("\ndef ")]

print("1) it uses the SHARED exit decision, not a private copy")
check("calls agents.spread_exit_reason", "_ag.spread_exit_reason(" in body,
      "a private copy would drift — that is the v59.36 bug returning")
check("and passes a frame-based clock, not the live one",
      "now_ts=ts" in body and "market_is_open=" in body,
      "calling market_open() here would square off every historical "
      "spread the moment the test runs after 15:23")

print("\n2) the pools are SHARED, not per-pair")
check("one open list for the whole book",
      body.count("open_list = []") + body.count("open_list, cd") >= 1
      and "for sym in symbols:" in body and "for name in names:" in body,
      "both loops must draw from the same list")
check("the slot cap is tested against that ONE list",
      "len(open_list) >= max_open" in body)
check("capital is recomputed from the CURRENT open list every time",
      "deployed = sum(" in body and "for x in open_list" in body,
      "live had a bug where a stale pre-cycle figure let several "
      "spreads each pass the same check")

print("\n3) entry admission mirrors _auto_spreads")
for frag, why in (("ts - last_eval < eval_gap", "60s evaluation gate"),
                  ("ts - cd.get(k, 0) < cooldown", "per-(symbol,strategy) cooldown"),
                  ("consec[k] >= stop_n", "consecutive-loss halt"),
                  ("max_cap_pct", "capital concentration cap")):
    check(f"{why}", frag in body)
check("the cooldown is stamped on ENTRY, not on exit",
      body.index("cd[k] = ts") > body.index("open_list.append("),
      "stamping on exit let an earlier cut open TEN clones of one "
      "spread back-to-back")

print("\n4) it reads the SAME config keys the live agent reads")
for k in ("max_concurrent_spreads", "spread_reentry_cooldown_min",
          "spread_stop_after_consecutive_losses", "max_spread_capital_pct",
          "margin_per_lot_spread"):
    check(f"{k}", k in body,
          "a replay with its own constants drifts the moment Settings change")

print("\n5) it actually runs, and stays within the shared caps")
res = backtester.replay_portfolio(log=lambda m: None)
check("returns the expected shape",
      isinstance(res, dict) and "trades" in res and "skipped" in res,
      str(type(res)))
tr = res.get("trades") or []
if not tr:
    print("  SKIP  no archived chain in this isolated store — "
          "cap fidelity cannot be measured here")
else:
    cfg = config.load()
    cap = int(cfg.get("max_concurrent_spreads", 2))
    peak = 0
    for day in {t["day"] for t in tr}:
        ev = []
        for t in [x for x in tr if x["day"] == day]:
            ev.append((t["entry_ts"], 1))
            ev.append((t["exit_ts"], -1))
        ev.sort()
        cur = 0
        for _, d in ev:
            cur += d
            peak = max(peak, cur)
    check("never exceeds max_concurrent_spreads", peak <= cap,
          f"peak {peak} vs cap {cap}")
    check("and does open more than one at a time", peak >= 2,
          f"peak {peak} — a peak of 1 would mean the pools are still "
          f"effectively per-pair")
    check("every trade names its symbol AND strategy",
          all(t.get("symbol") and t.get("strategy") for t in tr),
          "without both, a portfolio result cannot be attributed")

print("\n6) phase 1d — eligibility and targets now match LIVE")
ev_body = BT.split("def _eval_with_params(")[1]
ev_body = ev_body[:ev_body.index("\ndef ")]
check("the evaluator takes a regime argument, not a hardcoded one",
      "regime=None" in ev_body and "regime or {" in ev_body,
      "it used to hardcode {\"regime\": \"rangebound\"} with no candles "
      "while live passed the real regime")
check("both replays pass a RECONSTRUCTED regime",
      BT.count("regime=historical_regime(") == 2,
      "per-pair and portfolio replays must agree, or they cannot be "
      "compared with each other")
hr = BT.split("def historical_regime(")[1]
hr = hr[:hr.index("\ndef ")]
check("the regime is reconstructed by the LIVE classifier, not a copy",
      "_ag.RegimeAgent" in hr and "_classify(" in hr,
      "reimplementing ADX/ATR/opening-range here would drift from the "
      "agent — the failure this codebase has had three times")
check("and it cannot see bars that had not printed yet",
      "as_of_ts" in hr and "candles_before(" in hr,
      "a replay that reads future bars is not a backtest")

print("\n7) the replay reads the SAME profit/loss keys LIVE reads")
# 2026-08-06 — profit_capture (0.60) and loss_mult (1.5) appear ONLY in
# backtester.py and strategy_docs.py; agents.py reads NEITHER. Live uses
# spread_profit_target_pct (18%) and spread_loss_limit_multiple (1.0).
# The replay demanding 3.3x more profit than live before taking it is
# why replay spreads rode to "market closing" holding slots while live's
# turned over in minutes.
# 2026-08-06, later the same day — this asserted that agents.py did NOT
# read profit_capture/loss_mult, and flagged that "if live starts
# reading them, this test's premise changes". It has: enter_spread now
# prefers the TUNED per-symbol value, with the config key as fallback,
# so a tuner sweep can finally move live behaviour. The invariant worth
# defending is no longer "live ignores them" but "live and the replay
# read the SAME value".
AG_SRC = open(os.path.join(HERE, "agents.py")).read()
check("LIVE now reads the tuned profit_capture",
      "profit_capture" in AG_SRC,
      "it never did — every sweep of it moved backtest numbers only")
check("and the tuned loss_mult",
      '_tp.get("loss_mult")' in AG_SRC or "_tp[\"loss_mult\"]" in AG_SRC
      or 'loss_mult = _tp' in AG_SRC)
check("the config keys remain the FALLBACK, not the source",
      "spread_profit_target_pct" in AG_SRC
      and "spread_loss_limit_multiple" in AG_SRC,
      "a symbol the tuner has no entry for must still open spreads")
check("the replay reads the same tuned params",
      'p["profit_capture"]' in BT and 'pp["profit_capture"]' in BT,
      "the whole point is that both sides read ONE value")

# The seeded default must equal what live ran BEFORE this change, or
# deploying it silently alters behaviour. Stored versions carried
# profit_capture 0.6 — chosen against the backtester v59.36-39 proved
# was measuring a different strategy — which would have clamped to 0.45
# and moved live from an 18% target to 45%.
import backtester as _bt
for _n in ("bull_put_spread", "bear_call_spread"):
    for _s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        _p = _bt.get_params(_n, _s)
        check(f"{_n}/{_s} target still 18%",
              abs(_p["profit_capture"] * 100 - 18.0) < 0.01,
              f"{_p['profit_capture']*100:.0f}% — a reseed that changes live "
              f"behaviour on deploy is not a reseed")
check("bounds exist so the tuner can actually sweep them",
      "profit_capture" in (slib.SPREAD_BOUNDS.get("bull_put_spread") or {}),
      "it sat in DEFAULT_PARAMS with no bounds, so no sweep could move it")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all portfolio-replay checks passed")
