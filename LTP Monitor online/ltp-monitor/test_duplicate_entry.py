#!/usr/bin/env python3
"""test_duplicate_entry.py — two orders, one tracked position.

Observed live on 2026-08-06 from two dashboard clicks:

    14:11:18  PAPER BUY 65 x NIFTY 24650 CE @ 150.9   order A
    14:11:23  PAPER BUY 65 x NIFTY 24650 CE @ 151.3   order B
    14:11:31  B registers into positions["NIFTY"]
    14:11:35  A registers — OVERWRITING B

130 qty bought, 65 tracked. The untracked half had no stop, no exit
monitoring, and was invisible to concurrent-position counts,
deployed_capital and the portfolio kill-switch. Nothing was logged:
`positions` is keyed by SYMBOL, so the second write silently replaced
the first.

The risk gate's "no open position on X" check could not prevent it —
that is a CHECK-THEN-ACT race. The position does not exist until
_place() finishes, which took 13-17 SECONDS that afternoon because the
AI probability call was blocking on an Ollama timeout (normal is ~1s).
The window widens exactly when the UI looks dead and the user is most
likely to click again.

The fix is a claim taken BEFORE any slow work, plus a refusal to
overwrite at registration. This test drives the real ExecutionAgent,
including a genuinely CONCURRENT pair of calls — a sequential test
cannot see a race.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_duplicate_entry")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config

SYM = "NIFTY"


def _chain(ltp=150.0):
    return {"symbol": SYM, "spot": 24650.0, "rows": [
        {"strike": 24650.0,
         "ce": {"ltp": ltp, "oi": 1000, "oi_chg": 0, "volume": 10, "iv": 20.0,
                "bid": ltp - 0.5, "ask": ltp + 0.5, "security_id": "7001"},
         "pe": {"ltp": 100.0, "oi": 1000, "oi_chg": 0, "volume": 10, "iv": 20.0,
                "bid": 99.5, "ask": 100.5, "security_id": "7002"}}]}


def _job():
    return {"symbol": SYM,
            "analysis": {"strikes": [], "spot": 24650.0},
            "signal": {"signal": "BUY_CE", "strike": 24650.0, "entry": 150.0,
                       "option_ltp": 150.0, "stoploss": 105.0,
                       "target1": 240.0, "target2": 300.0, "confidence": 80,
                       "security_id": "7001", "source": "test"}}


cfg = config.load()
cfg["paper_mode"] = True
cfg["lot_sizes"] = dict(cfg.get("lot_sizes") or {}, NIFTY=65)
cfg["option_risk_per_trade_rupees"] = 15000
cfg["option_reentry_cooldown_sec"] = 0
cfg["paused_symbols"] = []
config.save(cfg)


def _fresh():
    bus = agents.Bus()
    bus.set("symbols", [SYM])
    bus.set("chain:" + SYM, _chain())
    return bus, agents.ExecutionAgent(bus, {"orders_factory": lambda: None,
                                            "get_chain": lambda s: None})


print("1) a second entry while one is already OPEN is refused")
bus, ex = _fresh()
r1 = ex.place(_job())
check("the first entry opens", (bus.get("positions") or {}).get(SYM) is not None,
      str(r1)[:110])
first = dict((bus.get("positions") or {})[SYM])
r2 = ex.place(_job())
check("the second is refused",
      isinstance(r2, dict) and "already open" in str(r2.get("error", "")),
      str(r2)[:130])
check("and the FIRST position is untouched",
      (bus.get("positions") or {})[SYM]["opened"] == first["opened"],
      "a silent overwrite is what lost 65 qty on 2026-08-06")
check("exactly one position is tracked",
      len(bus.get("positions") or {}) == 1,
      str(list((bus.get("positions") or {}))))

print("\n2) CONCURRENT submissions — the actual race, not a sequential proxy")
# The live failure needed two calls IN FLIGHT at once. A sequential test
# passes against the old code too, so it would have proved nothing.
bus2, ex2 = _fresh()
results = []
barrier = threading.Barrier(2)


def _fire():
    barrier.wait()                    # maximise overlap
    results.append(ex2.place(_job()))


threads = [threading.Thread(target=_fire) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)
opened = len([r for r in results if not (isinstance(r, dict) and r.get("error"))])
refused = [r for r in results if isinstance(r, dict) and r.get("error")]
check("both calls returned", len(results) == 2, str(len(results)))
check("exactly ONE opened a position", opened == 1,
      f"{opened} opened, {len(refused)} refused: {[str(r)[:60] for r in refused]}")
check("the bus holds exactly one position",
      len(bus2.get("positions") or {}) == 1,
      str(list((bus2.get("positions") or {}))))
check("the refusal names the reason",
      refused and any(("already open" in str(r.get("error", "")))
                      or ("in progress" in str(r.get("error", "")))
                      for r in refused),
      str([str(r)[:70] for r in refused]))

print("\n3) the claim is RELEASED — a refusal must not wedge the symbol")
bus3, ex3 = _fresh()
bad = _job()
bad["signal"]["stoploss"] = 200.0        # above entry -> refused downstream
ex3.place(bad)
check("no position was opened by the bad job",
      not (bus3.get("positions") or {}).get(SYM))
ok = ex3.place(_job())
check("a subsequent GOOD entry still works",
      (bus3.get("positions") or {}).get(SYM) is not None,
      f"{str(ok)[:110]} — a claim leaked on the error path would wedge "
      f"this symbol until restart")

print("\n4) the claim happens BEFORE the slow work, not after")
HERE = os.path.dirname(os.path.abspath(__file__))
AG = open(os.path.join(HERE, "agents.py")).read()
w = AG.split("    def place(self, job, manual=False):")[1]
w = w[:w.index("    def _place(self, job, manual=False):")]
check("place() claims the symbol", "_entering.add" in w, "in the wrapper")
check("under a lock", "_entry_lock" in w,
      "manual_trade() runs on the HTTP thread, cycle() on the agent thread")
check("and releases it in a finally", "finally:" in w and "_entering.discard" in w,
      "otherwise one error path wedges the symbol permanently")
check("the claim precedes the call to _place",
      w.index("_entering.add") < w.index("self._place("),
      "claiming AFTER the slow work would leave the race exactly as it was")

print("\n5) registration refuses to overwrite — defence in depth")
body = AG.split("    def _place(self, job, manual=False):")[1]
body = body[:body.index("\n    def ")]
i_guard = body.find("Refusing to overwrite it.")
i_write = body.find("positions[sym] = pos")
check("there is an explicit no-overwrite guard", i_guard > 0)
check("and it sits BEFORE the write", 0 < i_guard < i_write,
      f"guard@{i_guard} write@{i_write}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all duplicate-entry checks passed")
