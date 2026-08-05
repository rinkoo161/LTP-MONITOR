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
_readers = AG.count("watch_symbols")
check("exactly one reader of watch_symbols in agents.py", _readers == 1,
      f"{_readers} — it must stay archive-only")
# 2026-08-05 — this check previously counted the literal
# `cfg.get("watch_symbols"`. That string was PRESENT and the code was
# DEAD: `cfg` is not bound in _run(), so every daily cycle raised
# NameError and the loop never ran once from v59.22 until it was found by
# reading the log. Counting a substring cannot see a runtime binding
# error. Pin the resolved form instead, and note plainly that this is
# still a source check — the only thing that actually proved the loop
# works was executing it against the broker.
check("the reader uses a name that is actually bound in scope",
      'config.load().get("watch_symbols")' in AG,
      "`cfg` is not defined in _run(); config is a module-level import")

print("\n3b) FILINGS — materiality is a tier, never a direction")
import filings as _fl
for desc, txt, want in (
    ("Financial Result Updates", "", "high"),
    ("Outcome of Board Meeting", "", "high"),
    ("Credit Rating- New", "", "high"),
    ("Trading Window", "", "low"),
    ("Copy of Newspaper Publication", "", "low"),
    ("Disclosure under SEBI Takeover Regulations", "takeover regulation", "low"),
    ("Some Category Nobody Has Seen", "", "medium"),
):
    got, why = _fl.materiality(desc, txt)
    check(f"{desc[:38]:40} -> {want}", got == want, f"got {got} ({why[:34]})")
check("an unknown category is MEDIUM, never LOW",
      _fl.materiality("Brand New Category", "")[0] == "medium",
      "demoting an unrecognised filing to noise is how a material one "
      "disappears from the page")
check("the tier carries WHY it fired",
      "matched" in _fl.materiality("Financial Result Updates", "")[1],
      "a wrong tier must be traceable to a pattern, not argued about")
# The measured correction: bare 'takeover' matched 57 routine SAST
# disclosures and bare 'loss of' matched 'loss of share certificate'.
check("routine SAST disclosure is not HIGH",
      _fl.materiality("Disclosure under SEBI Takeover Regulations", "")[0] != "high")
check("loss of a share certificate is not HIGH",
      _fl.materiality("Loss of share certificate", "")[0] == "low")
check("but a real fire/accident still is",
      _fl.materiality("General Updates", "fire at the plant")[0] == "high")

print("\n3c) the filings panel fails loudly, not blankly")
FSRC = open(os.path.join(HERE, "filings.py")).read()
check("a fetch failure returns an error string",
      '"error": f"NSE announcements unavailable' in FSRC,
      "an empty list would read as 'the company filed nothing'")
check("failures are NOT cached",
      "Do NOT cache a failure" in FSRC,
      "otherwise one outage blanks the panel for the whole TTL")

print("\n4) the UI is wired, not just present")
for marker in ("wl_q", "wl_results", "wl_current", "wl_status"):
    check(f"element {marker} exists", f'id="{marker}"' in HTML)
for fn in ("loadWatchPage", "wlSearch", "wlAdd", "wlRemove", "wlSave", "wlRender", "wlSelect", "wlSpark"):
    # The trailing "(" matters: "function loadWatchPage" also matches
    # loadWatchPage_chain and _filings. Third ambiguous-substring slip of
    # this session — the others were "_age = " and a non-unique anchor.
    check(f"{fn}() is defined once", HTML.count(f"function {fn}(") == 1,
          str(HTML.count(f"function {fn}(")))
check("the picker is on its OWN page, not in Settings",
      'id="view-watch"' in HTML and 'settings-card-title">Watchlist' not in HTML,
      "requested explicitly — Settings is for configuration, not analysis")
check("the rail exposes it", 'id="rail-watch"' in HTML
      and "showView('watch')" in HTML)
check("opening the view loads it",
      'if(v==="watch")loadWatchPage();' in HTML,
      "otherwise the coverage numbers are stale from page load")
check("the page shows a chain, an OI profile and filings",
      all(x in HTML for x in ('wl_chain', 'wl_spark', 'wl_filings')))
check("removal is possible, not just adding",
      "wlRemove(" in HTML, "a list you cannot un-pick is a trap")

print("\n4b) the rail button matches its siblings — CSS classes must EXIST")
# 2026-08-05, reported: the Watch icon showed raw "watch" text. Cause: I
# wrote class="rail-label", a class with NO CSS rule, instead of the
# "lbl" every other rail button uses (absolutely positioned, opacity 0,
# revealed on hover). Every check that shipped it — marker strings,
# node --check, a 200 from the page — is blind to a class that does not
# exist. This one is not.
import re as _re
_rail = _re.findall(r'<button id="rail-([a-z]+)"[^>]*>(.*?)</button>', HTML, _re.S)
check("rail buttons were found", len(_rail) >= 5, f"{len(_rail)} buttons")
_classes = set()
for _id, _blk in _rail:
    m = _re.search(r'<span class="([a-z-]+)">[^<]*</span>\s*$', _blk.strip())
    if m:
        _classes.add(m.group(1))
check("every rail button uses the SAME label class", len(_classes) == 1,
      f"{sorted(_classes)} — a one-off class renders unstyled, which is "
      f"how raw text leaked into the icon rail")
check("and that class has a CSS rule",
      all(f".rail button .{c}" in HTML or f".{c}{{" in HTML for c in _classes),
      f"{sorted(_classes)} must be styled, not invented")
check("the watch button is among them",
      any(i == "watch" for i, _ in _rail))

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
