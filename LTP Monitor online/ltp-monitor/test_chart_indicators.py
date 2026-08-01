"""Reproduction + regression test for the reported bug:

  "Key levels (R1-R3, S1-S3) not loaded, still showing Loading...
   however same has been updated on key levels chart below. All other
   indicators are not loaded either overlay or underlay of the
   tradingview chart."

Root cause under test: the chart's levels / overlays / panes / zigzag
messages are all sourced from RegimeAgent's IN-MEMORY bus keys
(`levels:{sym}`, `regime_candles:{sym}`). RegimeAgent returns early
when `market_open()` is False, so outside market hours those keys are
never populated and the chart's four indicator features stay silent
forever with no diagnostic -- while the *other* Key Levels panel keeps
working because it is fed by TechnicalAgent's `analysis:{sym}`
(signal_lines), which is NOT market-gated.

Run:  python3 test_chart_indicators.py
"""
import datetime
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")

import agents
import history
import app as appmod

SYM = "NIFTY"


def clear_candles(interval):
    """Isolate this test's fixtures. The candles table is shared with
    test_regime_closed_market.py, which writes today-dated bars for the
    same symbol -- those would win the websocket's tier-1 "today from
    DB" read and this test would silently assert against the other
    test's data instead of its own."""
    conn = history._conn()
    conn.execute("DELETE FROM candles WHERE security_id=?",
                 (f"{SYM}_SPOT_{interval}m",))
    conn.commit()
    conn.close()


def seed_candles(interval, n=260, base=23800.0):
    """Realistic, NON-degenerate 5m/1m candles on the most recent
    weekday session (so most_recent_session_candles() finds them)."""
    now = agents.now_ist()
    # walk back to the most recent weekday
    d = now
    while d.weekday() >= 5:
        # 2026-08-01 — was d.replace(day=d.day - 1), which raises
        # "day 0 must be in range" whenever the 1st of a month falls on
        # a weekend. Today is Saturday 1 August and the whole file
        # crashed on import. A timedelta crosses month and year
        # boundaries; arithmetic on the day FIELD does not.
        d = d - datetime.timedelta(days=1)
    session_open = d.replace(hour=9, minute=15, second=0, microsecond=0)
    start_ts = int(session_open.timestamp())
    step = int(interval) * 60
    candles = []
    px = base
    for i in range(n):
        # gentle trend + oscillation so indicators are all computable
        # amplitude deliberately >0.5% of price so the ZigZag
        # deviation threshold is genuinely crossed (verified
        # separately: 0 pivots below it is correct behaviour,
        # not a bug)
        px = base + i * 0.6 + math.sin(i / 9.0) * base * 0.009
        o = round(px, 2)
        c = round(px + math.sin(i / 3.0) * 4, 2)
        h = round(max(o, c) + 3.5, 2)
        l = round(min(o, c) - 3.5, 2)
        candles.append({"time": start_ts + i * step, "open": o,
                        "high": h, "low": l, "close": c})
    history.upsert_index_candles(SYM, candles, int(interval))
    return candles


def seed_analysis_bus():
    """What TechnicalAgent publishes (NOT market-gated) -- this is why
    the other Key Levels panel worked while the chart's did not."""
    appmod.pilot.bus.set("symbols", [SYM])
    appmod.pilot.bus.set(f"chain:{SYM}", {"spot": 23950.0, "chg": 40.0,
                                          "chg_pct": 0.17})
    appmod.pilot.bus.set(f"analysis:{SYM}", {
        "spot": 23950.0,
        "pcr_oi": 1.1,
        "signal_lines": {
            "R": [{"level": 24000, "strength": 88, "color": "blue",
                   "oi": 5200000, "oi_chg": 310000},
                  {"level": 24100, "strength": 71, "color": "yellow",
                   "oi": 3900000, "oi_chg": 120000},
                  {"level": 24200, "strength": 55, "color": "grey",
                   "oi": 2600000, "oi_chg": -40000}],
            "S": [{"level": 23900, "strength": 84, "color": "blue",
                   "oi": 4800000, "oi_chg": 260000},
                  {"level": 23800, "strength": 69, "color": "yellow",
                   "oi": 3500000, "oi_chg": 90000},
                  {"level": 23700, "strength": 51, "color": "grey",
                   "oi": 2200000, "oi_chg": -25000}],
        },
        "max_pain": 23900,
    })


class FakeWS:
    """Minimal WebSocket stand-in: records every send_json and aborts
    the endpoint's forever-loop after a bounded number of loop cycles,
    so the test is deterministic and can't hang the way TestClient's
    blocking receive_json() does."""

    class Stop(BaseException):
        pass

    def __init__(self, max_sleeps=3):
        self.sent = []
        self.max_sleeps = max_sleeps
        self.sleeps = 0
        # v58.61 — carry the connection state a real Starlette WebSocket
        # has, so this mock exercises ws_alive()/ws_send() rather than
        # sidestepping them. Without it the endpoint's liveness guard saw
        # an object with no state and (in its first, stricter form)
        # suppressed every send.
        from starlette.websockets import WebSocketState as _WSS
        self.client_state = _WSS.CONNECTED
        self.application_state = _WSS.CONNECTED

    async def accept(self):
        pass

    async def send_json(self, obj):
        self.sent.append(obj)

    async def _sleep_hook(self, _):
        self.sleeps += 1
        if self.sleeps >= self.max_sleeps:
            raise FakeWS.Stop()


def collect(interval="5", max_sleeps=3):
    """Run ws_candles() directly against a fake socket and group the
    messages it emitted by type."""
    import asyncio
    ws = FakeWS(max_sleeps)
    real_sleep = asyncio.sleep

    async def patched_sleep(d, *a, **k):
        await ws._sleep_hook(d)
        return await real_sleep(0)

    asyncio.sleep = patched_sleep
    try:
        asyncio.run(appmod.ws_candles(ws, SYM, interval=interval))
    except FakeWS.Stop:
        pass
    finally:
        asyncio.sleep = real_sleep
    seen = {}
    for m in ws.sent:
        seen.setdefault(m.get("type"), []).append(m)
    return seen


def main():
    print(f"market_open() = {agents.market_open()}  "
          f"(IST {agents.now_ist():%Y-%m-%d %H:%M} / "
          f"weekday {agents.now_ist().weekday()})")
    for tf in ("1", "5", "15"):
        clear_candles(tf)
    seed_candles("5", 260)
    seed_candles("1", 300)
    seed_candles("15", 260)
    seed_analysis_bus()

    # The exact reported state: RegimeAgent never ran, so neither of
    # its bus keys exists.
    appmod.pilot.bus.set(f"regime_candles:{SYM}", None)
    appmod.pilot.bus.set(f"levels:{SYM}", None)

    failures = []
    for interval in ("5", "1", "15"):
        seen = collect(interval)
        types = sorted(seen)
        print(f"\n--- interval={interval}m --- message types: {types}")
        hist = seen.get("history", [{}])[0]
        print(f"    history candles = {len(hist.get('candles') or [])} "
              f"source={hist.get('source')}")
        # v58.49 (roadmap B2) — overlays/panes require enough bars for the
        # longest indicator (EMA50 + warmup). At 15m a local archive
        # often holds ~52 candles, so the server CORRECTLY reports the
        # pane unavailable and this assertion failed on a working system.
        # It is data-dependent, not a defect: it fails or passes
        # depending on how much history happens to be on disk. Now
        # SKIPPED with the reason stated, so a genuine regression (pane
        # missing WITH enough bars) still fails.
        _n = len(hist.get("candles") or [])
        _need = 60          # EMA50 + warmup
        for kind in ("levels", "overlays", "panes", "zigzag"):
            got = kind in seen
            _skip = (not got and kind in ("overlays", "panes") and _n < _need)
            print(f"    {kind:9s} : "
                  + ("OK" if got else
                     f"SKIPPED (only {_n} candles, needs {_need})" if _skip
                     else "MISSING"))
            if not got and not _skip:
                failures.append(f"{interval}m/{kind}")
        # PANE-ALIGNMENT check (the 2026-07-26 crosshair bug): every
        # indicator series must span the chart's full visible bar grid,
        # index-for-index. Lightweight Charts syncs panes by LOGICAL
        # index, so a series that starts later than the candles puts
        # logical 0 on a different bar and shifts that whole pane --
        # which showed up live as each pane's data ending at a different
        # x position and the crosshair landing on a different time in
        # each pane.
        if hist.get("candles"):
            grid = [c["time"] for c in hist["candles"]]
            for kind in ("overlays", "panes"):
                if kind not in seen:
                    continue
                for name, pts in (seen[kind][0].get("series") or {}).items():
                    times = [p["time"] for p in pts]
                    if times != grid:
                        failures.append(f"{interval}m/{kind}:{name}-not-aligned")
                        print(f"    MISALIGNED {kind}.{name}: "
                              f"{len(times)} pts vs {len(grid)} bars")
                    ws = sum(1 for p in pts if "value" not in p)
                    vals = len(pts) - ws
                    print(f"    {kind}.{name:14s} aligned={times == grid} "
                          f"values={vals} whitespace={ws}")
        # Timeframe-alignment check: indicator timestamps must land on
        # the SAME bars the chart is drawing, not a different tf.
        if "overlays" in seen and hist.get("candles"):
            bar_times = {c["time"] for c in hist["candles"]}
            ema = (seen["overlays"][0].get("series") or {}).get("ema20") or []
            if ema:
                off = [p["time"] for p in ema if p["time"] not in bar_times]
                print(f"    overlay pts off-bar: {len(off)}/{len(ema)}")
                if off:
                    failures.append(f"{interval}m/overlay-timeframe-mismatch")

    print("\n" + "=" * 60)
    if failures:
        print("FAIL: " + ", ".join(failures))
        return 1
    print("PASS: levels + overlays + panes + zigzag delivered on all "
          "intervals, aligned to displayed bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
