#!/usr/bin/env python3
"""test_watch_chain_snapshots.py — per-strike OI/IV archive for watchlist names.

The watch loop in BacktestAgent archives option-leg and futures CANDLES
once a day. Its own comment says the point is that a candidate
instrument accumulates history so "its liquidity can be measured"
before anyone decides whether to trade it — and liquidity is OI, volume
and bid/ask, all of which live in `chain_snapshots` and NONE of which
live in `candles`. Measured on 2026-08-06: ADANIENSOL had 803 candle
rows and ZERO snapshot rows, against ~30k per index.

This drives the REAL TechnicalAgent method against a stub broker rather
than asserting on source strings. That distinction is not academic here
— twice in this project a source-presence check passed while the code
under it raised on every cycle (the `cfg` NameError in this very watch
feature, and the rail-label CSS class). A string cannot see a runtime
binding error, so this executes the path and reads the database.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_watch_chain_snapshots")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config
import history

WSYM = "ADANIENSOL"


# Build legs through the PRODUCER'S OWN function rather than by hand.
# A stub that invents the shape cannot detect a mismatch with the code
# that really feeds this path — the first draft of this test did exactly
# that, used "strikes" where the producer emits "rows", and failed with
# a KeyError. Feed broker_adapter._leg() Dhan-shaped raw input and the
# shape is right by construction.
import broker_adapter as _ba


def _leg(ltp, oi, iv):
    return _ba._leg({
        "last_price": ltp, "oi": oi, "previous_oi": oi - 10,
        "previous_close_price": ltp - 1, "volume": 500,
        "implied_volatility": iv,
        "top_bid_price": ltp - 0.5, "top_ask_price": ltp + 0.5,
        "greeks": {"delta": 0.5, "gamma": 0.01, "theta": -2.0, "vega": 1.2},
    })


class StubBroker:
    """Records every chain fetch, so 'did it call once or every cycle?'
    is answerable rather than assumed."""

    def __init__(self, blow_up=None):
        self.calls = []
        self.blow_up = blow_up

    def option_chain(self, symbol):
        self.calls.append(symbol)
        if self.blow_up:
            raise self.blow_up
        # Same keys DhanClient.option_chain returns: rows/spot/totals/...
        return {
            "symbol": symbol, "spot": 940.0, "expiry": "2026-08-25",
            "timestamp": "06-Aug-2026 10:00:00", "totals": {},
            "source": "stub",
            "rows": [
                {"strike": 920.0, "ce": _leg(28.0, 120000, 31.0),
                 "pe": _leg(9.0, 90000, 33.0)},
                {"strike": 940.0, "ce": _leg(16.0, 240000, 30.0),
                 "pe": _leg(17.0, 210000, 30.5)},
                {"strike": 960.0, "ce": _leg(8.0, 150000, 30.0),
                 "pe": _leg(29.0, 80000, 32.0)},
            ],
        }


def _agent(broker):
    bus = agents.Bus()
    bus.set("symbols", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
    a = agents.TechnicalAgent(bus, {"dhan_client": lambda: broker})
    return a, bus


# The method is gated on the F&O session being open. Force it, so the
# test is not silently a no-op when run after hours — which is exactly
# when a suite tends to be run.
_real_session = agents.fno_session_open
agents.fno_session_open = lambda: True
# v60.00 — the WRITE boundary (history.upsert_chain_snapshot) now also
# checks in_market_session, after 50.6% of the real table turned out to
# be after-hours junk. This test drives the archive mechanics with
# now() timestamps, so simulate market hours the same way as above.
_real_ims = agents.in_market_session
agents.in_market_session = lambda ts: True

cfg = config.load()
cfg["watch_symbols"] = [WSYM]
cfg["watch_snapshot_interval_sec"] = 300
config.save(cfg)

try:
    print("1) it actually writes per-strike OI/IV — the whole point")
    broker = StubBroker()
    a, bus = _agent(broker)
    a._maybe_snapshot_watch_symbols()
    check("the broker was asked for the chain", broker.calls == [WSYM],
          str(broker.calls))
    conn = history._conn()
    rows = list(conn.execute(
        "SELECT strike,leg,ltp,oi,iv,bid,ask FROM chain_snapshots "
        "WHERE symbol=? ORDER BY strike,leg", (WSYM,)))
    conn.close()
    check("snapshot rows landed", len(rows) == 6, f"{len(rows)} rows (3 strikes x 2 legs)")
    oi = {(r[0], r[1]): r[3] for r in rows}
    check("OI is stored, not just price", oi.get((940.0, "ce")) == 240000,
          f"{oi.get((940.0, 'ce'))!r} — OI is the reason this table exists")
    ivs = [r[4] for r in rows]
    check("IV is stored", all(v is not None for v in ivs), str(ivs))
    spreads = [(r[6] - r[5]) for r in rows]
    check("bid/ask survive the round trip", all(abs(s - 1.0) < 1e-6 for s in spreads),
          "bid/ask spread is half of what 'measure the liquidity' means")

    print("\n2) ARCHIVE ONLY — the separation the design rests on")
    check("the watch symbol is NOT in the bus symbols list",
          WSYM not in (bus.get("symbols") or []),
          "that list drives strategy, risk and execution")
    check("no analysis: key was written for it",
          bus.get(f"analysis:{WSYM}") is None,
          "strategies consume analysis:{sym} — writing one is how an "
          "archive-only name becomes tradeable by accident")
    check("no chain: key was written for it",
          bus.get(f"chain:{WSYM}") is None)

    print("\n3) it is THROTTLED, and slower than the traded symbols")
    before = len(broker.calls)
    a._maybe_snapshot_watch_symbols()
    a._maybe_snapshot_watch_symbols()
    check("a second call inside the interval does NOT refetch",
          len(broker.calls) == before, f"{len(broker.calls) - before} extra fetches")
    check("the default cadence is registered in DEFAULTS",
          "watch_snapshot_interval_sec" in config.DEFAULTS,
          "config.save() silently drops unregistered keys")
    check("and is SLOWER than the index cadence",
          config.DEFAULTS["watch_snapshot_interval_sec"]
          > config.DEFAULTS.get("chain_snapshot_interval_sec", 60),
          f"{config.DEFAULTS['watch_snapshot_interval_sec']}s vs "
          f"{config.DEFAULTS.get('chain_snapshot_interval_sec', 60)}s — the "
          f"chain endpoint is shared with the four traded symbols")

    print("\n4) a broker failure degrades quietly and BACKS OFF ITS OWN PATH")
    import rate_limit
    rate_limit.reset("watch_chain")
    rate_limit.reset("quote")
    bad = StubBroker(blow_up=RuntimeError("Dhan rate limit hit — 429"))
    a2, bus2 = _agent(bad)
    a2._maybe_snapshot_watch_symbols()          # must not raise
    check("a 429 does not propagate", True, "it returned instead of raising")
    check("it backs off the watch path", rate_limit.is_limited("watch_chain"),
          "otherwise it retries into a limit every cycle")
    check("and does NOT back off the traded symbols' path",
          not rate_limit.is_limited("quote"),
          "a never-traded name must not be able to stall NIFTY/BANKNIFTY")
    rate_limit.reset("watch_chain")

    print("\n5) out of session it does nothing at all")
    agents.fno_session_open = lambda: False
    idle = StubBroker()
    a3, _ = _agent(idle)
    a3._maybe_snapshot_watch_symbols()
    check("no fetch when the F&O session is closed", idle.calls == [],
          str(idle.calls))
    agents.fno_session_open = lambda: True

    print("\n6) an empty watchlist costs nothing")
    cfg2 = config.load()
    cfg2["watch_symbols"] = []
    config.save(cfg2)
    empty = StubBroker()
    a4, _ = _agent(empty)
    a4._maybe_snapshot_watch_symbols()
    check("no watch symbols -> no broker call", empty.calls == [], str(empty.calls))

    print("\n7) it is wired into the cycle, not just defined")
    HERE = os.path.dirname(os.path.abspath(__file__))
    AG = open(os.path.join(HERE, "agents.py")).read()
    body = AG.split("class TechnicalAgent(Agent):")[1]
    body = body[:body.index("\n    def _compute_technical")]
    check("TechnicalAgent.cycle() calls it",
          "_maybe_snapshot_watch_symbols()" in body,
          "defined-but-never-called is how this feature failed the first time")
finally:
    agents.fno_session_open = _real_session
    agents.in_market_session = _real_ims

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all watch chain-snapshot checks passed")
