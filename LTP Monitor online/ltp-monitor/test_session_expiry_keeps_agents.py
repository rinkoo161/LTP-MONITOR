#!/usr/bin/env python3
"""test_session_expiry_keeps_agents.py — a expired login must not look
like a dead trading system.

2026-08-02, from a live report: "after 12 hours login session, all agents
stopped". They had not. The auth middleware is HTTP-only — it 401s API
calls and 302s page loads, and never signals the agent threads. What
actually happened is that this dashboard had NO 401 handling at all, so
every /api/* poll came back 401, the surrounding `.catch(){}` swallowed
it, and every panel rendered empty. Empty panels read as "agents
stopped"; the user was also never shown the login page, because a
single-page dashboard never reloads and only a full page load gets the
302.

Two independent things are asserted here, because fixing the visible
half while breaking the invisible one would be worse than the bug:

  1. the AGENTS keep running — nothing on the auth path may stop them;
  2. the BROWSER is sent to the login page instead of silently showing
     an empty dashboard.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_session_expiry_keeps_agents")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
APP = open(os.path.join(HERE, "app.py")).read()
HTML = open(os.path.join(HERE, "static", "dashboard.html")).read()
APP_CODE = "\n".join(l for l in APP.splitlines() if not l.strip().startswith("#"))

print("1) THE AGENTS KEEP RUNNING — auth may never stop them")
MW = APP.split("async def _require_login")[1].split("\n@app")[0]
for bad in ("pilot.stop", "stop_evt", "pilot.start", "auto_start"):
    check(f"the auth middleware never touches {bad}", bad not in MW,
          "agents are in-process threads; an expired cookie is not a "
          "reason to stop trading")
# pilot.stop must remain reachable ONLY from the explicit endpoint.
_stops = [l.strip() for l in APP_CODE.splitlines() if "pilot.stop()" in l]
check("pilot.stop() has exactly one call site", len(_stops) == 1, str(_stops))
check("and it is the explicit autopilot endpoint",
      "/api/autopilot/stop" in APP_CODE.split("pilot.stop()")[0][-400:],
      "stopping the book must be a deliberate act, never a side effect")
check("agents are started at app startup, not per request",
      "@app.on_event(\"startup\")" in APP
      and "_auto_start_agents" in APP)

print("\n2) THE BROWSER IS SENT TO THE LOGIN PAGE")
check("the middleware returns a 401 with a login path for API calls",
      '"login": "/login"' in APP,
      "the payload tells the client where to go")
check("and 302s a page load to /login",
      'RedirectResponse("/login"' in APP)
check("the dashboard intercepts 401 at the fetch boundary",
      "window.fetch = function" in HTML and "r.status === 401" in HTML,
      "~90 fetch call sites — a per-call check protects only the ones "
      "somebody remembered")
check("it navigates to the login page", 'window.location.href = dest' in HTML)

print("\n3) the interceptor's failure modes")
check("the body is CLONED before being read",
      "r.clone().json()" in HTML,
      "reading the body here would consume it and break the caller")
check("a latch prevents N concurrent 401s from racing",
      "redirecting = true" in HTML and "!redirecting" in HTML,
      "the dashboard polls many endpoints; all of them 401 at once")
check("the response is still returned to the caller",
      "return r;" in HTML.split("window.fetch = function")[1][:900],
      "callers must not see a changed contract")
check("the redirect target is same-origin only",
      'j.login.charAt(0) === "/"' in HTML and 'indexOf("//") !== 0' in HTML,
      "the destination comes off the wire; an absolute URL would be an "
      "open redirect")
check("it does not navigate when already there",
      "window.location.pathname !== dest" in HTML,
      "otherwise a 401 on the login page itself loops")

print("\n4) the whole thing is inert when auth is off")
check("the middleware short-circuits when auth_enabled is false",
      'if not cfg.get("auth_enabled", False)' in APP,
      "no 401 is ever produced, so the interceptor never fires")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all session-expiry checks passed")
