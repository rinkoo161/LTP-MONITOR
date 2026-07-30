"""v58.43 — pending items 9, 11, 12.

ITEM 9. A live log read:
    skipping spread evaluation — analysis is 1785296955s old (> 90s)
1,785,296,955 seconds is 56 years. `time.time() - (bus.get(k) or 0)`
subtracts an ABSENT timestamp from zero and yields the current epoch.
It failed safe, but conflated two conditions needing different
responses — MISSING (feed hasn't delivered yet; normal at startup,
self-resolving) vs STALE (feed delivered then stopped; a real fault) —
and a 56-year number reads like a clock bug, burying real warnings.

ITEM 11. `momentum_confluence` had no strategy_docs entry, absent since
before v58.27, so its Configuration view rendered empty.

ITEM 12. `mtf_confluence` logged ZERO lines across a full session. Its
status lives in a summary string that only shows if someone looks at
the Agents page, so silence was ambiguous: working and quiet, or
silently unavailable?
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents, strategy_docs, pa_strategies as pa

class FakeBus:
    def __init__(self, d=None): self.d = d or {}
    def get(self, k, default=None): return self.d.get(k, default)

print("1) MISSING is reported as missing, not as 56 years")
age, why = agents.data_age_of(FakeBus(), "chain_ts:NIFTY", label="NIFTY chain data")
check("absent timestamp -> age is None, not a huge number", age is None, str(age))
check("reason says 'not received yet'", "not received yet" in why, why)
check("the epoch number never appears", "17852" not in why and "1785" not in why, why)

now = time.time()
age2, why2 = agents.data_age_of(FakeBus({"chain_ts:NIFTY": now - 45}),
                                "chain_ts:NIFTY", label="NIFTY chain data")
check("present timestamp -> a real age", age2 is not None and 44 < age2 < 47, f"{age2:.0f}s")
check("reason states the age", "45s old" in why2 or "44s old" in why2 or "46s old" in why2, why2)

age3, _ = agents.data_age_of(FakeBus({"chain_ts": now - 10}),
                             "chain_ts:NIFTY", "chain_ts")
check("falls back through the key list in order", age3 is not None and age3 < 12)

age4, _ = agents.data_age_of(FakeBus({"chain_ts:NIFTY": 0}), "chain_ts:NIFTY")
check("a zero timestamp counts as MISSING, not 1970", age4 is None,
      "zero is exactly what produced the 56-year figure")

src = open("agents.py").read()
check("risk gate uses it", "data_age_of(self.bus" in src)
check("spread gate uses it", "chain_why" in src)
check("MISSING skips rather than rejects", "not blocking" in src,
      "graceful degradation: absent data must never veto")
check("no raw 'or 0' staleness pattern remains",
      "time.time() - (self.bus.get(f\"chain_ts:{sym}\") or 0)" not in src)

print("\n2) momentum_confluence is documented")
check("entry exists", "momentum_confluence" in strategy_docs.DOCS)
doc = strategy_docs.DOCS.get("momentum_confluence", {})
for k in ("title", "indicators", "entry", "exit", "params"):
    check(f"has '{k}'", bool(doc.get(k)))
check("documents BOTH entry paths",
      any("weapon" in str(x).lower() for x in doc.get("entry", [])))
check("names the known unwired gap honestly",
      any("NOT YET WIRED" in str(x) for x in doc.get("exit", [])),
      "the Pine original's MACD-histogram early exit is absent")
check("every PA strategy now has a docs entry",
      all(n in strategy_docs.DOCS for n in pa.PA_NAMES),
      str([n for n in pa.PA_NAMES if n not in strategy_docs.DOCS]))

print("\n3) mtf_confluence states its status")
check("announce helper exists", "_announce_once" in src)
check("announces once per DAY, not per cycle", '_announced_day' in src)
check("announces when it CANNOT run", "INACTIVE this session" in src)
check("announces when it CAN run", "silence after this line" in src,
      "so later silence unambiguously means 'no setup'")
i_help = src.index("def _announce_once")
i_cls = src.index("class MTFConfluenceAgent")
check("the helper belongs to that agent", i_help > i_cls)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
