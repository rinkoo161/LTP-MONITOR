"""MarketSense → LTP Monitor read-only bridge (2026-08-08).

Polls the MarketSense REST API (separate process, default :8100) and
mirrors its event/signal layer onto the bus for display and future gate
use. READ-ONLY BY DESIGN: this module never places orders, never touches
a broker, and MarketSense being down degrades to a stale-flagged summary
— it can never block or break the trading loop. The Dhan-only broker
constraint is untouched: MarketSense uses its own data sources in its
own process; nothing here introduces a second broker.

Duck-types the Orchestrator's agent interface (same precedent as
NewsMacroAgent) to avoid a circular import with agents.py.

Bus keys written:
    ms_events            list of high-materiality events (dashboard feed)
    ms_watchlist         top-conviction BUY signals from MarketSense
    ms_event_flag:{SYM}  latest high-materiality event for that symbol
    ms_risk_flag:{SYM}   set when MarketSense's A6 verdict is not clear
    ms_levels:{SYM}      entry/target/stop levels from the signal layer
    ms_link              health of the link itself (ok/stale + counts)

Consumers must treat these as advisory display data. If a future change
wants ms_risk_flag to gate order entry, that gate belongs in RiskAgent
server-side (per the standing convention that a disabled button is only
a UI hint) — not here.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config

IST = timezone(timedelta(hours=5, minutes=30))


def _get_json(base, path, timeout=10):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class MarketSenseAgent(threading.Thread):
    """Read-only poller for the MarketSense API."""

    name = "marketsense"
    interval = 60   # cheap due-check; actual poll cadence is configurable

    def __init__(self, bus, ctx):
        super().__init__(daemon=True)
        self.bus, self.ctx = bus, ctx
        self.stop_evt = threading.Event()
        self.last_run = None
        self.status = "idle"
        self.summary = ""
        self._last_poll = 0.0
        self._last_ok = None

    # -- Orchestrator duck-type ----------------------------------------
    def info(self):
        return {"name": self.name, "interval": self.interval,
                "last_run": self.last_run, "status": self.status,
                "summary": self.summary}

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.status = "running"
                self.cycle()
                self.status = "ok"
            except Exception as e:
                self.status = f"error: {e}"
                self.bus.log(self.name, f"⚠ {e}")
            self.last_run = datetime.now(IST).strftime("%H:%M:%S")
            self.stop_evt.wait(self.interval)
        self.status = "stopped"

    # -- the work ------------------------------------------------------
    def cycle(self):
        cfg = config.load()
        if not cfg.get("marketsense_enabled", True):
            self.summary = "disabled in settings"
            return
        poll_sec = int(cfg.get("marketsense_poll_sec", 300))
        if time.time() - self._last_poll < poll_sec:
            return  # not due yet; keep the cheap loop cheap
        self._last_poll = time.time()
        base = cfg.get("marketsense_url", "http://127.0.0.1:8100")

        try:
            pulse = _get_json(base, "/api/pulse?hours=24&min_materiality=7&limit=40")
            signals = _get_json(base, "/api/signals?limit=150")
        except (urllib.error.URLError, OSError, ValueError) as e:
            stale_min = (int((time.time() - self._last_ok) / 60)
                         if self._last_ok else None)
            self.summary = (f"MarketSense unreachable"
                            + (f" (data {stale_min}m stale)" if stale_min else
                               " (no data yet)"))
            self.bus.set("ms_link", {"ok": False, "error": str(e)[:120],
                                     "stale_min": stale_min})
            return  # keep whatever was on the bus; display marks staleness

        self._last_ok = time.time()
        self.bus.set("ms_events", pulse)
        for ev in pulse:
            sym = ev.get("symbol")
            if sym:
                self.bus.set(f"ms_event_flag:{sym}", ev)

        watchlist = [s for s in signals
                     if s.get("stance") == "buy"
                     and (s.get("conviction") or 0) >= 70]
        self.bus.set("ms_watchlist", watchlist)
        n_risk = 0
        for s in signals:
            sym = s.get("symbol")
            if not sym:
                continue
            if s.get("risk_verdict") in ("hard_block", "penalty") or \
                    s.get("stance") == "suppressed":
                self.bus.set(f"ms_risk_flag:{sym}", {
                    "verdict": s.get("risk_verdict"),
                    "stance": s.get("stance"),
                    "at": s.get("as_of")})
                n_risk += 1
            if s.get("entry") and s.get("invalidation") is not None:
                self.bus.set(f"ms_levels:{sym}", {
                    "entry": s.get("entry"), "target": s.get("target"),
                    "stop": s.get("invalidation"),
                    "profile": s.get("profile"), "at": s.get("as_of")})
        self.bus.set("ms_link", {"ok": True, "events": len(pulse),
                                 "watchlist": len(watchlist),
                                 "risk_flags": n_risk,
                                 "at": datetime.now(IST).strftime("%H:%M:%S")})
        self.summary = (f"{len(pulse)} events, {len(watchlist)} watchlist, "
                        f"{n_risk} risk flags")
