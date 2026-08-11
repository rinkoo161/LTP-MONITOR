#!/usr/bin/env python3
"""test_dhan_postback.py — v59.79, the public order-postback receiver.

This endpoint is internet-facing by definition (Dhan will not call
localhost), so the checks that matter are the refusals: no secret
configured, wrong secret, oversized body, malformed JSON — and that it
is reachable WITHOUT a session while auth is on, since a broker's
server cannot hold a cookie.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_dhan_postback")

import config
import dhan_order_ws as dow

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- ONE interpreter, two envelopes -------------------------------------
POSTBACK = {"dhanClientId": "1112539363", "orderId": "552209237",
            "orderStatus": "TRADED", "transactionType": "BUY",
            "securityId": "43492", "quantity": 65, "filled_qty": 65,
            "averageTradedPrice": 151.25}
ev = dow.normalize_event(POSTBACK)
check("the bare POSTBACK body normalizes (no Type/Data envelope)",
      ev is not None and ev["order_id"] == "552209237"
      and ev["status"] == "TRADED" and ev["traded_qty"] == 65
      and ev["avg_price"] == 151.25, str(ev)[:110])
ws_ev = dow.normalize_event({"Type": "order_alert", "Data": {
    "orderNo": "552209237", "status": "Traded", "securityId": "43492",
    "tradedQty": 65, "avgTradedPrice": 151.25}})
check("websocket and postback shapes produce the SAME normalized event",
      {k: ev[k] for k in ("order_id", "status", "security_id",
                          "traded_qty", "avg_price")}
      == {k: ws_ev[k] for k in ("order_id", "status", "security_id",
                                "traded_qty", "avg_price")})
check("unrelated JSON is still ignored",
      dow.normalize_event({"hello": "world"}) is None
      and dow.normalize_event({"Type": "heartbeat"}) is None)

# --- the endpoint's refusals and its one success path -------------------
import app as app_mod
from fastapi.testclient import TestClient

_orig_load = config.load
_base = config.load()
URL = "/api/dhan/postback/"


def _with(cfg_extra):
    config.load = lambda: {**_base, "auth_enabled": True, **cfg_extra}
    return TestClient(app_mod.app, raise_server_exceptions=False)


try:
    # no secret configured -> disabled, not open
    c = _with({"dhan_postback_secret": ""})
    r = c.post(URL + "anything", json=POSTBACK)
    check("with no secret configured the endpoint is DISABLED (503)",
          r.status_code == 503, f"{r.status_code}")

    SECRET = "s3cr3t-capability-url-token"
    c = _with({"dhan_postback_secret": SECRET})
    r = c.post(URL + "wrong-secret", json=POSTBACK)
    check("a wrong path secret is refused (401)", r.status_code == 401,
          f"{r.status_code}")

    r = c.post(URL + SECRET, json=POSTBACK)
    check("the correct path secret is accepted WITHOUT a session cookie "
          "(auth is on — a broker cannot log in)",
          r.status_code == 200 and r.json().get("ok") is True,
          f"{r.status_code} {r.text[:80]}")

    r = c.post(URL + SECRET, content=b"x" * (65 * 1024))
    check("an oversized body is refused (413)", r.status_code == 413,
          f"{r.status_code}")

    r = c.post(URL + SECRET, content=b"{not json")
    check("a malformed body is refused (400)", r.status_code == 400,
          f"{r.status_code}")

    # The payload actually reaches the book. `pilot.agents` is empty
    # until start() spawns threads, so inject a real ExecutionAgent
    # bound to the real bus — the routing under test is the lookup +
    # handler call, not the thread lifecycle.
    import agents as agents_mod
    ex = object.__new__(agents_mod.ExecutionAgent)
    ex.name = "execution"
    ex.bus = app_mod.pilot.bus
    _saved_agents = app_mod.pilot.agents
    app_mod.pilot.agents = [ex]
    try:
        app_mod.pilot.bus.set("positions", {"NIFTY": {
            "symbol": "NIFTY", "order_id": "UNCONFIRMED-ERROR",
            "security_id": "43492", "entry": 150.0, "qty": 65,
            "paper": False}})
        app_mod.pilot.bus.set("futures_positions", {})
        r = c.post(URL + SECRET, json=POSTBACK)
        p = (app_mod.pilot.bus.get("positions") or {}).get("NIFTY", {})
        check("a postback reaches the execution handler",
              r.status_code == 200 and "note" not in r.json(), r.text[:80])
        check("a postback resolves an UNCONFIRMED order id in the book",
              p.get("order_id") == "552209237", str(p)[:100])
        check("and books the real fill with measured slippage",
              p.get("entry") == 151.25 and p.get("entry_fill_slippage") == 1.25,
              str(p)[:120])
    finally:
        app_mod.pilot.agents = _saved_agents
finally:
    config.load = _orig_load

# --- inbound webhooks are reachable; everything else still is not -------
check("the postback path is auth-free by prefix",
      app_mod._auth_free("/api/dhan/postback/abc"))
check("the TradingView webhook is auth-free too "
      "(it was 401ing before its own secret check — v59.79 fix)",
      app_mod._auth_free("/api/tradingview/webhook"))
for guarded in ("/api/trades", "/api/settings", "/api/live/pnl",
                "/api/auth/users", "/api/dhan/postback"):
    check(f"{guarded} is still session-gated", not app_mod._auth_free(guarded))

# --- the secret never reaches the browser -------------------------------
pub = config.public_view({**_base, "dhan_postback_secret": "supersecret123456"})
check("the postback secret is masked in public_view",
      "dhan_postback_secret" not in pub
      and pub.get("dhan_postback_secret_set") is True
      and "supersecret123456" not in json.dumps(pub))
check("constant-time comparison is used on the public path",
      "hmac.compare_digest" in open(os.path.join(
          os.path.dirname(os.path.abspath(__file__)), "app.py")).read())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all dhan-postback checks passed")
