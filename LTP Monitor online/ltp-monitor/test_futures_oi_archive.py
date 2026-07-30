"""v58.69 — the futures-OI archive must be observable, and mode=full
must work when it has run.

On 2026-07-31 `future_oi_snapshots` did not exist in the live DB, six
days after v58.66 said futures OI "is now archived". Distinguishing
"never called" from "called and throwing" needed a probe against a
scratch database, because `except Exception: pass` makes those two
states look identical from outside. They are not: one is a missing
wire, the other a broken one.

So this file asserts two separate things:

  1. the archive call site REPORTS — throttled on failure, announced
     once on success — instead of swallowing;
  2. the replay really does reach mode="full" when futures rows exist,
     which is the whole point of archiving them.

(2) runs against a temp DB. Seeding synthetic futures OI into the real
store to prove a backtest works would corrupt the very archive the
backtest reads.
"""
import os, sys, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents


class FakeAgent:
    """Minimal stand-in — should_log_throttled only needs attribute storage."""
    pass


print("1) the shared throttle, directly")
a = FakeAgent()
check("first occurrence logs",
      agents.should_log_throttled(a, "_t", "NIFTY", "OperationalError: locked"))
check("the same reason again is suppressed",
      not agents.should_log_throttled(a, "_t", "NIFTY", "OperationalError: locked"))
check("a CHANGED reason logs immediately — new information is not rate-limited",
      agents.should_log_throttled(a, "_t", "NIFTY", "DiskFull: no space"))
check("a different key is independent",
      agents.should_log_throttled(a, "_t", "BANKNIFTY", "DiskFull: no space"))
a._t["NIFTY"] = ("DiskFull: no space", time.time() - 601)
check("and the same reason logs again after the 10-minute window",
      agents.should_log_throttled(a, "_t", "NIFTY", "DiskFull: no space"))

print("\n2) ExecutionAgent's thottle still behaves (it now delegates)")
src = open("agents.py").read()
check("_should_log_entry_fail delegates rather than duplicating the rule",
      "return should_log_throttled(self, \"_entry_fail_last\"" in src)
check("exactly one implementation of the rule exists",
      src.count("prev_reason, prev_ts = last.get(") == 1,
      str(src.count("prev_reason, prev_ts = last.get(")))

print("\n3) the archive call site no longer swallows")
_code = [ln.split("#", 1)[0] for ln in src.splitlines()]
i = next(n for n, ln in enumerate(_code) if "log_future_oi(" in ln)
window = "\n".join(_code[i:i + 22])
check("no bare `except Exception: pass` guarding the archive",
      "pass" not in window.split("except Exception")[-1][:80],
      window.split("except Exception")[-1][:60].strip())
check("a failure reaches the bus log", "futures OI archive FAILED" in window)
check("it is throttled, not per-cycle spam", "should_log_throttled" in window)
check("first success is announced once", "futures OI archive active" in window)

print("\n4) driving the real call site — failure must surface")
class Bus(agents.Bus):
    pass

def drive(monkey_raise):
    """Run _classify_future_tick with history.log_future_oi patched."""
    import history as _h
    orig = _h.log_future_oi
    bus = Bus()
    md = agents.MarketDataAgent(bus, {"get_chain": lambda s: None,
                                      "orders_factory": lambda: None})
    # baseline so the classifier has something to compare against
    md._fut_baseline = {"NIFTY": {"ltp": 24000.0, "oi": 1_000_000,
                                  "day": agents.now_ist().strftime("%Y-%m-%d")}}
    if monkey_raise:
        _h.log_future_oi = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("database is locked"))
    else:
        _h.log_future_oi = lambda *a, **k: None
    try:
        md._classify_future_tick("NIFTY", {"ltp": 24100.0, "oi": 1_100_000})
        md._classify_future_tick("NIFTY", {"ltp": 24110.0, "oi": 1_110_000})
    finally:
        _h.log_future_oi = orig
    return bus

_b = drive(monkey_raise=True)
_fails = [ln for ln in _b.feed if "futures OI archive FAILED" in ln]
check("a raising archive produces a visible log line", len(_fails) >= 1,
      _fails[0][-70:] if _fails else "NOTHING LOGGED — still silent")
check("and repeats within the window are throttled to one", len(_fails) == 1,
      f"{len(_fails)} lines")

_b2 = drive(monkey_raise=False)
_ok = [ln for ln in _b2.feed if "futures OI archive active" in ln]
check("a working archive announces itself exactly once", len(_ok) == 1,
      f"{len(_ok)} lines")
check("and logs no failure", not any("archive FAILED" in ln for ln in _b2.feed))

print("\n5) mode=full actually works once futures rows exist (temp DB)")
import history as _h
_real_db, _real_ready = _h.DB, _h._SCHEMA_READY
_h.DB = os.path.join(tempfile.mkdtemp(), "replay.db")
_h._SCHEMA_READY = False
try:
    now = int(time.time())
    # future_oi_series() buckets by IST calendar day, so derive the day
    # the same way rather than assuming it matches the local date.
    day = agents.now_ist().strftime("%Y-%m-%d")
    for k in range(5):
        _h.log_future_oi("ZZTEST", now - k * 60, 1_000_000 + k * 1000, 1000,
                         24000 + k, 5, "long_buildup")
    rows = _h.future_oi_series("ZZTEST", day)
    check("archived rows read back for the IST day", len(rows) == 5,
          f"{len(rows)} rows for {day}")
    check("the quadrant survives the round trip",
          bool(rows) and all(r["quadrant"] == "long_buildup" for r in rows))
    check("a day WITH futures rows is not chain_only",
          bool(rows), "an empty series is exactly what forces chain_only")
    check("a day WITHOUT rows yields the empty series that means chain_only",
          _h.future_oi_series("ZZTEST", "2020-01-01") == [])
finally:
    _h.DB, _h._SCHEMA_READY = _real_db, _real_ready

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
