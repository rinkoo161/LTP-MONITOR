"""v58.61 — continuous `socket.send() raised exception.` during a live session.

    socket.send() raised exception.   (x3, then again, then again...)

The handler already caught WebSocketDisconnect, which LOOKED sufficient.
It was not: Starlette's send_json() frequently does NOT raise when the
peer has gone -- the write fails in the asyncio transport, which prints
that line and returns normally. So the except never fired, the
`while True` push loop never exited, and every orphaned connection kept
writing once per second forever. Each symbol/timeframe switch left
another behind, which is why the rate GREW across the session rather
than appearing once.

Relying on a failed write to detect a dead peer is the mistake.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import app
from starlette.websockets import WebSocketState

class FakeWS:
    """Mimics the observed behaviour: send_json SUCCEEDS silently after
    the peer is gone, which is precisely why a try/except missed it."""
    def __init__(self, state=WebSocketState.CONNECTED):
        self.client_state = state
        self.application_state = state
        self.sent = 0
    async def send_json(self, payload):
        self.sent += 1          # never raises -- the real failure mode

print("1) Liveness is read from state, not inferred from a failed write")
live = FakeWS()
dead = FakeWS(WebSocketState.DISCONNECTED)
check("a connected peer is alive", app.ws_alive(live))
check("a disconnected peer is NOT alive", not app.ws_alive(dead))
half = FakeWS(); half.application_state = WebSocketState.DISCONNECTED
check("half-closed counts as dead", not app.ws_alive(half),
      "both client_state AND application_state must be CONNECTED")
# Inverted deliberately in v58.61: "unknown state" is NOT evidence of
# disconnection. The stricter version muted every send to any object
# that was not a full Starlette WebSocket -- which broke
# test_chart_indicators' FakeWS and would have silently muted any future
# wrapper. Only an explicit DISCONNECTED is treated as dead.
check("an object with no state is treated as ALIVE, not muted",
      app.ws_alive(object()),
      "failing closed here costs real functionality for a case a real "
      "Starlette socket cannot produce")

# 2026-08-01 — this used asyncio.get_event_loop(), which RAISES on
# Python 3.14 ("no current event loop"). The file died here, so every
# check below — the entire dead-peer section, i.e. the leak itself —
# silently stopped running while the file merely looked "already red".
# The product code was verified sound; this restores the coverage.
print("\n2) ws_send refuses to write to a dead peer")
async def _t():
    ok_live = await app.ws_send(live, {"a": 1})
    ok_dead = await app.ws_send(dead, {"a": 1})
    return ok_live, ok_dead, live.sent, dead.sent
ok_live, ok_dead, n_live, n_dead = asyncio.run(_t())
check("send to a live peer succeeds", ok_live is True and n_live == 1)
check("send to a dead peer returns False", ok_dead is False)
check("and does NOT write -- this is the leak, closed",
      n_dead == 0, f"{n_dead} writes attempted")

class Raiser(FakeWS):
    async def send_json(self, payload):
        raise RuntimeError("transport closed")
async def _t2():
    return await app.ws_send(Raiser(), {"a": 1})
check("a raising transport is also handled",
      asyncio.run(_t2()) is False)

print("\n3) The push loop reaps rather than spinning")
SRC = open("app.py").read()
i_loop = SRC.index("while True:")
check("the loop checks liveness on EVERY iteration",
      "if not ws_alive(websocket):" in SRC[i_loop:i_loop + 400])
check("and breaks out", "break" in SRC[i_loop:i_loop + 400])
check("every send in the handler goes through ws_send",
      SRC.count("await ws_send(websocket,") == 11,
      f"{SRC.count('await ws_send(websocket,')} converted")
check("only ws_send itself calls send_json directly",
      SRC.count("await websocket.send_json(") == 1)
import re as _re
_FLAT = " ".join(_re.sub(r"#", " ", SRC).split())
check("the error path no longer writes blindly on the way out",
      "one MORE failed write" in _FLAT,
      "flatten + strip comment markers before matching prose")
check("the root cause is recorded, not just the fix",
      "does\n    NOT raise when the peer has gone" in SRC
      or "NOT raise when the peer has gone" in " ".join(SRC.split()))

print("\n4) The app still works")
from fastapi.testclient import TestClient
c = TestClient(app.app)
check("dashboard serves", c.get("/").status_code == 200)
check("version endpoint fine", c.get("/api/version").status_code == 200)
check("ticker fine", c.get("/api/ticker").status_code == 200)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
