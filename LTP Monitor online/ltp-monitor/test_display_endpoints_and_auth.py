"""v58.53 — a live 502 and an overnight 401 loop.

    GET /api/ai_visual/NIFTY -> 502 Bad Gateway
    RuntimeError: Dhan rate limit hit -- slow down polling.

CAUSE 1. Display endpoints did:
    analysis = pilot.bus.get(f"analysis:{sym}") or analyze(get_chain(sym))
so on any poll where the bus was cold they made a SYNCHRONOUS broker
call. The dashboard polls these panels on a timer, so a cold bus meant a
broker call per panel per poll -- the same amplification class as the
prev_close 5-call storm (v58.34), and for the same reason: a code path
treating a shared rate-limited resource as free.

CAUSE 2. At 01:55 the Dhan token expired (401) and the transient path
retried it every 30s. A 401 does not fix itself; the only remedy is a
human pasting a new token, so retrying is pure noise plus wasted calls.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import rate_limit as rl
APP = open("app.py").read()

print("1) Display endpoints no longer fetch")
check("helper exists", "def bus_analysis_or_warming" in APP)
# The original inline `bus.get(...) or analyze(get_chain(...))` must
# still be gone — that unthrottled form is what produced the 502. The
# THROTTLED fetch inside the helper is the fix, not a relapse.
# Count only EXECUTABLE lines. The helper's docstring quotes the old
# broken line as the historical record, and an assertion that matched it
# would force deleting the explanation to pass -- the fourth time that
# pattern has appeared this session, so it is worth handling properly:
# strip docstrings and comments before searching.
import ast as _ast, io as _io, tokenize as _tok
def _code_only(path):
    out = []
    with open(path) as f:
        for tk in _tok.generate_tokens(f.readline):
            if tk.type in (_tok.COMMENT, _tok.STRING):
                continue
            out.append(tk.string)
    return " ".join(out)
_CODE = _code_only("app.py")
check("no EXECUTABLE line does the unthrottled inline bus-or-fetch",
      "or analyze ( get_chain" not in _CODE,
      "that form produced the 502")
# THREE executable sites remain, and only one is in scope here:
#   line 1152  the throttled display helper -- the fix
#   lines 201/222  /api/analysis, the PRIMARY data endpoint with its own
#                  momentum= path, not a display panel. It is polled too
#                  and probably wants the same throttle, but changing the
#                  core data path is a separate decision and rushing it
#                  alongside a regression fix is how the v58.53 mistake
#                  happened. Recorded in the ROADMAP instead.
check("the display helper is the ONLY throttled fetch site",
      "fresh = analyze(get_chain(sym))" in APP
      and APP.count("fresh = analyze(get_chain(sym))") == 1)
check("display endpoints route through the helper, not their own fetch",
      APP.count("bus_analysis_or_warming(") >= 4,
      "ai_visual, engine, strategies and the 863 variant")
# v58.54 — the v58.53 ban was too absolute and broke out-of-hours
# viewing: MarketDataAgent does not populate the bus when the market is
# closed, so banning the fetch left every index panel on "start agents"
# after a restart. The defect was UNTHROTTLED fetching, not fetching.
check("fetching is allowed again, but THROTTLED",
      "allow_fetch=True" in APP and "_DISP_MIN_INTERVAL" in APP)
check("throttle is per-symbol", "_disp_cache[sym]" in APP)
check("a cached result is served between fetches",
      "hit and (time.time() - hit[0])" in APP)
check("an expired token is NOT reported as 'warming up'",
      '"auth_expired": True' in APP,
      "it needs a human and will not resolve on its own")
check("a stale panel is preferred over an empty one on failure",
      "A stale panel beats an empty one" in APP)
check("it returns a signal instead of raising", '"warming_up"' in APP)
check("it consults the shared rate limiter before any fetch",
      'rate_limit.is_limited("quote")' in APP)
check("it reports how long the cooldown has left",
      "rate_limit.remaining('quote')" in APP)
check("a failed fetch feeds the limiter rather than being swallowed",
      "_rl.note_failure(e" in APP)
check("the helper documents having been wrong in BOTH directions",
      "wrong in both" in APP.lower(),
      "banning the fetch broke out-of-hours viewing")

print("\n2) Auth failures are not treated as transient")
rl.reset()
is429, secs = rl.note_failure("401 Client Error: Unauthorized", "quote")
check("a 401 is not misread as a rate limit", not is429)
check("it earns the long AUTH backoff", secs == rl.BACKOFF_AUTH, f"{secs}s")
check("it is identifiable as an auth failure", rl.is_auth_failure("quote"))
check("AUTH backoff is much longer than transient",
      rl.BACKOFF_AUTH > rl.BACKOFF_OTHER * 10,
      f"{rl.BACKOFF_AUTH} vs {rl.BACKOFF_OTHER}")
rl.reset()
rl.note_failure("Dhan token expired -- paste a fresh Access Token", "quote")
check("the human-readable token message is also recognised",
      rl.is_auth_failure("quote"))
rl.reset()
rl.note_failure("429 Too Many Requests", "quote")
check("a 429 is NOT flagged as auth", not rl.is_auth_failure("quote"))
check("and keeps its own backoff", abs(rl.remaining("quote") - rl.BACKOFF_429) < 3)
rl.reset()
rl.note_failure("connection reset by peer", "quote")
check("a transient error still gets the short backoff",
      abs(rl.remaining("quote") - rl.BACKOFF_OTHER) < 3)
check("and is not flagged as auth", not rl.is_auth_failure("quote"))
rl.reset()

print("\n3) The token alert fires once, not every 30s")
check("auth path is handled before the generic log line",
      APP.index("_rl.is_auth_failure") < APP.index("will retry shortly"))
check("alert is guarded by a once-flag", "_auth_alerted" in APP)
check("the message names the ONLY fix",
      "paste a fresh" in APP and "Access Token" in APP)
check("it states that nothing recovers until then",
      "nothing will recover" in APP)
check("it raises a HIGH alert, not just a log line",
      'alert("high", "app"' in APP)
check("the flag clears when the token is replaced",
      'globals()["_auth_alerted"] = False' in APP,
      "otherwise a second expiry the same day would be silent")
i_reset = APP.index('globals()["_auth_alerted"] = False')
i_fn = APP.index("def _reset_quote_rate_limit")
check("the clear lives in the named reset helper", i_reset > i_fn)

print("\n4) Endpoints still respond")
from fastapi.testclient import TestClient
import app
c = TestClient(app.app)
r = c.get("/api/ai_visual/NIFTY")
check("ai_visual returns 200, not 502", r.status_code == 200, str(r.status_code))
body = r.json()
check("it says warming_up rather than erroring",
      body.get("warming_up") is True or "ai" in body or "error" not in body,
      str(body)[:90])
check("dashboard still serves", c.get("/").status_code == 200)
check("version endpoint fine", c.get("/api/version").status_code == 200)


print("\n5) v58.54 — throttling actually throttles")
import app as _a, time as _t
_a._disp_cache.clear()
rl.reset()
_calls = {"n": 0}
_orig_get, _orig_an = _a.get_chain, _a.analyze
_a.get_chain = lambda s: _calls.__setitem__("n", _calls["n"] + 1) or {"x": 1}
_a.analyze = lambda ch, **k: {"spot": 100, "atm": 100, "strikes": []}
try:
    for _ in range(25):
        _a.bus_analysis_or_warming("TESTSYM")
    check("25 panel polls cost ONE broker fetch", _calls["n"] == 1,
          f"{_calls['n']} fetches")
    _a._disp_cache["TESTSYM"] = (_t.time() - 120, {"spot": 1})
    _a.bus_analysis_or_warming("TESTSYM")
    check("a stale cache entry triggers a refetch", _calls["n"] == 2,
          f"{_calls['n']} fetches")
    rl.reset()
    rl.note_failure("401 Unauthorized", "quote")
    _r = _a.bus_analysis_or_warming("TESTSYM2")[1]
    check("auth failure short-circuits before any fetch",
          _r and _r.get("auth_expired") is True, str(_r))
    check("and does not claim to be warming up", _r.get("warming_up") is False)
finally:
    _a.get_chain, _a.analyze = _orig_get, _orig_an
    _a._disp_cache.clear()
    rl.reset()

print("\n6) v58.55 -- /api/analysis degrades, and version strings agree")
import app as _ap
rl.reset(); _ap._disp_cache.clear()
_r = c.get("/api/analysis/NIFTY")
check("/api/analysis never returns 502 on a fetch failure",
      _r.status_code == 200, str(_r.status_code))
_b = _r.json()
check("it says WHY rather than returning an error page",
      _b.get("unavailable") is True or "spot" in _b or "atm" in _b,
      str(_b)[:80])
rl.reset(); _ap._disp_cache.clear()
rl.note_failure("401 Unauthorized", "quote")
_b2 = c.get("/api/analysis/NIFTY").json()
check("an expired token is reported as auth_expired, not a generic error",
      _b2.get("auth_expired") is True, str(_b2)[:80])
check("the reason names the fix", "Access Token" in str(_b2.get("reason")))
rl.reset(); _ap._disp_cache.clear()
check("it shares the display throttle", "_DISP_MIN_INTERVAL" in APP)
check("it feeds the shared rate limiter", '_rl.note_failure(fe, "quote")' in APP)
check("stale-beats-blank applies here too", "stale beats blank" in APP)

import subprocess as _sp
_g = _sp.run([sys.executable, "build_gate_versions.py"], capture_output=True, text=True)
check("version build gate passes", _g.returncode == 0, _g.stdout.strip().splitlines()[-1])
_gate_src = " ".join(open("build_gate_versions.py").read().split())
check("the gate explains why line-number sed was the bug",
      "does not error when the pattern is absent" in _gate_src,
      "phrase wraps across lines -- flatten before matching (5th time)")

print("\n7) v58.56 -- pasting a new token must RECOVER immediately")
import app as _ap2
rl.reset(); _ap2._disp_cache.clear()
rl.note_failure("401 Unauthorized", "quote")
_ap2._disp_cache["NIFTY"] = (0, {"stale": True})
check("an auth failure sets the long cooldown", rl.remaining("quote") > 1000)
_resp = c.post("/api/settings", json={"dhan_access_token": "fresh-token-xyz"})
check("settings POST succeeds", _resp.status_code == 200)
check("the cooldown is CLEARED by a credential update",
      not rl.is_limited("quote"),
      "a 1800s backoff is only correct if the recovery path resets it")
check("stale 'unavailable' panels are dropped so the next poll refetches",
      len(_ap2._disp_cache) == 0)
check("the auth-alert once-flag is cleared too",
      _ap2.__dict__.get("_auth_alerted") in (False, None),
      "so a later expiry is not silent")
# Anchor on the settings handler itself, not on reset_dhan() -- which
# appears more than once, so split()[1] was a different call site.
_seg = APP[APP.index('@app.post("/api/settings")'):]
_seg = _seg[:_seg.index("return config.public_view")]
check("the reset happens inside the settings handler",
      "_reset_quote_rate_limit()" in _seg and "_disp_cache.clear()" in _seg)
check("the code records WHY this is required",
      "only correct if the recovery path resets it" in APP)
rl.reset(); _ap2._disp_cache.clear()

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
