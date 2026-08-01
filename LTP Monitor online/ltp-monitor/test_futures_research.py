#!/usr/bin/env python3
"""test_futures_research.py — v59.0 item 30.

The page is an EVIDENCE RECORD. The two things that would quietly ruin
that are (a) it acquiring the ability to change state, and (b) the
provisional labels living in a payload that nothing renders — because a
caveat nobody sees is the same as no caveat: the number hardens into a
constant regardless.

So the checks are about what the page CANNOT do, and about the caveat
reaching the screen, rather than about it looking right.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_futures_research")

import futures_research_api as fra

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "futures_research_api.py")).read()
APP = open(os.path.join(HERE, "app.py")).read()
HTML = open(os.path.join(HERE, "static", "dashboard.html")).read()

print("1) READ ONLY — the page cannot change anything")
for bad in ("config.save", "save_versions", "live_enabled =", "bus.set",
            "manual_trade", "place_order"):
    check(f"api module never calls {bad}", bad not in SRC)
# Scan CODE ONLY. A prose comment saying "there is deliberately no
# /api/futures/hedge/toggle" would otherwise satisfy — or defeat — these
# checks by matching the test's own explanation. That exact mistake was
# made earlier in this engagement with `self.symbols`; not repeating it.
APP_CODE = "\n".join(l for l in APP.splitlines()
                     if not l.strip().startswith("#"))
# Every new endpoint must be a GET.
for ep in ("/api/futures/research/state", "/api/futures/postmortem",
           "/api/futures/gate", "/api/futures/costs", "/api/futures/hedge"):
    check(f"{ep} exists", f'"{ep}"' in APP_CODE)
    check(f"{ep} is a GET", f'@app.get("{ep}")' in APP_CODE,
          "an evidence page must not accept a mutation")
    check(f"{ep} is not also a POST", f'@app.post("{ep}")' not in APP_CODE)
check("no hedge toggle endpoint was added",
      "/api/futures/hedge/toggle" not in APP_CODE,
      "the spec listed one; a toggle on an evidence page is how "
      "'not deployable yet' gets forgotten")
check("the page has no deploy control",
      "view-fstrat" in HTML and "fr_deploy" not in HTML)

print("\n2) PROVISIONAL LABELS REACH THE SCREEN")
g = fra.promotion_gate_table()
check("gate payload carries sd provenance", bool(g.get("sd_provenance")))
check("gate payload carries cost provenance", bool(g.get("cost_provenance")))
check("payload is flagged provisional", g.get("provisional") is True)
check("the page RENDERS the provenance, not just carries it",
      "sd_provenance" in HTML and "PROVISIONAL" in HTML,
      "a caveat only in the payload lets the number harden anyway")
check("the headline is the hedged one",
      "distinguishable from zero" in (g.get("headline") or "")
      and "plausibly" in (g.get("headline") or ""), g.get("headline"))

print("\n3) THE S11 WORKED EXAMPLE IS PERMANENT")
w = fra.S11_WORKED_EXAMPLE
check("same trade count both ways", w["trades"] == 325)
check("opposite verdicts recorded",
      w["flat_model_pnl"] > 0 > w["notional_model_pnl"],
      f"₹{w['flat_model_pnl']:,} vs ₹{w['notional_model_pnl']:,}")
check("it is hard-coded, not derived from a run that may vanish",
      "S11_WORKED_EXAMPLE" in SRC and "325" in SRC)
check("the page renders it", "fr_s11" in HTML)

print("\n4) PANEL 8 SURFACES A STALE LOT SIZE")
c = fra.cost_readout()
syms = {r["symbol"]: r for r in c["symbols"]}
check("every symbol reports its lot source", all(r.get("lot_source") for r in c["symbols"]))
mism = [r for r in c["symbols"] if r.get("lot_mismatch")]
check("a config/scrip-master mismatch is reported, not silently resolved",
      all("scrip master says" in r["lot_mismatch"] for r in mism),
      f"{len(mism)} mismatch(es) surfaced")
check("the page renders the mismatch row", "lot_mismatch" in HTML)
check("the skewed spread distribution is carried",
      c["spread_distribution"]["median_points"] == 0.65
      and c["spread_distribution"]["max_points"] == 15.80)

print("\n5) HEDGE PANEL CANNOT MISREPRESENT SHADOW MODE")
h = fra.hedge_monitor()
check("declares shadow only", h["shadow_only"] is True)
check("orders placed is zero and stated", h["orders_placed"] == 0)
check("the 40-session gate is visible", h["sessions_required"] == 40)
check("sparse output is explained", "sparse" in h["note"])

print("\n6) the futures switches are reported, and still off")
import config
s = fra.research_state(None)
check("state reports both switches",
      "futures_strategy_enabled" in s and "futures_live_enabled" in s)
check("futures_strategy_enabled still False",
      config.DEFAULTS.get("futures_strategy_enabled") is False)
check("futures_live_enabled still False",
      config.DEFAULTS.get("futures_live_enabled") is False)
check("state declares itself read-only", s.get("read_only") is True)

print("\n7) the view is wired into the shell")
check("rail button exists", 'id="rail-fstrat"' in HTML)
check("registered in showView's list", '"fstrat"' in HTML)
check("dispatches to a loader", 'loadFuturesResearch()' in HTML)
check("loader is defined", "function loadFuturesResearch" in HTML)
check("render errors surface instead of blanking the page",
      "failed to render" in HTML,
      "a silent catch here would present an empty evidence page as fine")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all futures-research checks passed")
