#!/usr/bin/env python3
"""test_live_interfaces.py — v59.75, the live-test Dhan interface set.

What a test can honestly verify without a live account: the requests
this code SENDS (endpoint, verb, body), the tolerance of the fill
parser to Dhan's inconsistent wrapping, the fill-capture behaviour
against fake brokers, and the strict paper/live separation of the
P&L books. The response SHAPES stay unverified until the first
authenticated session — the code and this file both say so.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_live_interfaces")

import broker_adapter as ba

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- the wrappers send the documented requests --------------------------
CALLS = []


class _Resp:
    status_code = 200
    text = "{}"
    def json(self):
        return {"status": "success", "data": []}
    def raise_for_status(self):
        pass


class _Rec:
    def _hit(self, verb, url, **kw):
        CALLS.append((verb, url, kw.get("json")))
        return _Resp()
    def get(self, url, **kw):
        return self._hit("GET", url, **kw)
    def post(self, url, **kw):
        return self._hit("POST", url, **kw)
    def put(self, url, **kw):
        return self._hit("PUT", url, **kw)
    def delete(self, url, **kw):
        return self._hit("DELETE", url, **kw)


class _FakeClient:
    client_id = "111"
    _h = {"access-token": "x"}


_orig_requests = ba.requests
try:
    ba.requests = _Rec()
    o = ba.DhanOrders(_FakeClient())
    o.orders()
    o.trade_book()
    o.trade_book("42")
    o.order_by_correlation("LTP123")
    o.modify("9", order_type="LIMIT", qty=50, price=1.5)
    o.cancel("9")
    o.funds()
    o.positions()
finally:
    ba.requests = _orig_requests

_paths = [(v, u.replace(ba.API, "")) for v, u, _ in CALLS]
for expect in [("GET", "/orders"), ("GET", "/trades"), ("GET", "/trades/42"),
               ("GET", "/orders/external/LTP123"), ("PUT", "/orders/9"),
               ("DELETE", "/orders/9"), ("GET", "/fundlimit"),
               ("GET", "/positions")]:
    check(f"{expect[0]} {expect[1]} is requested", expect in _paths)
_modify_body = next(b for v, u, b in CALLS if v == "PUT")
check("modify body carries id/qty/price/type",
      _modify_body.get("orderId") == "9" and _modify_body.get("quantity") == 50
      and _modify_body.get("price") == 1.5
      and _modify_body.get("orderType") == "LIMIT")

# --- parse_fills tolerates Dhan's wrapping shapes ------------------------
fills = [{"orderId": "42", "tradedPrice": 100.0, "tradedQuantity": 40},
         {"orderId": "42", "tradedPrice": 101.0, "tradedQuantity": 25},
         {"orderId": "99", "tradedPrice": 500.0, "tradedQuantity": 10}]
px, qty = ba.parse_fills(fills, order_id="42")
check("weighted average across partial fills, other orders excluded",
      qty == 65 and abs(px - (100 * 40 + 101 * 25) / 65) < 0.01,
      f"avg {px} x{qty}")
check("data-wrapped dict shape parses",
      ba.parse_fills({"data": fills}, order_id="42")[1] == 65)
check("single-dict shape parses",
      ba.parse_fills({"tradedPrice": 99.5, "tradedQuantity": 30}) == (99.5, 30))
check("empty/garbage yields (None, 0), never invented numbers",
      ba.parse_fills([]) == (None, 0)
      and ba.parse_fills(None) == (None, 0)
      and ba.parse_fills([{"tradedPrice": "x"}]) == (None, 0))

# --- _actual_fill against fake brokers ----------------------------------
import agents


class FakeBus:
    def __init__(self):
        self.alerts, self.logs = [], []
    def get(self, k, d=None):
        return d
    def set(self, k, v):
        pass
    def log(self, name, msg):
        self.logs.append(msg)
    def alert(self, sev, src, sym, msg):
        self.alerts.append((sev, msg))


class _FillOrders:
    def __init__(self, fills):
        self._f = fills
    def trade_book(self, oid):
        return self._f


ex = object.__new__(agents.ExecutionAgent)
ex.name = "execution"
ex.bus = FakeBus()
px, q = ex._actual_fill(_FillOrders(fills), {"orderId": "42"}, 65, "BUY X")
check("_actual_fill returns the broker's average fill",
      q == 65 and abs(px - 100.38) < 0.01, f"{px} x{q}")
check("a complete fill raises no alert", not ex.bus.alerts)
ex.bus = FakeBus()
px, q = ex._actual_fill(_FillOrders(fills[:1]), {"orderId": "42"}, 65, "BUY X")
check("a PARTIAL fill is a HIGH alert",
      q == 40 and any(s == "high" and "PARTIAL" in m for s, m in ex.bus.alerts))


class _BoomOrders:
    def trade_book(self, oid):
        raise RuntimeError("api down")


ex.bus = FakeBus()
check("an unreachable trade book keeps the quote and says so",
      ex._actual_fill(_BoomOrders(), {"orderId": "42"}, 65, "X") == (None, 0)
      and any("unavailable" in m for m in ex.bus.logs))

# --- live vs paper books are strictly separate --------------------------
import app as app_mod
rows = [{"pnl": 100, "fees": 10, "slippage": 5, "paper": True},
        {"pnl": -50, "fees": 10, "slippage": 5, "paper": True},
        {"pnl": 700, "fees": 20, "slippage": 8, "paper": False},
        {"pnl": -200, "fees": 20, "slippage": 8}]          # no flag → paper
with open(store.path("trades.jsonl"), "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
app_mod._LIFETIME_TOTALS_CACHE.update(mtime=None, totals=None)
t = app_mod._lifetime_trade_totals()
check("paper book sums only paper rows (missing flag counts as paper)",
      t["paper"]["realized"] == -150 and t["paper"]["count"] == 3,
      str(t["paper"]))
check("live book sums only live rows",
      t["live"]["realized"] == 700 and t["live"]["count"] == 1
      and t["live"]["fees"] == 20, str(t["live"]))
check("combined stays for backward compatibility and equals the sum",
      t["realized"] == 550 and t["count"] == 4)

# --- the ground-truth endpoint exists and degrades honestly -------------
check("/api/live/pnl route is registered",
      any(getattr(r, "path", "") == "/api/live/pnl"
          for r in app_mod.app.routes))
out = app_mod.api_live_pnl()
check("without broker credentials it reports state, not zeros",
      out.get("paper_mode") is True and "internal_live_book" in out
      and out.get("internal_live_realized") == 700, str(out)[:160])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all live-interface checks passed")
