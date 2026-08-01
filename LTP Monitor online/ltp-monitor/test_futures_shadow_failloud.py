"""v59.0 Phase 0 §3.3 — the shadow journal must not lose rows silently.

log_futures_shadow() ended in `except Exception: pass`. The journal is
the evidence base for this entire project — 142 of 183 recorded
evaluations are REJECTED signals that exist nowhere else — and a
silently dropped row does not merely shrink the sample, it BIASES it:
the entries most likely to fail a write (a serialisation error on an
unusual gate value, a full disk) are not a random subset.

A journal that loses rows without saying so is worse than no journal,
because it still looks complete.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes the shadow journal")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents

print("1) the silent swallow is gone from the source")
src = open("agents.py").read()
# Slice the FUNCTION, not a fixed character window — the previous
# version used src[i:i+3200] and silently stopped short of the lines it
# was asserting on, failing against correct code.
i = src.index("def log_futures_shadow")
_after = src[i + 10:]
_next = _after.index("\ndef ") if "\ndef " in _after else len(_after)
body = src[i:i + 10 + _next]
code = "\n".join(l.split("#", 1)[0] for l in body.splitlines())
check("no bare `except Exception: pass` in the function",
      "except Exception:\n        pass" not in code)
check("a write failure reaches the bus", "WRITE FAILED" in body)
check("lost rows are counted", "_shadow_write_failures" in body)

print("\n2) a normal entry is written and readable")
bus = agents.Bus()
e = agents.log_futures_shadow(bus, "NIFTY", "LONG", {"regime": "trending-up"},
                              True, ltp=24800, lots=1, stop=24700, target=25000)
check("returns the entry", isinstance(e, dict) and e.get("symbol") == "NIFTY")
rows = [json.loads(l) for l in open(_store.path("shadow_signals.jsonl")) if l.strip()]
check("it landed on disk", any(r.get("entry") == 24800 for r in rows), str(len(rows)))
check("tagged kind=futures so one reader serves both journals",
      rows[-1].get("kind") == "futures", str(rows[-1].get("kind")))

print("\n3) an unserialisable gate value RAISES rather than vanishing")
raised = None
try:
    agents.log_futures_shadow(bus, "NIFTY", "LONG", {"bad": object()}, True, ltp=1)
except TypeError as ex:
    raised = str(ex)
check("TypeError propagates", raised is not None)
check("...and the message names the caller as the thing to fix",
      raised and "Fix the caller" in raised, (raised or "")[:60])
before = len(open(_store.path("shadow_signals.jsonl")).readlines())
try:
    agents.log_futures_shadow(bus, "NIFTY", "LONG", {"bad": object()}, True, ltp=1)
except TypeError:
    pass
after = len(open(_store.path("shadow_signals.jsonl")).readlines())
check("no partial/corrupt line is appended when it raises", before == after,
      f"{before} -> {after}")

print("\n4) an OS write failure is reported, not swallowed")
real = agents.SHADOW_PATH
agents.SHADOW_PATH = "/proc/definitely/not/writable/shadow.jsonl"
try:
    agents.log_futures_shadow(bus, "NIFTY", "LONG", {"regime": "x"}, True, ltp=1)
    survived = True
except Exception as ex:
    survived = f"raised {type(ex).__name__}"
finally:
    agents.SHADOW_PATH = real
check("an unwritable path does not crash the trading loop", survived is True,
      str(survived))
check("but it IS reported on the bus",
      any("WRITE FAILED" in l for l in bus.feed),
      next((l[-52:] for l in bus.feed if "WRITE FAILED" in l), "NOT REPORTED"))
check("and the loss is counted", agents._shadow_write_failures[0] >= 1,
      str(agents._shadow_write_failures[0]))

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
