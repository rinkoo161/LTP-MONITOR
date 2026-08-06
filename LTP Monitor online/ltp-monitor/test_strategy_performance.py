#!/usr/bin/env python3
"""test_strategy_performance.py — the Strategy Performance leaderboard.

Modelled on a QuantConnect leaderboard, and deliberately NOT the way
that page ranks. It sorted by out-of-sample 3-month Sharpe; its #2
strategy had a -4.00% five-year CAGR and a -0.80 one-year Sharpe, and
its #6 carried an 89.10% drawdown. Three months is ~60 observations, so
that ordering is mostly luck and it hides the two things that end an
account: drawdown, and a long-run negative edge.

So this table reports return AND drawdown side by side and refuses to
collapse them into one score, and its Sharpe is per-trade and labelled
as such rather than annualised off a journal that spans weeks.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_strategy_performance")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import strategy_stats

HERE = os.path.dirname(os.path.abspath(__file__))


def _write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


print("1) drawdown depends on ORDER, so the rows must be sorted by close")
# Same trades, opposite file order. A max-drawdown that changes with
# file order is not a drawdown.
a = [{"strategy": "s", "leg": "CE", "pnl": +100, "closed_date": "2026-08-01", "closed": "10:00"},
     {"strategy": "s", "leg": "CE", "pnl": -300, "closed_date": "2026-08-02", "closed": "10:00"},
     {"strategy": "s", "leg": "CE", "pnl": +400, "closed_date": "2026-08-03", "closed": "10:00"}]
p1 = _write(a)
p2 = _write(list(reversed(a)))
r1 = strategy_stats.performance(p1, min_trades=1)["strategies"][0]
r2 = strategy_stats.performance(p2, min_trades=1)["strategies"][0]
check("same trades, reversed file order -> same maxdd",
      r1["maxdd"] == r2["maxdd"], f"{r1['maxdd']} vs {r2['maxdd']}")
check("and the drawdown is the real one", r1["maxdd"] == -300,
      f"{r1['maxdd']} — +100 then -300 is a 300 trough from the peak")
check("net is unaffected by order", r1["net"] == r2["net"] == 200)

print("\n2) recovery is net / |maxdd|, and below 1.0 means not yet recovered")
check("recovery reported", r1["recovery"] == round(200 / 300, 2), str(r1["recovery"]))
check("it is BELOW 1.0 here", r1["recovery"] < 1.0,
      "the strategy has not earned back its own worst run")
flat = _write([{"strategy": "f", "leg": "CE", "pnl": 10,
                "closed_date": "2026-08-01", "closed": "10:00"}] * 3)
rf = strategy_stats.performance(flat, min_trades=1)["strategies"][0]
check("no drawdown -> recovery is None, not infinity", rf["recovery"] is None,
      "on a short journal 'never drew down' means 'not enough history', "
      "and printing inf would read as a perfect strategy")

print("\n3) Sharpe is PER-TRADE and never annualised")
src = open(os.path.join(HERE, "strategy_stats.py")).read()
check("the module says so in the docstring", "NOT ANNUALISED" in src.upper())
check("and no annualisation factor is applied",
      "252" not in src and "sqrt" not in src,
      "annualising a 3-week journal produces an impressive figure that "
      "means nothing — the exact error this table exists to avoid")
check("the payload carries the caveat to the UI",
      "not annualised" in strategy_stats.performance(p1, min_trades=1)["note"].lower())

print("\n4) thin samples are SEPARATED, never dropped")
mix = _write([{"strategy": "big", "leg": "CE", "pnl": 10,
               "closed_date": "2026-08-01", "closed": f"10:0{i}"} for i in range(5)]
             + [{"strategy": "tiny", "leg": "CE", "pnl": 9999,
                 "closed_date": "2026-08-01", "closed": "11:00"}])
res = strategy_stats.performance(mix, min_trades=3)
names = [r["strategy"] for r in res["strategies"]]
thin = [r["strategy"] for r in res["thin"]]
check("a 1-trade strategy does not top the ranking", "tiny" not in names, str(names))
check("but it is still reported", "tiny" in thin, str(thin),)
check("a bucket that vanishes is how 'no losses' gets believed",
      len(res["thin"]) == 1)

print("\n5) unattributed trades are labelled by INSTRUMENT, not hidden")
un = _write([{"leg": "SPREAD", "pnl": 5, "closed_date": "2026-08-01", "closed": "10:00"},
             {"kind": "future", "side": "LONG", "pnl": -5,
              "closed_date": "2026-08-01", "closed": "10:01"},
             {"leg": "CE", "pnl": 1, "closed_date": "2026-08-01", "closed": "10:02"}])
labs = [r["strategy"] for r in strategy_stats.performance(un, min_trades=1)["strategies"]]
check("each unattributed group names its instrument type",
      all("unattributed" in l for l in labs) and len(labs) == 3, str(labs))

print("\n6) the endpoint is declared BEFORE /api/strategies/{symbol}")
import app as _app
paths = [r.path for r in _app.app.routes
         if getattr(r, "path", "").startswith("/api/strategies")]
check("performance precedes the {symbol} catch-all",
      paths.index("/api/strategies/performance") < paths.index("/api/strategies/{symbol}"),
      f"{paths[:3]} — FastAPI matches in declaration order, so it would "
      f"otherwise be captured as a symbol")

print("\n7) every CSS class the markup uses actually EXISTS")
# 2026-08-06 — the first cut referenced .num, .chip and .muted and NONE
# of them had a rule. Same failure as `rail-label` in the watchlist
# rail: a class that does not exist renders as nothing and passes every
# marker check, a node --check and a 200 response.
H = open(os.path.join(HERE, "static", "dashboard.html")).read()
styles = "".join(re.findall(r"<style[^>]*>(.*?)</style>", H, re.S))
for cls in ("num", "chip", "muted", "up", "dn"):
    defined = re.search(r"(^|[,\s}])\." + cls + r"\b[^{]*\{", styles)
    check(f".{cls} has a CSS rule", bool(defined),
          "referenced by the performance table markup")
check("numeric cells are right-aligned despite the !important left rule",
      "#perfTable td.num" in styles,
      "an existing `table td{text-align:left!important}` outranks a bare "
      ".num, so the override is scoped to this table rather than "
      "weakening a rule whose purpose is undocumented")

print("\n8) the table is wired into the view")
check("loadStrategyPerformance is defined once",
      H.count("function loadStrategyPerformance(") == 1)
check("and called when the strategies view opens",
      'if(v==="strat")loadStrategyPerformance();' in H)
check("the operational Strategy Library table is still present",
      'id="stratTable"' in H,
      "the leaderboard is additive — deploy/config controls must remain")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all strategy-performance checks passed")
