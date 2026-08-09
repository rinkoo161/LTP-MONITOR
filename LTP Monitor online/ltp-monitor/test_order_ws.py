#!/usr/bin/env python3
"""test_order_ws.py — v59.76, the Dhan order-update websocket.

What is testable without a live session: the auth frame (copied from
the first-party package's source — asserted against that shape, not
against docs), the tolerant event normalizer, the handler's three jobs
(id resolution, phantom alerts, fill booking) against fake buses, and
the live-only lifecycle. The wire protocol itself stays UNVERIFIED
until the first authenticated session, and the module says so."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_order_ws")

import agents
import config
import dhan_order_ws as dow

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeBus:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.alerts, self.logs = [], []
    def get(self, k, d=None):
        return self.state.get(k, d)
    def set(self, k, v):
        self.state[k] = v
    def log(self, name, msg):
        self.logs.append(msg)
    def alert(self, sev, src, sym, msg):
        self.alerts.append((sev, msg))


# --- the auth frame matches the first-party package's shape -------------
m = dow.auth_message("1000000001", "JWT")
check("auth frame is the package's LoginReq shape",
      m == {"LoginReq": {"MsgCode": 42, "ClientId": "1000000001",
                         "Token": "JWT"}, "UserType": "SELF"})

# --- normalizer ---------------------------------------------------------
ev = dow.normalize_event({"Type": "order_alert", "Data": {
    "orderNo": "112", "status": "Traded", "securityId": "43492",
    "txnType": "BUY", "quantity": 65, "tradedQty": 65,
    "avgTradedPrice": 151.25, "correlationId": "LTP42"}})
check("order_alert normalizes with typed fields",
      ev["order_id"] == "112" and ev["status"] == "TRADED"
      and ev["traded_qty"] == 65 and ev["avg_price"] == 151.25
      and ev["correlation_id"] == "LTP42")
check("non-alert frames are ignored",
      dow.normalize_event({"Type": "heartbeat"}) is None
      and dow.normalize_event("garbage") is None
      and dow.normalize_event({"Type": "order_alert", "Data": None}) is None)
check("snake_case variants parse too",
      dow.normalize_event({"Type": "order_alert",
                           "Data": {"order_id": "9", "orderStatus": "Rejected",
                                    "tradedQuantity": 10, "price": "99.5"}}
                          )["avg_price"] == 99.5)

# --- the handler's three jobs -------------------------------------------
ex = object.__new__(agents.ExecutionAgent)
ex.name = "execution"

def _pos(oid, sec="43492", entry=150.0):
    return {"symbol": "NIFTY", "order_id": oid, "security_id": sec,
            "entry": entry, "qty": 65, "paper": False}

# 1. UNCONFIRMED id resolved via security_id match
ex.bus = FakeBus({"positions": {"NIFTY": _pos("UNCONFIRMED-ERROR")},
                  "futures_positions": {}})
ex._on_order_event({"Type": "order_alert", "Data": {
    "orderNo": "112", "status": "Traded", "securityId": "43492",
    "tradedQty": 65, "avgTradedPrice": 151.25}})
p = ex.bus.get("positions")["NIFTY"]
check("UNCONFIRMED order id is resolved from the feed",
      p["order_id"] == "112" and p["order_status"] == "TRADED")
check("the real fill is booked with measured slippage",
      p["entry"] == 151.25 and p["quote_at_entry"] == 150.0
      and p["entry_fill_slippage"] == 1.25)
check("the event lands in the bounded feed",
      len(ex.bus.get("order_update_feed")) == 1)

# 2. REJECTED on a tracked order → HIGH phantom alert
ex.bus = FakeBus({"positions": {"NIFTY": _pos("77")},
                  "futures_positions": {}})
ex._on_order_event({"Type": "order_alert", "Data": {
    "orderNo": "77", "status": "Rejected", "securityId": "43492"}})
check("a REJECTED tracked order raises a HIGH phantom alert",
      any(s == "high" and "PHANTOM" in msg for s, msg in ex.bus.alerts),
      str(ex.bus.alerts))
check("and the entry price is NOT touched on a rejection",
      ex.bus.get("positions")["NIFTY"]["entry"] == 150.0)

# 3. unrelated events touch nothing
ex.bus = FakeBus({"positions": {"NIFTY": _pos("77")},
                  "futures_positions": {}})
ex._on_order_event({"Type": "order_alert", "Data": {
    "orderNo": "999", "status": "Traded", "securityId": "10101"}})
check("an event for someone else's order changes no position",
      ex.bus.get("positions")["NIFTY"]["order_id"] == "77"
      and "order_status" not in ex.bus.get("positions")["NIFTY"])

# feed stays bounded
ex.bus = FakeBus({"positions": {}, "futures_positions": {}})
for i in range(150):
    ex._on_order_event({"Type": "order_alert",
                        "Data": {"orderNo": str(i), "status": "Traded"}})
check("the update feed is capped at 100",
      len(ex.bus.get("order_update_feed")) == 100)

# --- live-only lifecycle ------------------------------------------------
class _FakeClient:
    started = stopped = False
    def __init__(self, *a, **k):
        _FakeClient.last = self
    def start(self):
        self.started = True
    def stop(self):
        self.stopped = True
    def status(self):
        return {"state": "connected"}

import dhan_order_ws as _dow_mod
_orig_client, _orig_load = _dow_mod.OrderUpdateClient, config.load
_base = config.load()
try:
    _dow_mod.OrderUpdateClient = _FakeClient
    lex = object.__new__(agents.ExecutionAgent)
    lex.name = "execution"
    lex.bus = FakeBus()
    config.load = lambda: {**_base, "paper_mode": True}
    lex._order_ws_manage()
    check("paper mode never opens the socket",
          getattr(lex, "_order_ws", None) is None
          and lex.bus.get("order_ws") == {"state": "off"})
    config.load = lambda: {**_base, "paper_mode": False, "broker": "dhan",
                           "dhan_client_id": "1", "dhan_access_token": "t",
                           "order_update_ws_enabled": True}
    lex._order_ws_manage()
    check("live mode with creds starts the client",
          getattr(lex, "_order_ws", None) is not None
          and _FakeClient.last.started
          and lex.bus.get("order_ws") == {"state": "connected"})
    config.load = lambda: {**_base, "paper_mode": True}
    lex._order_ws_manage()
    check("flipping back to paper stops it",
          getattr(lex, "_order_ws", None) is None and _FakeClient.last.stopped)
finally:
    _dow_mod.OrderUpdateClient = _orig_client
    config.load = _orig_load

check("'order_update_ws_enabled' registered in DEFAULTS",
      "order_update_ws_enabled" in config.DEFAULTS)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all order-websocket checks passed")
