#!/usr/bin/env python3
"""test_watchlist_picker.py — Phase 1 Settings picker.

Lets a user pick any F&O underlying Dhan actually serves, validated
against the scrip master, so its chain and futures candles are ARCHIVED
and its liquidity can be measured. It does not trade them.

That last property is the one worth defending in a test. `watch_symbols`
is a DIFFERENT config key from the bus "symbols" list, which drives
strategy, risk and execution — a name in that list would be traded. The
whole design rests on those staying separate, and nothing about the
markup makes that obvious to a later reader.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_watchlist_picker")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(HERE, "static", "dashboard.html")).read()
APP = open(os.path.join(HERE, "app.py")).read()

import config
import instrument_registry as ir

# Stub the scrip master so this is offline and deterministic.
def _row(inst, sid, under, name, lot="675.0"):
    return {"EXCH_ID": "NSE", "INSTRUMENT": inst, "SECURITY_ID": sid,
            "UNDERLYING_SYMBOL": under, "SYMBOL_NAME": name,
            "LOT_SIZE": lot, "SM_EXPIRY_DATE": "2036-08-25"}


ir._CACHE["rows"] = [
    _row("OPTSTK", "900", "ADANIENSOL", "ADANIENSOL-OPT"),
    _row("FUTSTK", "58087", "ADANIENSOL", "ADANIENSOL-Aug2036-FUT"),
    _row("EQUITY", "10217", "ADANIENSOL", "ADANI ENERGY SOLUTION LTD", "1.0"),
    _row("EQUITY", "4321", "CASHONLY", "SOME CASH ONLY LTD", "1.0"),
]

import app as appmod
from fastapi.testclient import TestClient
client = TestClient(appmod.app)

print("1) the picker only offers what can actually be analysed")
r = client.get("/api/instruments/search?q=ADANI")
res = r.json().get("results") or []
check("search returns option-bearing names", any(
    x["symbol"] == "ADANIENSOL" for x in res), str(res))
r2 = client.get("/api/instruments/search?q=CASHONLY")
check("a cash-only underlying is NOT offered",
      not (r2.json().get("results") or []),
      "offering a name the system cannot analyse is a support question")

print("\n2) validation explains itself, because the user must act on it")
j = client.get("/api/instruments/validate?symbol=ADANIENSOL").json()
check("a valid symbol passes", j["ok"], str(j["reason"]))
check("and returns what sizing needs",
      (j.get("instrument") or {}).get("lot_size") == 675,
      "lot size is READ from the CSV, never hardcoded")
j2 = client.get("/api/instruments/validate?symbol=NOSUCHNAME").json()
check("an unknown name is rejected", not j2["ok"])
check("with a reason, not just false",
      "scrip master" in (j2.get("reason") or ""), str(j2.get("reason"))[:70])

print("\n3) DATA ONLY — the separation the whole design rests on")
w = client.get("/api/instruments/watchlist").json()
check("the endpoint reports both lists", "watch_symbols" in w
      and "traded_symbols" in w)
check("and says plainly that watch != traded",
      "never traded" in (w.get("note") or "").lower(), str(w.get("note"))[:60])
check("watch_symbols is a SEPARATE config key from the traded list",
      "watch_symbols" in config.DEFAULTS,
      "the bus 'symbols' list drives strategy, risk and execution")
# The archiver is the only consumer. If a strategy/execution path ever
# reads watch_symbols, this fails and someone has to justify it.
AG = open(os.path.join(HERE, "agents.py")).read()
_readers = AG.count('cfg.get("watch_symbols"')
check("exactly one reader of watch_symbols in agents.py", _readers == 1,
      f"{_readers} — it must stay archive-only")

print("\n4) the UI is wired, not just present")
for marker in ("wl_q", "wl_results", "wl_current", "wl_status"):
    check(f"element {marker} exists", f'id="{marker}"' in HTML)
for fn in ("wlLoad", "wlSearch", "wlAdd", "wlRemove", "wlSave", "wlRender"):
    check(f"{fn}() is defined once", HTML.count(f"function {fn}") == 1,
          str(HTML.count(f"function {fn}")))
check("the panel is populated when Settings opens",
      "wlLoad()" in HTML.split("async function loadSettings(){")[1][:400],
      "otherwise the coverage numbers are stale from page load")
check("removal is possible, not just adding",
      "wlRemove(" in HTML, "a list you cannot un-pick is a trap")

print("\n5) it shows COVERAGE, not just membership")
check("the endpoint reports archived days and bars",
      '"chain_days"' in APP and '"future_bars"' in APP,
      "a name that was accepted but is archiving NOTHING looks identical "
      "to a working one otherwise — the failure mode this project keeps "
      "hitting")

print("\n6) the page's JavaScript still parses")
import re
blocks = re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S)
big = max(blocks, key=len) if blocks else ""
check("a script block was found", len(big) > 1000, f"{len(big)} chars")
try:
    p = os.path.join("/tmp", "_wl_syntax_check.js")
    open(p, "w").write(big)
    out = subprocess.run(["node", "--check", p], capture_output=True,
                         text=True, timeout=60)
    check("node --check passes", out.returncode == 0,
          (out.stderr or "")[:160])
    os.unlink(p)
except FileNotFoundError:
    print("  SKIP  node not installed — syntax not verified here")
except Exception as e:
    print(f"  SKIP  syntax check unavailable ({type(e).__name__})")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all watchlist-picker checks passed")
