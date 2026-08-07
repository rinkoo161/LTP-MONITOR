#!/usr/bin/env python3
"""test_graceful_shutdown.py — a restart must never need SIGKILL.

2026-08-08. A restart hung past 20s on SIGTERM and had to be SIGKILLed.
That is the dangerous outcome, not the slow one: a forced kill lands
mid-write to open_state.json / history.db, and open_state.json is what
re-seeds live positions on the next boot.

CAUSE: uvicorn's graceful shutdown waits for every open connection, and
a websocket never ends by itself, so a single open chart tab held the
whole process. Measured against an isolated instance, timing the
PROCESS:

    before:  no client -> 0s        websocket open -> 10s (bound only)
    after:   no client -> 0s        websocket open ->  1s

TWO WRONG TURNS ARE ENCODED HERE AS CHECKS, because both were believed
and one was published:

 1. The first measurement timed PORT occupancy (`lsof -ti:PORT`) and
    concluded the websocket was innocent — every case "exited" in ~0.5s.
    A hung uvicorn RELEASES ITS LISTENING SOCKET WHILE STILL RUNNING, so
    the port goes free long before the process does. v59.56 shipped with
    "root cause NOT found" and "websocket ruled out" in its commit
    message and ROADMAP entry on the strength of that false negative.
    Only `kill -0` tells you whether a process is alive.

 2. The first fix set the flag from @app.on_event("shutdown"). That does
    nothing: lifespan shutdown fires AFTER uvicorn drains connections,
    i.e. after the wait it was meant to cut short. Measured 11s with it
    in place — identical to no fix. It has to hook the SIGNAL, via
    uvicorn.Server.handle_exit.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_graceful_shutdown")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
APP = open(os.path.join(HERE, "app.py")).read()
_code = [l for l in APP.split("\n") if not l.strip().startswith("#")]

print("1) the signal is caught EARLY — not from the lifespan shutdown event")
check("a handle_exit override exists",
      any("def handle_exit(" in l for l in _code),
      "@app.on_event('shutdown') fires after the drain and measured 11s, "
      "identical to no fix")
seg = APP.split("def handle_exit(")[1][:300] if "def handle_exit(" in APP else ""
check("and it sets the shutdown flag", "SHUTTING_DOWN.set()" in seg)
check("and it still delegates to uvicorn's own handler",
      "super().handle_exit(" in seg,
      "swallowing the signal would stop the server ever shutting down")
check("the flag is NOT set from on_event('shutdown')",
      not re.search(r'on_event\("shutdown"\)\s*\ndef\s+\w+\([^)]*\):\s*\n\s*SHUTTING_DOWN\.set\(\)', APP),
      "that was tried and does nothing — see the module docstring")

print("\n2) the websocket push loop actually checks it")
ws = APP.split('@app.websocket("/ws/candles/{symbol}")')[1]
check("the push loop breaks on SHUTTING_DOWN",
      "SHUTTING_DOWN.is_set()" in ws,
      "without this the connection is only closed when the timeout "
      "fires, so every restart with a dashboard open pays the full 10s")

print("\n3) the timeout remains as a BACKSTOP")
run_lines = [l for l in _code if "uvicorn.Config(" in l or "uvicorn.run(" in l]
check("timeout_graceful_shutdown is still passed",
      any("timeout_graceful_shutdown" in l for l in _code),
      "the loop fix handles the websocket; the timeout still bounds "
      "anything else that blocks — the root cause of the ORIGINAL hang "
      "is now known, but an unbounded wait is wrong regardless")
m = re.search(r"timeout_graceful_shutdown\s*=\s*(\d+)", APP)
check("it is positive", bool(m) and int(m.group(1)) > 0, m.group(0) if m else "not found")
if m:
    check("and shorter than restart_app.sh's 20s escalation window",
          int(m.group(1)) < 20,
          f"{m.group(1)}s — a longer timeout would still end in the "
          f"SIGKILL this exists to prevent")

print("\n4) uvicorn supports it at the pinned version")
import inspect

import uvicorn
check("uvicorn.Config accepts timeout_graceful_shutdown",
      "timeout_graceful_shutdown" in
      inspect.signature(uvicorn.Config.__init__).parameters,
      f"uvicorn {uvicorn.__version__} — a silently-ignored kwarg would "
      f"leave the wait unbounded while looking fixed")
check("uvicorn.Server has handle_exit to override",
      hasattr(uvicorn.Server, "handle_exit"))

print("\n5) the restart script verifies the PROCESS, not a proxy")
SH = os.path.join(HERE, "scratch", "restart_app.sh")
if os.path.exists(SH):
    s = open(SH).read()
    check("it asserts exactly one app.py process afterwards", "-eq 1" in s,
          "2026-08-06: /api/version answered correctly from the OLD "
          "process while two orchestrators ran, each exiting the same "
          "positions")
    check("and does not match its own command line", "app\\.py$" in s,
          "2026-08-08: an unanchored ps pattern matched the shell "
          "wrapper running the script, so it killed its own parent")
else:
    print("SKIP  restart_app.sh not present")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all graceful-shutdown checks passed")
