#!/usr/bin/env python3
"""test_graceful_shutdown.py — a restart must never need SIGKILL.

2026-08-08. A restart hung past 20s on SIGTERM and had to be SIGKILLed.
That is the dangerous outcome, not the slow one: a forced kill lands
mid-write to open_state.json / history.db, and open_state.json is what
re-seeds live positions on the next boot.

uvicorn's `timeout_graceful_shutdown` defaults to None, which means it
waits FOREVER for in-flight connections. Proven side by side on a
minimal Starlette app with one never-returning request:

    uvicorn DEFAULT (no timeout)   -> STILL ALIVE after 23s (unbounded)
    timeout_graceful_shutdown=10   -> exited in 10s

WHAT THIS DOES NOT DO. The root cause of the original hang was NOT
identified, and the obvious suspect was RULED OUT. Measured against a
real isolated instance of this app:

    SIGTERM, no client                  -> exits in 0.6s
    SIGTERM, websocket held open        -> exits in 0.5s
    SIGTERM, websocket dropped ABRUPTLY -> exits in 0.6s

so the chart websocket — including the orphaned-push-loop failure mode
documented at app._ws_alive() — does not block shutdown. The leading
remaining explanation is an HTTP request blocked on a locked SQLite
file (`database is locked` appears in activity.log at 00:17:58 that
morning while concurrent replays held write locks), which was also not
reproduced.

So this BOUNDS the symptom rather than removing a known cause. That is
worth having on its own: the process now exits by itself within 10s
whatever is in flight, and the restart script never escalates.
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

print("1) the server is started with a BOUNDED graceful shutdown")
run_lines = [l for l in _code if "uvicorn.run(" in l]
check("app.py calls uvicorn.run exactly once", len(run_lines) == 1,
      f"{run_lines}")
check("and passes timeout_graceful_shutdown",
      any("timeout_graceful_shutdown" in l for l in run_lines),
      "uvicorn's default is None — it waits forever for in-flight "
      "connections, which is what forced the SIGKILL")

m = re.search(r"timeout_graceful_shutdown\s*=\s*(\d+)", " ".join(run_lines))
check("the timeout is a positive number", bool(m) and int(m.group(1)) > 0,
      m.group(0) if m else "not found")
if m:
    secs = int(m.group(1))
    check("and is shorter than the restart script's own SIGTERM window",
          secs < 20,
          f"{secs}s vs the 20s scratch/restart_app.sh waits before "
          f"escalating — a timeout longer than that would still force a "
          f"SIGKILL, which is the whole thing being prevented")

print("\n2) uvicorn actually supports it at the pinned version")
import inspect

import uvicorn
check("uvicorn.Config accepts timeout_graceful_shutdown",
      "timeout_graceful_shutdown" in
      inspect.signature(uvicorn.Config.__init__).parameters,
      f"uvicorn {uvicorn.__version__} — a silently-ignored kwarg would "
      f"leave the wait unbounded while looking fixed")

print("\n3) the restart script still verifies the PROCESS, not a proxy")
SH = os.path.join(HERE, "scratch", "restart_app.sh")
if os.path.exists(SH):
    s = open(SH).read()
    check("it asserts exactly one app.py process afterwards",
          "-eq 1" in s,
          "2026-08-06: /api/version answered correctly from the OLD "
          "process while two orchestrators ran, each exiting the same "
          "positions")
    check("and it does not match its own command line",
          "app\\.py$" in s or "app\\.py$" in s.replace("'", ""),
          "2026-08-08: an unanchored ps pattern matched the shell "
          "wrapper running the script, so it killed its own parent and "
          "exited 144 mid-restart")
else:
    print("SKIP  restart_app.sh not present")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all graceful-shutdown checks passed")
