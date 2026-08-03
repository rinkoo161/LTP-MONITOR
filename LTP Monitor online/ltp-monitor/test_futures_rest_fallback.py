#!/usr/bin/env python3
"""test_futures_rest_fallback.py — the REST fallback must not depend on
the websocket it falls back FROM.

2026-08-03, from live data: `future_oi_snapshots` held exactly ONE day
(31 July) while `chain_snapshots` held 5.4 days. The app ran the whole
session on 3 August and archived no futures OI at all.

Cause, end to end:

    dhanhq missing from the interpreter the app was launched with
      -> _ensure_ws_client() returns None
      -> _sync_ws_feed() returns early
      -> _ensure_futures_subscribed() never runs   (its ONLY caller)
      -> _future_sec_ids stays empty
      -> _poll_futures_via_rest() returns on `if not future_sec_ids`
      -> _classify_future_tick() never called
      -> log_future_oi() never reached

Every step was silent. No exception, no log line, and the one success
message the archive does emit ("futures OI archive active") had fired
once on 31 July and never again — which read as "still fine" rather
than "has not run since".

The tests below are BEHAVIOURAL: they drive the real methods with a
stub scrip master and assert on what lands in the maps the REST poller
actually reads, rather than scraping source text. The one source-level
check is for a call site, which has no runtime signature.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_futures_rest_fallback")

import agents

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeBus:
    def __init__(self, symbols):
        self.store = {"symbols": symbols}
        self.logs = []

    def get(self, k, d=None):
        return self.store.get(k, d)

    def set(self, k, v):
        self.store[k] = v

    def log(self, who, msg):
        self.logs.append(msg)


class FakeScripMaster:
    """Stands in for dhan_scrip_master. Shape copied from the real
    return value: {sym: ([contract, ...], detail_dict)}."""
    def __init__(self):
        self.calls = 0

    def get_current_futures_for_symbols(self, symbols, n=3):
        self.calls += 1
        exp = datetime.datetime(2026, 8, 25, tzinfo=agents.IST)
        out = {}
        for i, s in enumerate(symbols):
            out[s] = ([{"security_id": str(58000 + i * 10 + j),
                        "symbol_name": f"{s}-Aug2026-FUT",
                        "expiry": exp} for j in range(3)], {})
        return out


class FakeClient:
    def __init__(self, ok=True):
        self.ok = ok
        self.subscribed = []

    def subscribe_more(self, sym, sec_id):
        self.subscribed.append((sym, sec_id))
        return self.ok


def agent(symbols=("NIFTY", "BANKNIFTY")):
    a = agents.MarketDataAgent.__new__(agents.MarketDataAgent)
    a.name = "market_data"
    a.bus = FakeBus(list(symbols))
    return a


_real_sm = agents.dhan_scrip_master

print("1) RESOLUTION WORKS WITH NO WEBSOCKET CLIENT (the actual fix)")
sm = FakeScripMaster()
agents.dhan_scrip_master = sm
try:
    a = agent()
    a._ensure_futures_subscribed(None)          # REST mode — no client
    ids = getattr(a, "_future_sec_ids", {})
    roles = getattr(a, "_future_roles", {})
    check("contracts land in _future_sec_ids without a client", len(ids) == 6,
          f"{len(ids)} resolved — this map is what _poll_futures_via_rest reads")
    check("front month is marked", "front" in roles.values())
    check("far months are marked", "month2" in roles.values()
          and "month3" in roles.values())
    check("front expiry is published for enter_future()",
          a.bus.get("future_expiry:NIFTY") == "2026-08-25")

    print("\n2) THE REST POLLER CAN NOW SEE THEM")
    # The guard that returned instantly for a whole session.
    check("the map the poller guards on is non-empty",
          bool(getattr(a, "_future_roles", None))
          and bool(getattr(a, "_future_sec_ids", None)))

    print("\n3) A LATE WEBSOCKET STILL SUBSCRIBES WHAT REST RESOLVED")
    # Resolution stamped `checked` for today. Subscription must NOT be
    # gated by that stamp, or a ws connecting after resolution (a
    # reconnect, or dhanhq only importable after a restart) would never
    # subscribe anything for the rest of the day.
    c = FakeClient(ok=True)
    a._ensure_futures_subscribed(c)
    check("the client subscribed the already-resolved contracts",
          len(c.subscribed) == 6, f"{len(c.subscribed)} subscribed")
    a._ensure_futures_subscribed(c)
    check("a second pass does not re-subscribe the same contracts",
          len(c.subscribed) == 6,
          f"{len(c.subscribed)} calls after two passes — a growing count "
          f"would mean subscribe_more is hit every 3s cycle forever")

    print("\n4) A FAILING SUBSCRIPTION IS RETRIED, NOT LOST")
    a2 = agent(("NIFTY",))
    bad = FakeClient(ok=False)
    a2._ensure_futures_subscribed(bad)
    n_first = len(bad.subscribed)
    check("resolution succeeded even though subscription failed",
          len(getattr(a2, "_future_sec_ids", {})) == 3,
          "REST must still have the contracts")
    good = FakeClient(ok=True)
    a2._ensure_futures_subscribed(good)
    check("the next pass retries the unsubscribed contracts",
          len(good.subscribed) == 3,
          f"first pass tried {n_first}, retry tried {len(good.subscribed)}")

    print("\n5) AN EMPTY MAP IS ANNOUNCED, NOT SILENT")
    a3 = agent()
    a3._poll_futures_via_rest()
    said = [m for m in a3.bus.logs if "futures" in m.lower()]
    check("the poller logs when no futures are resolved", bool(said),
          said[0][:80] if said else "SILENT — this is the bug that cost a session")
    check("and it names the consequence for the archive",
          any("archive" in m.lower() for m in said),
          "an operator must be able to tell this from a quiet market")
finally:
    agents.dhan_scrip_master = _real_sm

print("\n6) RESOLUTION IS REACHABLE WITHOUT THE WEBSOCKET PATH")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "agents.py")).read()
_code = "\n".join(l for l in SRC.split("\n") if not l.strip().startswith("#"))
calls = [l.strip() for l in _code.split("\n")
         if "_ensure_futures_subscribed(" in l and "def " not in l]
check("it is called from more than just _sync_ws_feed", len(calls) >= 2,
      f"{len(calls)} call sites: {calls}")
check("at least one call site passes no client", any("(None)" in c for c in calls),
      "the REST-mode call")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all futures REST-fallback checks passed")
