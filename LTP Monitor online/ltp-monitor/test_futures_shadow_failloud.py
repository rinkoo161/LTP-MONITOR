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

print("\n5) THE OPTIONS WRITER, hardened 2026-08-03")
# _log_shadow_signal sat twenty lines below the comment explaining why
# `except Exception: pass` was removed from log_futures_shadow — and
# still had it. It is the LARGER evidence base of the two: 389 attributed
# option/price-action signals against 184 futures ones. The same three
# properties are asserted, because "the reasoning obviously transfers"
# is exactly what left it unfixed for a release.
j = src.index("def _log_shadow_signal")
_a2 = src[j + 10:]
_n2 = _a2.index("\ndef ") if "\ndef " in _a2 else len(_a2)
obody = src[j:j + 10 + _n2]
ocode = "\n".join(l.split("#", 1)[0] for l in obody.splitlines())
check("no bare `except Exception: pass` in the options writer",
      "except Exception:\n        pass" not in ocode,
      "the futures writer's fix never reached this one")
check("an options write failure reaches the bus", "WRITE FAILED" in obody)
check("its lost rows are counted too", "_shadow_write_failures" in obody)


class _J(dict):
    pass


_bus2 = agents.Bus()
_job = {"symbol": "NIFTY", "signal": {
    "signal": "BUY_CE", "strike": 24200, "entry": 150.0, "stoploss": 105.0,
    "target1": 240.0, "target2": 300.0, "confidence": 70, "source": "orb"}}
_n_before = len(open(_store.path("shadow_signals.jsonl")).readlines())
agents._log_shadow_signal(_bus2, _job, "REJECTED", ["✗ test gate"])
_rows2 = [json.loads(l) for l in open(_store.path("shadow_signals.jsonl")) if l.strip()]
check("a normal options entry lands on disk",
      len(_rows2) > _n_before and _rows2[-1].get("strike") == 24200)
check("and carries its strategy attribution",
      _rows2[-1].get("source") == "orb",
      "this field is what makes 'has S8 ever fired' answerable at all")

_raised2 = None
_job_bad = {"symbol": "NIFTY", "signal": {
    "signal": "BUY_CE", "strike": object(), "entry": 1.0, "stoploss": 0.5,
    "target1": 2.0, "target2": 3.0, "confidence": 1, "source": "orb"}}
_b2 = len(open(_store.path("shadow_signals.jsonl")).readlines())
try:
    agents._log_shadow_signal(_bus2, _job_bad, "REJECTED", [])
except TypeError as ex:
    _raised2 = str(ex)
check("an unserialisable field RAISES rather than vanishing",
      _raised2 is not None, "silently dropping it would bias the record")
check("...naming the caller as the thing to fix",
      bool(_raised2) and "Fix the caller" in _raised2, (_raised2 or "")[:60])
check("and appends no partial line",
      len(open(_store.path("shadow_signals.jsonl")).readlines()) == _b2)

_realp = agents.SHADOW_PATH
agents.SHADOW_PATH = "/proc/definitely/not/writable/shadow.jsonl"
_before_fails = agents._shadow_write_failures[0]
try:
    agents._log_shadow_signal(_bus2, _job, "REJECTED", [])
finally:
    agents.SHADOW_PATH = _realp
# (no `check(..., True)` here: if that call had raised, the checks below
# would never run and the suite would error — they ARE the assertion that
# an OS failure leaves the caller alive.)
check("an OS failure is reported on the bus, not swallowed",
      any("WRITE FAILED" in l for l in _bus2.feed),
      next((l[-52:] for l in _bus2.feed if "WRITE FAILED" in l), "NOT REPORTED"))
check("and counted", agents._shadow_write_failures[0] > _before_fails,
      f"{_before_fails} -> {agents._shadow_write_failures[0]}")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
