"""dhan_order_ws.py — persistent Dhan ORDER-UPDATE websocket client.

v59.76. The reverse channel for live orders: Dhan pushes every order
event (Pending/Traded/Rejected/Cancelled/Part-Traded) over a websocket
the CLIENT connects out to — no postback URL, no public exposure of
this machine, which is exactly right for an app whose config holds
plaintext keys and whose auth is trusted-network-only.

PROVENANCE, same discipline as dhan_ws.py: the endpoint, the auth
message shape and the `{"Type": "order_alert", "Data": {...}}`
envelope are copied from the installed first-party package's
`dhanhq/orderupdate.py` source (v2.x), not guessed from docs. That
class has no reconnect loop, no stop, and prints to stdout, so this
wraps the PROTOCOL it demonstrates in the lifecycle this app needs:
own thread, exponential backoff (5s→60s), a stop event, a status dict
for the dashboard, and a callback into the caller instead of prints.

HONEST STATUS: not yet exercised against a live session (the same
UNVERIFIED caveat as the v59.75 REST wrappers). The recv loop uses a
5s wait_for timeout to poll the stop flag; cancelling a websockets
recv() can in principle drop the in-flight frame — acceptable here
because order events are sparse and every consumer of these updates is
a BELT on top of the polling confirm (_confirm_order/_actual_fill) and
the positions reconciler, never the only source of truth.

Field names in events, per Dhan's order-alert schema: Data.orderNo,
Data.status, Data.correlationId, Data.securityId, Data.txnType,
Data.quantity, Data.tradedQty, Data.avgTradedPrice / Data.price.
normalize_event() parses tolerantly (camel/snake variants) and is the
ONE place event fields are interpreted.
"""
import asyncio
import json
import threading
import time

ORDER_FEED_WSS = "wss://api-order-update.dhan.co"

TERMINAL_BAD = ("REJECTED", "CANCELLED")


def auth_message(client_id, token):
    """The login frame, byte-for-byte the shape the first-party
    orderupdate.py sends (MsgCode 42, UserType SELF)."""
    return {"LoginReq": {"MsgCode": 42,
                         "ClientId": str(client_id),
                         "Token": str(token)},
            "UserType": "SELF"}


def normalize_event(msg):
    """An order event → a flat dict, or None for anything else.

    Accepts BOTH envelopes Dhan uses, because there must be exactly one
    interpreter of order-event fields (v59.79):

      * websocket: {"Type": "order_alert", "Data": {...}}
      * POSTBACK : the bare JSON body Dhan POSTs to a configured URL
                   (no envelope, no "Type" — identified by carrying an
                   order id / status).

    Returns {"order_id", "status", "correlation_id", "security_id",
    "txn_type", "qty", "traded_qty", "avg_price", "raw"} with None for
    absent fields. Shape-tolerant for the same reason parse_fills() is:
    Dhan mixes camelCase, snake_case and wrapping across surfaces."""
    if not isinstance(msg, dict):
        return None
    if msg.get("Type") == "order_alert":
        d = msg.get("Data")
    elif any(k in msg for k in ("orderId", "orderNo", "order_id",
                                "orderStatus")):
        d = msg               # postback: the body IS the payload
    else:
        return None
    if not isinstance(d, dict) or not d:
        return None          # an alert with no payload is noise, not an event

    def _s(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, ""):
                return str(v)
        return None

    def _f(*keys):
        for k in keys:
            try:
                v = d.get(k)
                if v not in (None, ""):
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def _i(*keys):
        v = _f(*keys)
        return int(v) if v is not None else None

    return {"order_id": _s("orderNo", "orderId", "order_id"),
            "status": (_s("status", "orderStatus") or "").upper() or None,
            "correlation_id": _s("correlationId", "correlation_id"),
            "security_id": _s("securityId", "security_id"),
            "txn_type": _s("txnType", "transactionType"),
            "qty": _i("quantity", "qty"),
            # filled_qty / averageTradedPrice are the POSTBACK spellings
            # (v59.79) — same quantities, different surface.
            "traded_qty": _i("tradedQty", "tradedQuantity", "filledQty",
                             "filled_qty"),
            "avg_price": _f("avgTradedPrice", "averageTradedPrice",
                            "tradedPrice", "price"),
            "raw": d}


class OrderUpdateClient:
    """Threaded connect/auth/listen loop with reconnect and stop."""

    def __init__(self, client_id, token, on_event, log=lambda m: None):
        self.client_id, self.token = client_id, token
        self.on_event, self.log = on_event, log
        self._stop = threading.Event()
        self._thread = None
        self.state = "off"
        self.connected_at = None
        self.events = 0
        self.last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="dhan-order-ws")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.state = "stopped"

    def status(self):
        return {"state": self.state, "connected_at": self.connected_at,
                "events": self.events, "last_error": self.last_error}

    async def _session(self):
        import websockets     # dependency of the dhanhq package
        async with websockets.connect(ORDER_FEED_WSS, ping_interval=20,
                                      ping_timeout=20) as ws:
            await ws.send(json.dumps(auth_message(self.client_id,
                                                  self.token)))
            self.state = "connected"
            self.connected_at = time.time()
            self.log("order-update websocket connected")
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self.events += 1
                try:
                    self.on_event(msg)
                except Exception as e:
                    # A consumer bug must not kill the feed; it is
                    # logged, not swallowed silently.
                    self.log(f"order-update handler failed: "
                             f"{type(e).__name__}: {e}")

    def _run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                self.state = "connecting"
                asyncio.run(self._session())
                backoff = 5           # clean close → quick reconnect
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.log(f"order-update websocket dropped: "
                         f"{self.last_error} — retry in {backoff}s")
            if self._stop.is_set():
                break
            self.state = f"reconnecting ({backoff}s)"
            self._stop.wait(backoff)
            backoff = min(60, backoff * 2)
        self.state = "stopped"
