"""
Multi-agent trading system.

Agents (each runs on its own thread & cadence, communicating through a
shared Bus with a blackboard state store and pub/sub topics):

  market_data    every 3s      chain snapshots (Dhan rate limit = 1 req/3s;
                               upgrade path: Dhan WebSocket for true ticks)
  technical      every 60s     analyzer (OI walls, PCR, risk zones, bias)
  news           every 10min   RSS headlines -> Claude sentiment/risk flags
  social         every 10min   public feeds (Reddit RSS) -> retail mood
  fundamental    daily 8:45    macro/context brief for the day
  strategy       on analysis   builds trade signal (AI + full context)
  risk           on signal     pre-order gate: approves or rejects
  execution      on approval   places orders (paper/live), monitors, exits
  learning       EOD 15:35     reviews the day's trades, writes journal

Message flow:
  strategy -> topic 'signal'   -> risk
  risk     -> topic 'approved' -> execution
  execution-> topic 'closed'   -> learning (journal)

All timings are IST-aware; strategy/risk/execution stand down outside
market hours (09:15-15:30 Mon-Fri).
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta, timezone

import config
from analyzer import analyze, ai_signal, ai_budget_status as _ai_budget

try:
    import dhan_ws
except Exception as _e:
    dhan_ws = None
    print(f"[agents] dhan_ws unavailable, websocket market data disabled: {_e}")

try:
    import dhan_scrip_master
except Exception as _e:
    dhan_scrip_master = None
    print(f"[agents] dhan_scrip_master unavailable, futures OI disabled: {_e}")

try:
    from news_macro_agent import NewsMacroAgent
except Exception as _e:
    # Optional feature module — degrade loudly (printed at import time,
    # not silently swallowed) rather than crashing the whole app if the
    # global-macro data module has an issue.
    NewsMacroAgent = None
    print(f"[agents] news_macro_agent unavailable, NewsMacroAgent disabled: {_e}")

IST = timezone(timedelta(hours=5, minutes=30))
BASE = os.path.dirname(os.path.abspath(__file__))
# Persist trade history + logs in the user's home dir so a code-folder
# update / re-zip never wipes them.
STORE_DIR = os.path.expanduser("~/.ltp-monitor")
os.makedirs(STORE_DIR, exist_ok=True)
JOURNAL = os.path.join(STORE_DIR, "journal.json")
TRADES_FILE = os.path.join(STORE_DIR, "trades.jsonl")   # append-only, one JSON per line
OPEN_STATE_FILE = os.path.join(STORE_DIR, "open_state.json")  # snapshot of open positions+spreads
LOG_FILE = os.path.join(STORE_DIR, "activity.log")


def _save_open_state(positions: dict, spreads: dict):
    """Snapshot currently-open positions/spreads to disk so a restart
    (e.g. to apply an update) doesn't silently lose track of them —
    unlike trades.jsonl (append-only, closed trades only), this is a
    full overwrite since open state mutates constantly. Written
    atomically (tmp file + rename) so a crash mid-write can't corrupt
    it and lose everything."""
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        tmp = OPEN_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"positions": positions, "spreads": spreads,
                      "saved_at": now_ist().strftime("%Y-%m-%d %H:%M:%S")},
                     f, default=str)
        os.replace(tmp, OPEN_STATE_FILE)
    except Exception as e:
        print(f"[persist] failed to save open state: {e}")


def load_open_state():
    """Restore open positions/spreads on startup. Returns (positions,
    spreads), both {} if no snapshot exists or it's corrupt — a
    corrupt/missing snapshot should never crash startup, just start
    with no known open trades (same as before this feature existed)."""
    if not os.path.exists(OPEN_STATE_FILE):
        return {}, {}
    try:
        with open(OPEN_STATE_FILE) as f:
            d = json.load(f)
        return d.get("positions", {}) or {}, d.get("spreads", {}) or {}
    except Exception as e:
        print(f"[persist] failed to load open state: {e}")
        return {}, {}


def _append_trade(trade: dict):
    """Append a closed trade to the persistent log. Never overwrites."""
    try:
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        print(f"[persist] failed to write trade: {e}")


def load_persisted_trades():
    """Load all historical closed trades from disk on startup."""
    if not os.path.exists(TRADES_FILE):
        return []
    out = []
    try:
        with open(TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"[persist] failed to load trades: {e}")
    return out


def _append_activity(line: str):
    """Append a single activity-log line to disk (best-effort)."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def now_ist():
    return datetime.now(IST)


def market_open():
    t = now_ist()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return 9 * 60 + 15 <= hm <= 15 * 60 + 30


class ClaudeAuthError(Exception):
    """LLM auth error (online key invalid) — distinct from transient errors."""


def claude(prompt, api_key, max_tokens=500):
    """Now routes through the unified local/online LLM layer (Ollama by
    default). api_key kept for signature compatibility."""
    import llm
    text, engine, err = llm.generate_json(prompt, max_tokens)
    if err:
        if "invalid" in err or "401" in err:
            raise ClaudeAuthError(err)
        raise RuntimeError(err)
    return text


# ================================================================== bus

class Bus:
    """Blackboard + pub/sub. Agents write state and publish events."""

    def __init__(self):
        self.state = {}                      # shared blackboard
        self._subs = {}                      # topic -> [callback]
        self._lock = threading.Lock()
        self.feed = deque(maxlen=400)        # global activity feed
        self.alerts = deque(maxlen=100)      # high-priority alert stream

    def set(self, key, value):
        with self._lock:
            self.state[key] = value
            if key in ("positions", "spreads"):
                _save_open_state(self.state.get("positions", {}) or {},
                                 self.state.get("spreads", {}) or {})

    def get(self, key, default=None):
        with self._lock:
            return self.state.get(key, default)

    def subscribe(self, topic, cb):
        self._subs.setdefault(topic, []).append(cb)

    def publish(self, topic, msg):
        for cb in self._subs.get(topic, []):
            try:
                cb(msg)
            except Exception as e:
                self.log("bus", f"⚠ handler error on {topic}: {e}")

    def log(self, agent, msg):
        line = f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S')}] [{agent}] {msg}"
        self.feed.append(line)
        _append_activity(line)
        print("  " + line)

    def alert(self, severity, category, symbol, message):
        """severity: 'high' | 'medium' | 'low'. Pushed to the alert stream
        (dashboard banner/bell) and the activity log."""
        a = {"id": f"{time.time():.6f}", "ts": now_ist().strftime("%H:%M:%S"),
             "severity": severity, "category": category, "symbol": symbol,
             "message": message}
        self.alerts.append(a)
        self.log(category, f"🔔[{severity.upper()}] {symbol}: {message}")
        return a


# ================================================================== base

class Agent(threading.Thread):
    name = "agent"
    interval = 60

    def __init__(self, bus: Bus, ctx: dict):
        super().__init__(daemon=True)
        self.bus, self.ctx = bus, ctx
        self.stop_evt = threading.Event()
        self.last_run = None
        self.status = "idle"
        self.summary = ""

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.status = "running"
                self.cycle()
                self.status = "ok"
            except Exception as e:
                self.status = f"error: {e}"
                self.bus.log(self.name, f"⚠ {e}")
            self.last_run = now_ist().strftime("%H:%M:%S")
            self.stop_evt.wait(self.interval)
        self.status = "stopped"

    def cycle(self):
        raise NotImplementedError

    def info(self):
        return {"name": self.name, "interval": self.interval,
                "last_run": self.last_run, "status": self.status,
                "summary": self.summary}


# ================================================================== agents

class MarketDataAgent(Agent):
    name = "market_data"

    @property
    def interval(self):
        # Dhan's option-chain endpoint hard-limits to 1 request/3s.
        # Kotak's documented limit is 10 requests/second across ALL
        # APIs (confirmed in their official docs) — a full chain fetch
        # needs ~3-5 sequential calls (index quote + option batches,
        # occasionally +OI), each already spaced by the global rate
        # limiter in broker_adapter.py, so cycling faster here is safe
        # and meaningfully cuts the observed refresh lag.
        try:
            broker = config.load().get("broker", "dhan")
        except Exception:
            broker = "dhan"
        return 1.0 if broker == "kotak" else 3

    def cycle(self):
        syms = self.bus.get("symbols", ["NIFTY"])
        active = self.bus.get("active_symbol") or syms[0]
        # CRITICAL: a symbol with an open position must refresh every
        # cycle — never fall back to slow background rotation just because
        # the user is looking at a different tab. Stale price data on an
        # open trade is how profit turns into a missed stoploss.
        positions = self.bus.get("positions", {}) or {}
        open_syms = list(positions.keys())
        i = self.bus.get("_md_idx", 0)
        self.bus.set("_md_idx", i + 1)
        if open_syms:
            # cycle through open positions first (they need the freshest
            # data); only touch other symbols on the odd slot if there's
            # exactly one open position (spare bandwidth for the display)
            sym = open_syms[i % len(open_syms)]
            if len(open_syms) == 1 and i % 3 == 2:
                others = [s for s in syms if s not in open_syms] or syms
                sym = others[(i // 3) % len(others)] if active in open_syms else active
        elif i % 2 == 0 and active in syms:
            sym = active
        else:
            others = [s for s in syms if s != active] or syms
            sym = others[(i // 2) % len(others)]
        chain = None
        fail_until = self.bus.get(f"md_fail_until:{sym}", 0)
        if time.time() < fail_until:
            # this symbol is in a cooldown after repeated failures — skip
            # it silently this cycle rather than hammering a broken call
            self.summary = f"{sym} backing off (see earlier error) · cycling {len(syms)} indices"
            return
        try:
            chain = self.ctx["get_chain"](sym)
        except Exception as e:
            fails = self.bus.get(f"md_fails:{sym}", 0) + 1
            self.bus.set(f"md_fails:{sym}", fails)
            if fails <= 1 or fails % 20 == 0:
                self.bus.log(self.name, f"{sym}: {e}")
            if "429" in str(e) or "Too Many Requests" in str(e):
                # explicit rate-limit signal from the broker — back off
                # hard rather than the smaller generic backoff, and don't
                # let it grow unbounded (60s is already a long wait for a
                # single symbol's refresh)
                backoff = 60
            else:
                backoff = min(300, 10 * (2 ** min(fails, 5)))   # 20s.. capped at 5min
            self.bus.set(f"md_fail_until:{sym}", time.time() + backoff)
            self.summary = f"{sym} fetch failed ({fails}x) — backing off {backoff}s"
            return
        self.bus.set(f"md_fails:{sym}", 0)
        self.bus.set(f"chain:{sym}", chain)
        self.bus.set(f"chain_ts:{sym}", time.time())
        self.bus.set("chain_ts", time.time())
        self._sync_ws_feed(sym, chain)
        # spot history for intraday momentum (no extra API calls)
        hist = self.bus.get(f"spot_hist:{sym}", [])
        if chain.get("spot"):
            hist.append((time.time(), chain["spot"]))
            self.bus.set(f"spot_hist:{sym}", hist[-800:])
        # live ticker entry (prev_close filled by app-side cache, but
        # some brokers — Kotak — return change/% directly on the quote,
        # which is used as a fallback when candle-derived prev_close
        # isn't available for that broker)
        tick = self.bus.get("ticker", {})
        tick[sym] = {"spot": chain.get("spot"),
                     "ts": now_ist().strftime("%H:%M:%S"),
                     "chg": chain.get("chg"), "chg_pct": chain.get("chg_pct")}
        self.bus.set("ticker", tick)
        self.summary = f"{sym} {chain.get('spot')} · cycling {len(syms)} indices"

    # ---------------------------------------------------------- hybrid feed
    # HYBRID DESIGN (2026-07-24, validated live 2026-07-23 against a real
    # Dhan account — see ROADMAP.md and dhan_ws.py's module docstring for
    # the full design review of what was and wasn't built and why):
    #
    # REST (above) stays the ONLY source of chain SHAPE — which strikes
    # exist, IV, greeks — the websocket feed has none of that. What it
    # adds is faster, in-between-REST-poll freshness for the fields it
    # DOES carry: spot (index Ticker packets) and LTP/OI/depth (option
    # Full packets), merged onto the same REST-fetched chain dict via
    # dhan_ws.merge_tick_into_chain() rather than replacing any of it.
    # This is deliberately additive: if `market_data_feed` is anything
    # other than "websocket" (the default is "rest"), none of this runs
    # and MarketDataAgent behaves exactly as it always has.
    def _sync_ws_feed(self, sym, chain):
        cfg = config.load()
        if cfg.get("market_data_feed", "rest") != "websocket":
            return
        if dhan_ws is None or cfg.get("broker", "dhan") != "dhan":
            return
        client = self._ensure_ws_client(cfg)
        if client is None:
            return
        self._ensure_futures_subscribed(client)
        subscribed = getattr(self, "_ws_subscribed_legs", None)
        if subscribed is None:
            subscribed = set()
            self._ws_subscribed_legs = subscribed
        # As REST discovers new strikes over time (spot drifting to a new
        # ATM zone, expiry roll, etc.), grow the websocket subscription to
        # match — REST is still what decided these strikes exist at all.
        for row in chain.get("rows", []):
            for leg_key in ("ce", "pe"):
                leg = row.get(leg_key) or {}
                sec_id = leg.get("security_id")
                if not sec_id or sec_id in subscribed:
                    continue
                try:
                    if client.subscribe_more(sym, sec_id):
                        subscribed.add(sec_id)
                    # else: connection not up yet (or bad segment) —
                    # deliberately NOT added to `subscribed`, so the next
                    # cycle (this method runs every ~3s) retries it rather
                    # than silently losing it forever
                except Exception as e:
                    self.bus.log(self.name, f"ws subscribe_more failed for "
                                 f"{sym} {sec_id}: {e}")

    def _ensure_futures_subscribed(self, client):
        """Subscribe the current-month future per symbol via the scrip
        master lookup, re-checked once per trading day. Re-checking
        daily (rather than once ever) is what makes monthly rollover
        automatic: dhan_scrip_master.get_current_futures_detailed()
        always resolves to whichever contracts are nearest-unexpired —
        if the front one is a different security_id than yesterday's
        (the old one expired, a new month is now current), this picks
        it up with no code change or manual security-ID update needed.

        Extended 2026-07-25 per explicit request ("there are 2 more
        months - capture those as well"): now subscribes up to 3
        nearest-expiry contracts per symbol, not just the front month.
        The FRONT month keeps its exact existing role unchanged — it's
        still the only one driving future_oi_trend:{sym}/future_ohlc:
        {sym} (the live OI-buildup strategy signal and LTP Monitor
        panel), so nothing about the existing strategy pipeline
        changes. The 2nd/3rd months are additive: tracked separately
        under future_months:{sym} for cross-month OI/volume-wall
        analysis — captured now so that data has a lead time before
        any UI/strategy actually consumes it, same pattern already
        used for the candle-DB and volume-profile persistence work.

        Reuses subscribe_more() (the same method used for option legs)
        rather than add_future_instrument() — that method is designed
        for the pre-connection instrument list passed to start(), not
        for adding to an already-open connection. Futures use the same
        NSE_FNO/BSE_FNO Full-mode subscription shape as option legs, so
        subscribe_more() applies identically once the security_id is
        resolved here.
        """
        if dhan_scrip_master is None:
            return
        today = now_ist().strftime("%Y-%m-%d")
        checked = getattr(self, "_futures_checked_date", None)
        if checked is None:
            checked = {}
            self._futures_checked_date = checked
        future_map = getattr(self, "_future_sec_ids", None)
        if future_map is None:
            future_map = {}
            self._future_sec_ids = future_map
        # sec_id -> "front"/"month2"/"month3", so _on_ws_tick/_classify_
        # future_tick can tell the strategy-driving front-month contract
        # apart from the additive far-month ones without changing the
        # front-month code path at all.
        future_roles = getattr(self, "_future_roles", None)
        if future_roles is None:
            future_roles = {}
            self._future_roles = future_roles
        for sym in self.bus.get("symbols", []):
            if checked.get(sym) == today:
                continue
            try:
                futures, detail = dhan_scrip_master.get_current_futures_detailed(sym, n=3)
            except Exception as e:
                self.bus.log(self.name, f"futures lookup failed for {sym}: {e}")
                checked[sym] = today   # don't hammer this every 3s cycle today
                continue
            if not futures:
                self.bus.log(self.name, f"no current future found for {sym}: {detail}")
                checked[sym] = today
                continue
            all_subscribed = True
            newly_subscribed = []
            for i, future in enumerate(futures):
                role = "front" if i == 0 else f"month{i + 1}"
                sec_id = int(future["security_id"])
                if sec_id in future_map:
                    continue   # already subscribed to this exact contract
                if client.subscribe_more(sym, future["security_id"]):
                    future_map[sec_id] = sym
                    future_roles[sec_id] = role
                    newly_subscribed.append((role, future))
                else:
                    # connection not up yet — leave `checked` unset so
                    # the next cycle retries the whole symbol rather
                    # than silently giving up on futures for the day
                    all_subscribed = False
            for role, future in newly_subscribed:
                self.bus.log(self.name,
                            f"{sym} {role} future subscribed: "
                            f"{future.get('symbol_name', '?')} "
                            f"(security_id={future['security_id']}, "
                            f"expiry={future['expiry'].date()})")
            if all_subscribed:
                checked[sym] = today

    def _future_month_tick(self, sym, sec_id, tick):
        """Lightweight LTP/OI tracker for the 2nd/3rd month contracts
        (role != "front") — same tick data _classify_future_tick uses
        for the front month, but WITHOUT the buildup-classification/
        strategy-signal machinery, since that's specified against the
        front month only. Published to future_months:{sym} keyed by
        role, for cross-month OI/volume-wall analysis to consume later
        — data captured now, analysis not yet built (same "capture
        first, analyze later" pattern as the candle DB and volume-
        profile persistence work)."""
        ltp, oi = tick.get("ltp"), tick.get("oi")
        if ltp is None:
            return
        role = self._future_roles.get(sec_id, "?")
        months = getattr(self, "_future_months", None)
        if months is None:
            months = {}
            self._future_months = months
        sym_months = months.setdefault(sym, {})
        sym_months[role] = {"security_id": sec_id, "ltp": ltp, "oi": oi,
                            "volume": tick.get("volume")}
        self.bus.set(f"future_months:{sym}", sym_months)

    def _classify_future_tick(self, sym, tick):
        """Classify long/short buildup from a futures LTP+OI tick,
        comparing against a baseline captured at the first tick of
        each trading day — buildup is a session-level concept (has
        today's positioning grown net long or net short), not
        tick-to-tick noise, so a same-day baseline is the right
        reference point rather than the previous tick.

        Writes exactly "long" / "short" / None to
        future_oi_trend:{symbol} — matching mtf_confluence_strategy.
        evaluate()'s future_buildup parameter precisely (confirmed by
        reading its exact comparisons before writing this, not
        assumed). Only the STRICT textbook buildup quadrants map to a
        signal:
          price up + OI up   -> "long"  (long buildup, per rinkoo.docx)
          price down + OI up -> "short" (short buildup, per rinkoo.docx)
        The other two quadrants (short covering: price up + OI down;
        long unwinding: price down + OI down) are real, commonly-
        watched signals too, but are a WEAKER/different read than the
        strict "buildup" the doc specifically asks for — reported only
        on the richer diagnostic key (future_oi_quadrant), not fed into
        the strategy's future_buildup input, so the strategy only ever
        sees the exact signal it was specified against.
        """
        ltp, oi = tick.get("ltp"), tick.get("oi")
        if ltp is None or oi is None:
            return
        today = now_ist().strftime("%Y-%m-%d")
        baselines = getattr(self, "_future_baseline", None)
        if baselines is None:
            baselines = {}
            self._future_baseline = baselines
        b = baselines.get(sym)
        self._update_future_ohlc(sym, ltp, today)
        if not b or b.get("date") != today:
            baselines[sym] = {"date": today, "ltp": ltp, "oi": oi}
            self.bus.set(f"future_oi_trend:{sym}", None)
            self.bus.set(f"future_oi_quadrant:{sym}", None)
            return
        price_up, oi_up = ltp > b["ltp"], oi > b["oi"]
        if price_up and oi_up:
            quadrant, trend = "long_buildup", "long"
        elif not price_up and oi_up:
            quadrant, trend = "short_buildup", "short"
        elif price_up and not oi_up:
            quadrant, trend = "short_covering", None
        else:
            quadrant, trend = "long_unwinding", None
        self.bus.set(f"future_oi_trend:{sym}", trend)
        self.bus.set(f"future_oi_quadrant:{sym}", quadrant)
        self.bus.set(f"future_tick:{sym}",
                     {"ltp": ltp, "oi": oi, "baseline_ltp": b["ltp"],
                      "baseline_oi": b["oi"]})

    def _update_future_ohlc(self, sym, ltp, today):
        """Session OHLC + a VWAP proxy for the future, extending the
        existing futures tick pipeline (LTP Monitor enhancement,
        feature #1) — reuses the exact same tick data
        _classify_future_tick already receives, no new subscription.

        VWAP proxy, not a true volume-weighted average: Dhan's Full
        packet exposes cumulative session volume, not a clean per-tick
        trade-size delta, so a mathematically correct VWAP would need
        extra reconstruction with real risk of getting it subtly wrong.
        This uses a running mean of LTP across ticks (a TWAP) instead —
        same honest tradeoff already documented and accepted elsewhere
        in this codebase for the spot side (AnchorPullback's "session
        anchor" is explicitly a TWAP proxy for the same reason: no
        clean per-trade volume signal available). Labeled "vwap" in the
        API response for trader-familiar terminology, but this
        docstring and the roadmap entry are explicit about what it
        actually is.
        """
        ohlc = getattr(self, "_future_ohlc", None)
        if ohlc is None:
            ohlc = {}
            self._future_ohlc = ohlc
        o = ohlc.get(sym)
        if not o or o.get("date") != today:
            ohlc[sym] = {"date": today, "open": ltp, "high": ltp, "low": ltp,
                        "close": ltp, "vwap_sum": ltp, "vwap_n": 1,
                        "vwap": ltp}
            self.bus.set(f"future_ohlc:{sym}", ohlc[sym])
            return
        o["high"] = max(o["high"], ltp)
        o["low"] = min(o["low"], ltp)
        o["close"] = ltp
        o["vwap_sum"] += ltp
        o["vwap_n"] += 1
        o["vwap"] = round(o["vwap_sum"] / o["vwap_n"], 2)
        self.bus.set(f"future_ohlc:{sym}", o)

    def _ensure_ws_client(self, cfg):
        """Lazy singleton — created once, reused across cycles/symbols.
        Cooldown on repeated failure so a bad token doesn't retry every
        3s forever."""
        client = getattr(self, "_ws_client", None)
        if client is not None:
            return client
        fail_until = getattr(self, "_ws_fail_until", 0)
        if time.time() < fail_until:
            return None
        client_id = cfg.get("dhan_client_id")
        access_token = cfg.get("dhan_access_token")
        if not client_id or not access_token:
            self._ws_fail_until = time.time() + 60
            return None
        try:
            client = dhan_ws.DhanWebsocketClient(
                client_id, access_token,
                on_tick=self._on_ws_tick,
                on_status=lambda m: self.bus.log(self.name, f"ws: {m}"))
            for sym in self.bus.get("symbols", []):
                if sym.upper() in dhan_ws.INDEX_SECURITY_ID:
                    client.add_index_instrument(sym)
            client.start()
            self._ws_client = client
            self._ws_subscribed_legs = set()
            self.bus.log(self.name, "websocket market-data feed started "
                         "(hybrid mode: REST for chain shape/greeks, "
                         "websocket overlay for live LTP/OI/spot)")
            return client
        except Exception as e:
            self.bus.log(self.name, f"⚠ websocket feed failed to start: {e} "
                         f"— falling back to REST-only for 5 min")
            self._ws_fail_until = time.time() + 300
            return None

    def _on_ws_tick(self, sym, sec_id, tick):
        """Callback from the websocket client — runs on ITS thread, not
        this agent's cycle thread, so this must be safe to call anytime.
        Bus.set/get are already lock-protected (see Bus class)."""
        try:
            index_ids = {int(v) for v in dhan_ws.INDEX_SECURITY_ID.values()}
            if sec_id in index_ids:
                # index tick: spot-only update, no OI/depth to merge
                chain = self.bus.get(f"chain:{sym}")
                if not chain:
                    return   # REST hasn't fetched this symbol yet — wait for it
                chain["spot"] = tick["ltp"]
                self.bus.set(f"chain:{sym}", chain)
                tk = self.bus.get("ticker", {})
                if sym in tk:
                    tk[sym]["spot"] = tick["ltp"]
                    self.bus.set("ticker", tk)
                self._build_candle(sym, tick["ltp"])
                return
            future_map = getattr(self, "_future_sec_ids", {})
            if sec_id in future_map:
                role = getattr(self, "_future_roles", {}).get(sec_id, "front")
                if role == "front":
                    self._classify_future_tick(sym, tick)
                else:
                    self._future_month_tick(sym, sec_id, tick)
                return
            chain = self.bus.get(f"chain:{sym}")
            if not chain:
                return   # REST hasn't fetched this symbol's chain yet
            updated = dhan_ws.merge_tick_into_chain(chain, sec_id, tick)
            if updated:
                self.bus.set(f"chain:{sym}", chain)
        except Exception as e:
            self.bus.log(self.name, f"⚠ ws tick merge error: {e}")

    def _build_candle(self, sym, ltp):
        """Candle Builder Service (per the requested architecture:
        DhanHQ WS -> Market Data Service -> Candle Builder -> FastAPI
        WebSocket -> Lightweight Charts). Aggregates live spot ticks
        (already flowing through the existing websocket hybrid feed —
        no new subscription) into 1-minute candles.

        Publishes the CURRENTLY-FORMING candle to live_candle:{symbol}
        on every tick (what the WebSocket server pushes for a live-
        updating chart), and persists each COMPLETED minute to
        history.py's existing candles table (security_id convention
        "{symbol}_SPOT_1m", reusing the schema built for option-leg
        candles rather than adding a parallel table) the moment a new
        minute begins.
        """
        import history
        now = time.time()
        minute = int(now // 60) * 60
        tracker = getattr(self, "_candle_1m", None)
        if tracker is None:
            tracker = {}
            self._candle_1m = tracker
        cur = tracker.get(sym)
        if not cur or cur["minute"] != minute:
            if cur:
                try:
                    history.upsert_candles(f"{sym}_SPOT_1m", [{
                        "ts": cur["minute"], "o": cur["open"], "h": cur["high"],
                        "l": cur["low"], "c": cur["close"], "v": None, "oi": None}])
                except Exception as e:
                    self.bus.log(self.name, f"⚠ candle persist failed for {sym}: {e}")
            cur = {"minute": minute, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
            tracker[sym] = cur
        else:
            cur["high"] = max(cur["high"], ltp)
            cur["low"] = min(cur["low"], ltp)
            cur["close"] = ltp
        self.bus.set(f"live_candle:{sym}",
                     {"time": cur["minute"], "open": cur["open"],
                      "high": cur["high"], "low": cur["low"],
                      "close": cur["close"]})


class RegimeAgent(Agent):
    """Classifies today's market regime (trending / rangebound / choppy /
    gap-and-fade) and checks multi-timeframe alignment.

    Purpose: block bad trades before they happen. Last Monday's four
    losing BUY_CE trades all happened in a choppy/mean-reverting regime
    where buying calls is a slow bleed. This agent flags that.

    Publishes bus state 'regime:<sym>' consumed by the risk agent."""
    name = "regime"
    interval = 90     # candles don't move that fast; every 90s is plenty

    def cycle(self):
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            self.summary = "no broker client — set Dhan token"
            return
        if not market_open():
            # regime is meaningless outside market hours; report clearly
            self.summary = "market closed — regime idle"
            return
        syms = self.bus.get("symbols", ["NIFTY"])
        # compute regime for ALL symbols so switching tickers / trading any
        # index has fresh regime data (candle API is separate from the
        # option-chain rate limit; 12 light calls per 90s is fine)
        targets = syms
        done = []
        for sym in targets:
            if time.time() < self.bus.get(f"regime_fail_until:{sym}", 0):
                done.append(f"{sym[:4]}:skipped(see log)")
                continue
            try:
                r = self._classify(sym, d)
            except Exception as e:
                fails = self.bus.get(f"regime_fails:{sym}", 0) + 1
                self.bus.set(f"regime_fails:{sym}", fails)
                if fails <= 1 or fails % 10 == 0:
                    self.bus.log(self.name, f"{sym}: skipped ({e})")
                # 10 min backoff after repeated identical failures — this
                # is almost always a broker capability gap (e.g. Kotak has
                # no candle endpoint), not a transient blip worth retrying
                # every 90 seconds forever
                backoff = 600 if fails >= 3 else 90
                self.bus.set(f"regime_fail_until:{sym}", time.time() + backoff)
                done.append(f"{sym[:4]}:error")
                continue
            self.bus.set(f"regime_fails:{sym}", 0)
            if r:
                self.bus.set(f"regime:{sym}", r)
                done.append(f"{sym[:4]}:{r['regime'][:6]}")
                self._compute_bias(sym, r)
                self._compute_levels(sym)
            else:
                done.append(f"{sym[:4]}:warmup")
        self.summary = " · ".join(done) or "waiting for candles (needs ~15m after open)"

    def _compute_bias(self, sym, regime):
        """Feature #2 (AI Market Bias) — extends this agent rather than
        adding a new one, since it already runs every 90s with fresh
        regime/candle data. Every input is read from bus keys other
        parts of this system already populate; only Supertrend/
        Ichimoku (inside market_bias.py) are newly computed here."""
        import market_bias as mb
        future_ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
        future_chg_pct = None
        if future_ohlc.get("close") and future_ohlc.get("open"):
            future_chg_pct = round(
                (future_ohlc["close"] - future_ohlc["open"])
                / future_ohlc["open"] * 100, 2)
        analysis = self.bus.get(f"analysis:{sym}") or {}
        result = mb.compute_bias(
            spot_chg_pct=regime.get("session_change_pct"),
            future_chg_pct=future_chg_pct,
            daily_candles=self.bus.get(f"regime_candles:{sym}"),
            regime=regime,
            oi_bias_pcr=analysis.get("pcr_oi"),
            future_trend=self.bus.get(f"future_oi_trend:{sym}"),
            vix=self.bus.get("india_vix"),
            global_sentiment=self.bus.get("global_risk_sentiment"),
        )
        self.bus.set(f"bias:{sym}", result)

    def _compute_levels(self, sym):
        """Feature #3 (Support/Resistance) — extends this agent the
        same way _compute_bias does: reuses regime_candles (no new API
        call) for previous-day levels, and analyzer.py's existing
        signal_lines (OI walls, retained as the primary S/R source,
        not recomputed) for the options-chain side."""
        import support_resistance as sr
        analysis = self.bus.get(f"analysis:{sym}") or {}
        chain = self.bus.get(f"chain:{sym}") or {}
        spot = chain.get("spot")
        if spot is None:
            return
        candles = self.bus.get(f"regime_candles:{sym}")
        prev_day = sr.previous_day_levels(candles, symbol=sym)
        self._persist_daily_ohlc(sym, spot)
        spot_ohlc_vwap = None  # spot VWAP is derived at the API layer
                               # from spot_hist, not tracked per-agent —
                               # merge_levels() below uses futures VWAP
                               # only, since that's what this agent has
                               # direct access to; the API layer's own
                               # response already carries spot VWAP
                               # separately for display.
        future_ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
        levels = sr.merge_levels(analysis.get("signal_lines"), prev_day,
                                 future_ohlc.get("vwap"), spot)
        self.bus.set(f"levels:{sym}", levels)

    def _persist_daily_ohlc(self, sym, spot):
        """Tracks today's running O/H/L/C in memory (same per-day-reset
        pattern already used for futures OHLC and OI baselines) and
        upserts to history.py's daily_ohlc table on every call —
        idempotent (REPLACE on the symbol+date key), so this just
        keeps today's row current as the session progresses. This is
        what makes today automatically become tomorrow's persisted
        "previous day" — no separate end-of-day job needed."""
        if spot is None:
            return
        import history
        today = now_ist().strftime("%Y-%m-%d")
        tracker = getattr(self, "_daily_ohlc_tracker", None)
        if tracker is None:
            tracker = {}
            self._daily_ohlc_tracker = tracker
        t = tracker.get(sym)
        if not t or t.get("date") != today:
            tracker[sym] = {"date": today, "open": spot, "high": spot,
                           "low": spot, "close": spot}
        else:
            t["high"] = max(t["high"], spot)
            t["low"] = min(t["low"], spot)
            t["close"] = spot
        t = tracker[sym]
        try:
            history.upsert_daily_ohlc(sym, today, t["open"], t["high"],
                                      t["low"], t["close"])
        except Exception as e:
            # DB write failing shouldn't take down regime/bias/levels
            # computation for this cycle — log once, not every 90s.
            if not getattr(self, "_daily_ohlc_write_failed", False):
                self._daily_ohlc_write_failed = True
                self.bus.log(self.name, f"⚠ failed to persist daily OHLC "
                                        f"for {sym}: {e}")

    def _fetch_candles(self, d, sym, tf):
        """Dhan's intraday endpoint is rate-limited — pace every call."""
        time.sleep(1.2)
        return d.intraday(sym, tf)["candles"]

    def _persist_candles(self, sym, c1, c5, c15):
        """Writes 1m/5m/15m candles to history.py's SQLite store for
        this symbol. Fail loud, not silent (project convention) —
        logged once per symbol rather than crashing the regime cycle
        or being swallowed, since a DB hiccup here shouldn't take down
        regime/bias/levels computation for the other symbols."""
        import history
        try:
            history.upsert_index_candles(sym, c1, 1)
            history.upsert_index_candles(sym, c5, 5)
            history.upsert_index_candles(sym, c15, 15)
        except Exception as e:
            key = f"_candle_persist_failed_{sym}"
            if not getattr(self, key, False):
                setattr(self, key, True)
                self.bus.log(self.name, f"⚠ failed to persist candles "
                                        f"for {sym}: {e}")

    def _today_only(self, candles):
        """Filter to candles whose timestamp falls on today's IST calendar
        date. Dhan's intraday fetch deliberately spans ~3 prior trading
        days (needed so ADX/ATR have enough bars to warm up early in the
        session) — but that means the FRONT of the array is NOT today's
        open. Anything computed as "today's opening range" or "today's
        session move" off the raw array is silently reading a blend of
        today and 1-2 prior days."""
        today = now_ist().strftime("%Y-%m-%d")
        out = []
        for c in candles:
            t = c.get("time")
            if t is None:
                continue
            d_str = datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")
            if d_str == today:
                out.append(c)
        return out

    def _classify(self, sym, d):
        """Compute the regime label + multi-timeframe alignment.
        All from OHLC — no LLM, no extra API calls beyond candles."""
        # Fetch candles at three timeframes. Dhan intraday deliberately
        # returns ~3 prior trading days too (needed so ADX/ATR have
        # enough bars to warm up early in the session) — kept as-is for
        # those indicators. But "today's opening range" and "today's
        # session move" must NOT be computed off that raw multi-day
        # array, or the front of it (still-warming candles from
        # yesterday or before) gets silently read as today's open.
        c5 = self._fetch_candles(d, sym, "5")
        # Separate bus key (not bloating regime:{sym}, which many
        # consumers/API responses read and shouldn't have to carry a
        # large candle array) — Feature #2 (AI Market Bias) reuses
        # this same fetch for MACD/RSI/Supertrend/Ichimoku, no new
        # API call needed.
        self.bus.set(f"regime_candles:{sym}", c5)
        self.bus.set(f"pa_candles:{sym}", None)  # cleared; set correctly below
        c15 = self._fetch_candles(d, sym, "15")
        c1 = self._fetch_candles(d, sym, "1")
        # Persist all three timeframes for ALL symbols on every cycle
        # ("store the candles in local db for further use and
        # analysis, now onwards" — 2026-07-25). Reuses this exact
        # fetch, no new API calls. Deliberately persisted before the
        # warmup-length early-return below, so even a symbol still
        # warming up gets its candles captured rather than discarded.
        self._persist_candles(sym, c1, c5, c15)
        if len(c5) < 20 or len(c15) < 8 or len(c1) < 15:
            return None


        c5_today = self._today_only(c5)
        c1_today = self._today_only(c1)
        c15_today = self._today_only(c15)
        if len(c5_today) < 3:
            # not enough of TODAY's session yet for a meaningful opening
            # range/session read, even though multi-day history exists
            return None

        # pa_strategies.evaluate() explicitly requires "today's session
        # candles, oldest first" for ORB's opening-range window, the
        # VWAP-proxy anchor's cumulative mean, and EMA-MTF's cross
        # timing — all three assume index 0 is today's 9:15 open. Feeding
        # it the raw multi-day array (2-3 prior trading days blended in)
        # silently broke every one of those assumptions: the "opening
        # range" was really some prior day's mid-session candles, and the
        # anchor was a multi-day cumulative average, not today's. This is
        # the likely reason ORB/vwap_pullback/ema_mtf — all clearly
        # profitable in backtest, which correctly replays session-only
        # candles — never fired live.
        self.bus.set(f"pa_candles:{sym}", {"c1": c1_today, "c5": c5_today,
                                           "c15": c15_today, "ts": time.time()})
        # ---- Regime classification (based on 5m candles for today) ----
        # ATR (14) on 5m: proxy for volatility per bar — uses the full
        # multi-day series on purpose, ADX/ATR genuinely need the history
        atr14 = self._atr(c5, 14)
        # ADX (14) on 5m: trend strength — same, multi-day warmup is correct here
        adx14 = self._adx(c5, 14)
        # Opening range (first 15 min = first 3 x 5m candles) — TODAY only
        or_hi = max(c["high"] for c in c5_today[:3])
        or_lo = min(c["low"] for c in c5_today[:3])
        or_range = or_hi - or_lo
        curr = c5_today[-1]["close"]

        session_hi = max(c["high"] for c in c5_today)
        session_lo = min(c["low"] for c in c5_today)
        session_range = session_hi - session_lo

        # Where is price relative to opening range?
        or_position = ("above" if curr > or_hi else
                       "below" if curr < or_lo else "inside")
        # How much has the market travelled beyond the OR?
        or_expansion = ((session_range / or_range) if or_range > 0 else 1.0)

        # Directional bias from close-to-close over the session
        first_close = c5_today[0]["close"]
        session_change_pct = (curr - first_close) / first_close * 100

        # Whipsaw: number of sign flips in 5m candle direction over last 20
        recent = c5_today[-20:]
        directions = [1 if c["close"] > c["open"] else -1 for c in recent]
        flips = sum(1 for i in range(1, len(directions))
                    if directions[i] != directions[i-1])

        # ---- Regime label ----
        # Strong ADX + directional move + expansion out of OR = trending
        # Weak ADX + many flips + tight range = choppy/rangebound
        # Fast reversal from an OR break = gap-and-fade
        atr_pct = (atr14 / curr) * 100 if curr else 0

        if adx14 >= 25 and or_position != "inside" and or_expansion >= 1.5:
            regime = "trending-up" if session_change_pct > 0 else "trending-down"
            confidence = min(95, 50 + adx14)
        elif adx14 < 18 and flips >= 10:
            regime = "choppy"
            confidence = 60 + min(30, flips * 2)
        elif or_expansion < 1.3:
            regime = "rangebound"
            confidence = 70
        elif or_position == "inside" and flips >= 7:
            # broke OR then came back — classic fade
            regime = "gap-and-fade"
            confidence = 65
        else:
            regime = "mixed"
            confidence = 40

        # ---- Multi-timeframe confluence ----
        # Simple: is the trend direction the same on 1m, 5m, 15m?
        # TODAY-ONLY slices — using the raw multi-day arrays here was the
        # actual bug: comparing today's early-session move against a
        # "first third" that was really yesterday's (or older) closing
        # levels guarantees a false "no-alignment" read no matter how
        # cleanly today itself is trending.
        #
        # Note found 2026-07-22: even with correct today-only data,
        # 1m/5m/15m are legitimately DIFFERENT time windows (last 15min /
        # 75min / 120min of momentum) — they can disagree often during
        # completely normal intraday chop (a bounce within a pullback
        # within a trend), which made "no-alignment" the dominant outcome
        # for hours at a stretch even on a clearly trending day. Adding
        # the regime's own session-level directional read (already
        # computed above from the whole day's ADX/OR-expansion, a much
        # more stable signal) as a 4th vote lets a clear session trend
        # break a short-term-noise tie instead of every signal getting
        # blocked by momentary disagreement between three short windows.
        tf_bias = {
            "1m": self._trend_bias(c1_today[-15:]),
            "5m": self._trend_bias(c5_today[-15:]),
            "15m": self._trend_bias(c15_today[-8:]),
        }
        regime_vote = ("bull" if regime == "trending-up" else
                       "bear" if regime == "trending-down" else None)
        votes = list(tf_bias.values()) + ([regime_vote] if regime_vote else [])
        bulls = sum(1 for v in votes if v == "bull")
        bears = sum(1 for v in votes if v == "bear")
        total_votes = len(votes)
        if bulls == total_votes and bulls >= 3:
            confluence = "strong-bull"
        elif bears == total_votes and bears >= 3:
            confluence = "strong-bear"
        elif bulls >= 2 and bulls > bears:
            confluence = "mixed-bull"
        elif bears >= 2 and bears > bulls:
            confluence = "mixed-bear"
        else:
            confluence = "no-alignment"

        # ---- Trade playbook based on regime ----
        # This is what the risk agent will use to gate signals.
        allowed = {
            "trending-up":    ["BUY_CE"],
            "trending-down":  ["BUY_PE"],
            "rangebound":     [],                 # avoid directional bets
            "choppy":         [],                 # premium bleeds in chop
            "gap-and-fade":   ["BUY_CE", "BUY_PE"],  # both possible; needs confluence
            "mixed":          ["BUY_CE", "BUY_PE"],
        }.get(regime, [])

        return {
            "regime": regime,
            "confidence": int(round(confidence)),
            "adx": round(adx14, 1),
            "atr_pct": round(atr_pct, 2),
            "or_high": or_hi,
            "or_low": or_lo,
            "or_position": or_position,
            "or_expansion": round(or_expansion, 2),
            "session_change_pct": round(session_change_pct, 2),
            "flips_20bar": flips,
            "tf_bias": tf_bias,
            "confluence": confluence,
            "allowed_signals": allowed,
            "computed_at": now_ist().strftime("%H:%M:%S"),
        }

    def _atr(self, candles, n=14):
        trs = []
        prev_close = candles[0]["close"]
        for c in candles[1:]:
            tr = max(c["high"] - c["low"],
                     abs(c["high"] - prev_close),
                     abs(c["low"] - prev_close))
            trs.append(tr)
            prev_close = c["close"]
        if len(trs) < n:
            return sum(trs) / max(len(trs), 1)
        # simple moving ATR (Wilder's smoothing not needed for this use)
        return sum(trs[-n:]) / n

    def _adx(self, candles, n=14):
        """Simplified ADX — trend strength 0-100. Good enough to
        distinguish trending vs rangebound without a full library."""
        if len(candles) < n + 2:
            return 0
        plus_dm = []
        minus_dm = []
        trs = []
        for i in range(1, len(candles)):
            up = candles[i]["high"] - candles[i-1]["high"]
            dn = candles[i-1]["low"] - candles[i]["low"]
            plus_dm.append(up if up > dn and up > 0 else 0)
            minus_dm.append(dn if dn > up and dn > 0 else 0)
            tr = max(candles[i]["high"] - candles[i]["low"],
                     abs(candles[i]["high"] - candles[i-1]["close"]),
                     abs(candles[i]["low"] - candles[i-1]["close"]))
            trs.append(tr)
        window = min(n, len(trs))
        atr = sum(trs[-window:]) / window
        if atr == 0:
            return 0
        pdi = 100 * (sum(plus_dm[-window:]) / window) / atr
        mdi = 100 * (sum(minus_dm[-window:]) / window) / atr
        if pdi + mdi == 0:
            return 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
        return dx

    def _trend_bias(self, candles):
        """Quick trend bias for one timeframe: compare last close to
        the middle-third average — resistant to a single-bar spike."""
        # Needs at least 3 candles: below that, `closes[:0]`/`closes[-0:]`
        # is a Python slicing gotcha (closes[-0:] is the WHOLE list, not
        # zero elements), which can produce a spurious "bull" read with
        # too little data to mean anything.
        if len(candles) < 3:
            return "flat"
        closes = [c["close"] for c in candles]
        first_third = sum(closes[:len(closes)//3]) / max(len(closes)//3, 1)
        last_third = sum(closes[-len(closes)//3:]) / max(len(closes)//3, 1)
        if last_third > first_third * 1.001:
            return "bull"
        if last_third < first_third * 0.999:
            return "bear"
        return "flat"


class TechnicalAgent(Agent):
    name = "technical"
    interval = 60

    def cycle(self):
        syms = self.bus.get("symbols", ["NIFTY"])
        done = []
        for sym in syms:
            chain = self.bus.get(f"chain:{sym}")
            if not chain:
                continue
            momentum = compute_momentum(self.bus.get(f"spot_hist:{sym}", []))
            indicators = self._indicators(sym)
            analysis = analyze(chain, momentum=momentum,
                               indicators=indicators)
            if analysis.get("error"):
                continue
            self.bus.set(f"analysis:{sym}", analysis)
            self._check_iv_spike(sym, analysis)
            done.append(f"{sym[:4]}:{analysis['bias'].split()[0][:4]}"
                        + (f"({momentum['trend'][:2]})" if momentum else ""))
            self.bus.publish("analysis", {"symbol": sym})
        self.summary = " · ".join(done) if done else "waiting for market data"

    def _check_iv_spike(self, sym, analysis):
        """Volatility alert: a fast rise in average IV often precedes/
        follows a macro surprise or big event — flag it with a price
        change so the person can react before the move finishes."""
        iv = analysis.get("avg_iv") or 0
        hist = self.bus.get(f"iv_hist:{sym}", [])
        hist.append((time.time(), iv, analysis.get("spot")))
        hist = hist[-400:]
        self.bus.set(f"iv_hist:{sym}", hist)
        if len(hist) < 6:
            return
        target = hist[-1][0] - 900       # ~15 min ago
        past = min(hist, key=lambda x: abs(x[0] - target))
        if abs(past[0] - target) > 600 or not past[1]:
            return
        iv_chg_pct = (iv - past[1]) / past[1] * 100
        last_alert = self.bus.get(f"iv_alert_ts:{sym}", 0)
        if iv_chg_pct >= 20 and time.time() - last_alert > 900:
            spot_chg = (analysis["spot"] - past[2]) if past[2] else 0
            self.bus.alert("high", "volatility", sym,
                           f"IV jumped {iv_chg_pct:.0f}% in ~15m (now "
                           f"{iv:.1f}%) alongside a spot move of "
                           f"{spot_chg:+.1f} — possible news/event-driven "
                           "volatility spike. Widen stops / reduce size.")
            self.bus.set(f"iv_alert_ts:{sym}", time.time())

    def _indicators(self, sym):
        """MACD + Stochastic from 5-minute candles (cached by the client)."""
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            return None
        try:
            candles = d.intraday(sym, "5")["candles"]
        except Exception:
            return None
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        if len(closes) < 35:
            return None

        def ema(vals, n):
            k = 2 / (n + 1)
            e = vals[0]
            out = []
            for v in vals:
                e = v * k + e * (1 - k)
                out.append(e)
            return out

        macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
        signal = ema(macd_line, 9)
        hist_v = macd_line[-1] - signal[-1]
        hh, ll = max(highs[-14:]), min(lows[-14:])
        k_val = (closes[-1] - ll) / (hh - ll) * 100 if hh > ll else 50

        def k_at(i):
            h, l = max(highs[i-13:i+1]), min(lows[i-13:i+1])
            return (closes[i] - l) / (h - l) * 100 if h > l else 50
        d_val = sum(k_at(len(closes)-1-j) for j in range(3)) / 3
        return {"macd_hist": round(hist_v, 2),
                "macd_positive": hist_v > 0,
                "stoch_k": round(k_val, 1), "stoch_d": round(d_val, 1),
                "stoch_zone": ("oversold" if k_val < 20 else
                               "overbought" if k_val > 80 else "neutral")}


def compute_momentum(hist):
    """Intraday momentum from the 3-second spot history."""
    if len(hist) < 5:
        return None
    now = hist[-1]
    spot = now[1]

    def pct_ago(seconds):
        target = now[0] - seconds
        past = min(hist, key=lambda x: abs(x[0] - target))
        if abs(past[0] - target) > seconds * 0.6:
            return None
        return round((spot - past[1]) / past[1] * 100, 3)

    p5, p15 = pct_ago(300), pct_ago(900)
    ref = p15 if p15 is not None else p5
    if ref is None:
        return None
    if ref > 0.12:
        trend = "rising"
    elif ref < -0.12:
        trend = "falling"
    else:
        trend = "flat"
    return {"pct_5m": p5, "pct_15m": p15, "trend": trend, "spot": spot}


class NewsAgent(Agent):
    name = "news"
    interval = 900

    def cycle(self):
        # 2026-07-24: was a single hardcoded Google-News RSS query,
        # scraped with a raw regex. Now pulls from every enabled feed
        # in news_engine's shared config (the user's confirmed Indian
        # sources plus global feeds), each headline categorized and
        # bias-scored, and logged into the SAME shared, deduplicated
        # tracker NewsMacroAgent writes into — this is the actual fix
        # for "picking similar information again and again": both
        # agents now draw from one shared, deduplicated pipeline
        # instead of two independent ones repeatedly processing the
        # same underlying stories.
        #
        # Everything below this point (the AI sentiment/risk_event
        # classification, the "news" bus key shape, the cooldown/
        # state-transition alerting) is UNCHANGED from before this
        # change — that logic is close to live risk-gating
        # (news_risk_opportunity() reads exactly this bus key) and was
        # deliberately left untouched beyond swapping the input source.
        import news_engine as ne
        events, errors = ne.fetch_all_enabled(max_items_per_feed=8)
        for err in errors:
            self.bus.log(self.name, f"⚠ feed fetch failed: {err['feed']} — {err['error']}")
        for evt in events:
            ne.log_tracked_event(evt)   # shared, deduped tracker for the dashboard
        # Bug found 2026-07-24: prune_tracker_file() existed but was
        # never actually called anywhere — retention was unbounded in
        # practice, confirmed live (1000+ accumulated entries). Runs at
        # most once/hour here, not every 15-min cycle, since it's a
        # full-file rewrite and doesn't need to run more often than that.
        if time.time() - getattr(self, "_last_prune", 0) > 3600:
            self._last_prune = time.time()
            ne.prune_tracker_file()
        heads = [e["description"] for e in events if e.get("valid")][:15]
        if not heads:
            self.summary = "no headlines fetched"
            return
        # Bug found 2026-07-22: the same handful of headlines kept
        # re-triggering "News risk event" alerts every cycle from ~3pm
        # onward. Root cause — the RSS feed wasn't actually returning
        # new content, but the LLM call was still re-run on the exact
        # same headlines every cycle, and an occasional parse/auth
        # failure would knock risk_event to False for one cycle, then
        # the next (identical-content) cycle flipped it back to True —
        # a false "not-active -> active" edge on stale data, firing a
        # duplicate alert with the same note. Fix: skip re-analysis
        # entirely when the headline set hasn't changed, and validate
        # that the feed is actually updating rather than silently
        # re-processing the same content.
        sig = hash(tuple(sorted(heads)))
        prev_sig = self.bus.get("news_headline_sig")
        if sig == prev_sig:
            stale_since = self.bus.get("news_stale_since") or time.time()
            self.bus.set("news_stale_since", stale_since)
            stale_minutes = (time.time() - stale_since) / 60
            prev = self.bus.get("news") or {}
            if stale_minutes >= 120 and not self.bus.get("news_stale_warned"):
                self.bus.log(self.name,
                             f"⚠ feed has returned identical headlines for "
                             f"{stale_minutes:.0f} min — not re-analyzing or "
                             f"re-alerting on unchanged content")
                self.bus.set("news_stale_warned", True)
            self.summary = (f"{prev.get('sentiment','neutral')} · "
                           f"risk_event={prev.get('risk_event', False)} "
                           f"(unchanged {stale_minutes:.0f}m)")
            return
        self.bus.set("news_headline_sig", sig)
        self.bus.set("news_stale_since", time.time())
        self.bus.set("news_stale_warned", False)
        if config.load().get("ai_engine", "local") != "off":
            try:
                out = claude(
                    "Market news headlines for Indian indices below. Reply "
                    "ONLY JSON: {\"sentiment\":\"bullish|bearish|neutral\","
                    "\"risk_event\":true|false,\"note\":\"<one line>\"}\n\n"
                    + "\n".join(heads), None, 200)
                j = json.loads(out.replace("```json", "").replace("```", "").strip())
            except ClaudeAuthError as e:
                j = {"sentiment": "neutral", "risk_event": False, "note": str(e)}
                self.bus.log(self.name, f"⚠ {e}")
            except Exception:
                j = {"sentiment": "neutral", "risk_event": False,
                     "note": "AI sentiment unavailable"}
        else:
            j = {"sentiment": "neutral", "risk_event": False,
                 "note": "headlines collected (AI off)"}
        j["headlines"] = heads[:8]
        # State-transition alerting: alert when a risk event first
        # appears, and only re-alert periodically (a cooldown) while it
        # remains ongoing — never on every cycle.
        #
        # Bug found 2026-07-22: comparing exact note TEXT (as a proxy for
        # "is this a new event") doesn't work — the LLM naturally rewords
        # a continuing, unchanged market condition slightly every single
        # cycle ("indices experienced significant declines across news
        # headlines" -> "have experienced significant declines in the
        # recent news headlines" -> "fell significantly due to
        # geopolitical tensions and oil prices" -- all the SAME ongoing
        # afternoon selloff, just reworded), so note_changed was true
        # almost every cycle and fired a fresh alert every 15-30 minutes
        # all afternoon. A cooldown is the only guard that doesn't
        # depend on the LLM's wording being stable.
        cfg = config.load()
        prev = self.bus.get("news") or {}
        was_active = bool(prev.get("risk_event"))
        now_active = bool(j.get("risk_event"))
        if now_active:
            # Bug found 2026-07-22: `(not was_active) or (cooldown expired)`
            # let risk_event's occasional flip to False — ordinary
            # classification noise across a headline change, not a real
            # resolution of the event — re-arm an IMMEDIATE alert next
            # cycle regardless of the cooldown. Observed cadence was
            # ~30min despite a 60min cooldown, exactly consistent with
            # every other 15min headline-change cycle tripping this.
            # The cooldown alone is the only guard that can't be
            # defeated by noise: `last_alert_ts` starts at 0, so the
            # very first alert still fires immediately.
            cooldown_sec = cfg.get("news_realert_cooldown_minutes", 60) * 60
            last_alert_ts = self.bus.get("news_last_alert_ts", 0)
            should_alert = (time.time() - last_alert_ts) >= cooldown_sec
            if should_alert:
                j["flagged_ts"] = time.time()
                self.bus.set("news_last_alert_ts", time.time())
                self.bus.alert("high", "news", "",
                               f"News risk event: {j.get('note','')}")
            else:
                # same ongoing event, still within the cooldown — keep the
                # original timestamp so risk agent's expiry window is
                # measured from when the event was first detected, not
                # re-armed every cycle just because the wording shifted
                j["flagged_ts"] = prev.get("flagged_ts", time.time())
        self.bus.set("news", j)
        self.summary = f"{j['sentiment']} · risk_event={j['risk_event']}"


class SocialAgent(Agent):
    name = "social"
    interval = 900

    FEEDS = ["https://www.reddit.com/r/IndianStreetBets/.rss",
             "https://www.reddit.com/r/IndianStockMarket/.rss"]

    def cycle(self):
        titles = []
        for url in self.FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ltp-monitor/1.0"})
                xml = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
                titles += re.findall(r"<title>(.*?)</title>", xml)[1:9]
            except Exception:
                continue
        titles = [t[:120] for t in titles][:12]
        if not titles:
            self.summary = "no social data (feeds unavailable)"
            self.bus.set("social", {"mood": "unknown", "posts": []})
            return
        engine_on = config.load().get("ai_engine","local") != "off"
        mood = "unknown"
        if engine_on:
            try:
                out = claude("Retail trader forum post titles (India). One "
                             "word only - overall mood: euphoric, bullish, "
                             "neutral, bearish, or fearful.\n\n" + "\n".join(titles),
                             None, 20)
                mood = out.strip().lower().split()[0]
            except Exception:
                pass
        self.bus.set("social", {"mood": mood, "posts": titles[:6]})
        self.summary = f"retail mood: {mood}"


class FundamentalAgent(Agent):
    name = "fundamental"
    interval = 3600        # checks hourly, produces once per day at ~08:45

    def cycle(self):
        today = now_ist().strftime("%Y-%m-%d")
        cur = self.bus.get("macro") or {}
        if cur.get("date") == today:
            self.summary = f"today's brief ready ({cur.get('stance','')})"
            return
        if now_ist().hour < 8:
            self.summary = "waiting for 08:45 IST"
            return
        news = self.bus.get("news") or {}
        engine_on = config.load().get("ai_engine","local") != "off"
        brief = {"date": today, "stance": "neutral",
                 "note": "no AI key — neutral macro assumption"}
        if engine_on:
            try:
                out = claude(
                    "Write a 3-line pre-market macro brief for Indian index "
                    "option traders for today. End with STANCE: bullish/"
                    "bearish/neutral. Recent headlines:\n"
                    + "\n".join(news.get("headlines", ["none"])), None, 300)
                stance = "neutral"
                m = re.search(r"STANCE:\s*(\w+)", out, re.I)
                if m:
                    stance = m.group(1).lower()
                brief = {"date": today, "stance": stance, "note": out.strip()}
            except Exception as e:
                brief["note"] = f"AI unavailable: {e}"
        self.bus.set("macro", brief)
        self.summary = f"daily brief: {brief['stance']}"


class StrategyAgent(Agent):
    name = "strategy"
    interval = 5           # event-driven; the loop only drains a queue

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._pending = deque()
        bus.subscribe("analysis", self._pending.append)
        self._last_signal_ts = 0
        self._recent_signals = deque(maxlen=6)   # (sym, signal, strike, ts)
        self._backoff_until = 0

    def cycle(self):
        if not self._pending:
            return
        jobs = {}
        while self._pending:                 # dedupe: latest per symbol
            m = self._pending.popleft()
            jobs[m["symbol"]] = m
        if not market_open():
            self.summary = "market closed — standing down"
            return
        cfg = config.load()
        max_pos = cfg.get("max_concurrent_positions", 1)
        positions = self.bus.get("positions", {}) or {}
        if len(positions) >= max_pos:
            self.summary = f"{len(positions)}/{max_pos} positions open — no new signals"
            return
        # never signal again on a symbol that already has an open position
        jobs = {s: j for s, j in jobs.items() if s not in positions}
        if not jobs:
            return
        if time.time() < self._backoff_until:
            self.summary = f"backing off after repeated rejects ({int(self._backoff_until-time.time())}s)"
            return
        if time.time() - self._last_signal_ts < 120:
            return                       # cooldown between signals
        # Check if risk-rejected signals repeat — if so, halt for a while.
        # This avoids the "same BUY_CE 24100 every 2 min for 30 min" spam.
        last_verdict = self.bus.get("last_risk_check") or {}
        if last_verdict.get("verdict") == "REJECTED":
            failed = [c for c in last_verdict.get("checks", []) if c.startswith("✗")]
            hard_reasons = ("daily loss limit", "halted", "cooldown",
                            "trades ", "market open")
            if any(any(hr in f for hr in hard_reasons) for f in failed):
                # 15-min backoff — the reason isn't going to change quickly
                self._backoff_until = time.time() + 900
                self.summary = "hard-reject reason present; 15-min backoff"
                self.bus.log(self.name, self.summary)
                return
        context = {
            "news": self.bus.get("news"),
            "social_mood": (self.bus.get("social") or {}).get("mood"),
            "macro": (self.bus.get("macro") or {}).get("stance"),
        }
        cfg = config.load()
        # Cost control: by default only run the (expensive) signal on the
        # symbol the user is actively watching. The AI gate additionally
        # caches and rate-limits, so this stays cheap.
        if cfg.get("ai_active_only", True):
            active = self.bus.get("active_symbol")
            jobs = {active: jobs[active]} if active in jobs else {}
        best = None
        for sym in jobs:
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis or analysis.get("error"):
                continue
            sig = ai_signal(analysis, context=context)
            self.bus.set(f"signal:{sym}", sig)
            if sig["signal"] != "WAIT" and \
               (best is None or sig["confidence"] > best[1]["confidence"]):
                best = (sym, sig, analysis)
        if best:
            sym, sig, analysis = best
            sig["symbol"] = sym
            self.bus.set("last_signal", sig)
            self._last_signal_ts = time.time()
            trade_params = (f"entry ₹{sig.get('entry')} SL ₹{sig.get('stoploss')} "
                           f"T1 ₹{sig.get('target1')} T2 ₹{sig.get('target2')}")
            self.summary = (f"{sym}: {sig['signal']} {sig.get('strike','')} "
                          f"conf {sig['confidence']}% — {trade_params}")
            self.bus.log(self.name, self.summary)
            self.bus.alert("medium", "strategy", sym,
                           f"{sig['signal'].replace('_',' ')} {sig.get('strike','')} "
                           f"signal generated (confidence {sig['confidence']}%) — {trade_params}")
            self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                        "analysis": analysis})
        else:
            self.summary = f"scanned {len(jobs)} indices — WAIT"


SHADOW_PATH = os.path.expanduser("~/.ltp-monitor/shadow_signals.jsonl")


def _log_shadow_signal(bus, job, verdict, checks):
    """Persist every signal decision — approved AND rejected — so we can
    later ask 'was the risk agent right to reject that?' not just
    review the trades that were actually taken. Rejected signals get a
    pending resolution tracked forward against real subsequent prices."""
    sig, sym = job["signal"], job["symbol"]
    entry = {
        "id": f"{sym}-{int(time.time()*1000)}",
        "ts": now_ist().isoformat(), "symbol": sym,
        "signal": sig["signal"], "strike": sig.get("strike"),
        "entry": sig.get("entry"), "stoploss": sig.get("stoploss"),
        "target1": sig.get("target1"), "target2": sig.get("target2"),
        "confidence": sig.get("confidence"), "verdict": verdict,
        "checks": checks, "failed_checks": [c for c in checks if c.startswith("✗")],
        "resolution": "taken" if verdict == "APPROVED" else "pending",
    }
    try:
        os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
        with open(SHADOW_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    if verdict == "REJECTED":
        pending = bus.get("shadow_pending", [])
        pending.append(entry)
        bus.set("shadow_pending", pending[-100:])   # bounded, most-recent


def _resolve_shadow_signals(bus, get_chain):
    """Called periodically: check whether rejected signals, HAD they
    been taken, would have hit target or stoploss — using the same
    strike's real subsequent LTP. Times out after 90 minutes as
    'unresolved' (regime shifted enough that the hypothetical no
    longer means much)."""
    pending = bus.get("shadow_pending", [])
    if not pending:
        return
    still_pending = []
    resolved_updates = []
    for e in pending:
        age_min = (time.time() - datetime.fromisoformat(e["ts"]).timestamp()) / 60 \
            if isinstance(e["ts"], str) else 0
        try:
            chain = bus.get(f"chain:{e['symbol']}") or get_chain(e["symbol"])
            row = next((r for r in chain["rows"] if r["strike"] == e["strike"]), None)
            leg = "ce" if "CE" in e["signal"] else "pe"
            ltp = row[leg].get("ltp") if row else None
        except Exception:
            ltp = None
        outcome = None
        if ltp:
            if ltp >= (e.get("target1") or 1e18):
                outcome = "would_have_hit_target1"
            elif ltp <= (e.get("stoploss") or -1):
                outcome = "would_have_hit_stoploss"
        if not outcome and age_min > 90:
            outcome = "unresolved_timeout"
        if outcome:
            resolved_updates.append({**e, "resolution": outcome,
                                     "resolved_ltp": ltp,
                                     "resolved_at": now_ist().isoformat()})
        else:
            still_pending.append(e)
    bus.set("shadow_pending", still_pending)
    if resolved_updates:
        try:
            lines = open(SHADOW_PATH).readlines() if os.path.exists(SHADOW_PATH) else []
            by_id = {json.loads(l)["id"]: l for l in lines if l.strip()}
            for u in resolved_updates:
                by_id[u["id"]] = json.dumps(u) + "\n"
            with open(SHADOW_PATH, "w") as f:
                f.writelines(by_id.values())
        except Exception:
            pass


def news_risk_opportunity(news, signal_direction, cfg):
    """Roadmap item: replace the blanket news-risk block window with
    directional risk/opportunity scoring.

    The old gate blocked EVERY signal for `news_block_minutes` after any
    flagged news risk event, regardless of which way the news actually
    pointed — a bearish headline blocked BUY_PE (the trade it should have
    supported) just as hard as BUY_CE. This scores the news against the
    proposed trade's direction instead:

      - Returns (blocks, note, score). score is in [-1, 1]:
          score < 0  -> news conflicts with this trade direction (risk)
          score > 0  -> news supports this trade direction (opportunity,
                        never blocks — a bearish headline is exactly the
                        kind of signal a BUY_PE should be allowed to act on)
          score == 0 -> no directional read (neutral sentiment, or the
                        event has aged out)
      - The effect decays linearly from full strength at the moment the
        event was flagged to zero at `news_block_minutes` — a hard cliff
        at the deadline was replaced with a fading influence, so a trade
        proposed at 19 minutes isn't treated identically to one at 1
        minute.
      - Only a CONFLICTING direction can block; an aligned or neutral
        direction never does.
    """
    if not news.get("risk_event"):
        return False, "no active news risk", 0.0
    flagged_ts = news.get("flagged_ts", 0)
    window = max(1, cfg.get("news_block_minutes", 20))
    age_min = (time.time() - flagged_ts) / 60
    if age_min >= window:
        return False, f"news risk expired ({age_min:.0f}m > {window}m window)", 0.0
    decay = max(0.0, 1 - age_min / window)
    sentiment = news.get("sentiment", "neutral")
    wants_bull = signal_direction == "BUY_CE"
    wants_bear = signal_direction == "BUY_PE"
    if sentiment == "bearish" and wants_bull:
        return True, (f"bearish news conflicts with CE buy "
                      f"({age_min:.0f}m old, strength {decay:.2f})"), -decay
    if sentiment == "bullish" and wants_bear:
        return True, (f"bullish news conflicts with PE buy "
                      f"({age_min:.0f}m old, strength {decay:.2f})"), -decay
    if sentiment == "bearish" and wants_bear:
        return False, (f"bearish news aligns with PE buy — opportunity, "
                       f"not blocked (strength {decay:.2f})"), decay
    if sentiment == "bullish" and wants_bull:
        return False, (f"bullish news aligns with CE buy — opportunity, "
                       f"not blocked (strength {decay:.2f})"), decay
    return False, f"neutral-direction news risk ({age_min:.0f}m old) — no directional block", 0.0


class RiskAgent(Agent):
    name = "risk"
    interval = 5

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._queue = deque()
        bus.subscribe("signal", self._queue.append)
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_ts = 0
        self.halted = False
        bus.subscribe("closed", self._on_closed)

    def _on_closed(self, msg):
        pnl = msg.get("pnl", 0)
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_ts = time.time()
            cfg = config.load()
            stop_n = cfg.get("stop_after_consecutive_losses", 2)
            if stop_n and self.consecutive_losses >= stop_n:
                self.halted = True
                self.bus.alert("high", "risk", msg.get("symbol", ""),
                               f"AUTOPILOT HALTED — {self.consecutive_losses} "
                               f"losses in a row. Change to manual or reset in Settings.")
        else:
            self.consecutive_losses = 0

    def evaluate(self, job):
        """Run all pre-order checks. Returns (ok, checks)."""
        sig, cfg = job["signal"], config.load()
        checks = []
        ok = True

        def check(cond, label):
            nonlocal ok
            checks.append(("✓" if cond else "✗") + " " + label)
            ok = ok and cond

        trades = self.bus.get("trades_today", 0)
        check(not self.halted, "autopilot not halted (consecutive losses)")
        portfolio_halted_until = self.bus.get("portfolio_halt_until", 0)
        if portfolio_halted_until:
            remaining = (portfolio_halted_until - time.time()) / 60
            check(remaining <= 0,
                 f"portfolio kill-switch cooldown ({remaining:.0f}m remaining)")
        cooldown_min = cfg.get("cooldown_after_loss_min", 15)
        if cooldown_min > 0 and self.last_loss_ts:
            since_loss = (time.time() - self.last_loss_ts) / 60
            check(since_loss >= cooldown_min,
                  f"cooldown ({since_loss:.0f}m/{cooldown_min}m since last loss)")
        check(market_open(), "market open")
        positions = self.bus.get("positions", {}) or {}
        max_pos = cfg.get("max_concurrent_positions", 1)
        check(job["symbol"] not in positions,
              f"no open position on {job['symbol']}")
        check(len(positions) < max_pos,
              f"concurrent positions {len(positions)}/{max_pos}")
        check(trades < cfg["max_trades_per_day"],
              f"trades {trades}/{cfg['max_trades_per_day']}")
        check(sig["confidence"] >= cfg["min_confidence"],
              f"confidence {sig['confidence']}≥{cfg['min_confidence']}")
        check(sig.get("entry", 0) > 0 and sig.get("stoploss", 0) > 0,
              "valid price points")
        entry, sl, t1 = sig.get("entry", 0), sig.get("stoploss", 0), sig.get("target1", 0)
        rr = (t1 - entry) / (entry - sl) if entry > sl else 0
        check(rr >= 1.95, f"risk-reward {rr:.1f} (need ≥2.0)")
        atm = job["analysis"].get("atm")
        strike = sig.get("strike")
        if sig.get("signal") == "BUY_CE":
            check(strike is not None and atm and strike <= atm,
                  f"strike {strike} not OTM (ATM {atm})")
        elif sig.get("signal") == "BUY_PE":
            check(strike is not None and atm and strike >= atm,
                  f"strike {strike} not OTM (ATM {atm})")

        # ---- Regime & multi-timeframe checks (from RegimeAgent) ----
        # This is what would have blocked last Monday's 4 losing BUY_CE
        # trades in a rangebound/choppy tape.
        if cfg.get("regime_gate_enabled", True):
            regime = self.bus.get(f"regime:{job['symbol']}") or {}
            if not regime:
                # No regime data yet for this symbol (warmup / just switched)
                # — don't block on missing data, just note it.
                check(True, f"regime data pending for {job['symbol']} (not blocking)")
            else:
                reg_label = regime.get("regime", "unknown")
                allowed = regime.get("allowed_signals", [])
                if allowed:
                    check(sig["signal"] in allowed,
                          f"regime '{reg_label}' allows {allowed or 'nothing'}")
                elif reg_label in ("choppy", "rangebound"):
                    # explicit block for known-bad regimes
                    check(False, f"regime is {reg_label} — avoid directional buys")
                # multi-timeframe confluence — only when we actually have data
                confluence = regime.get("confluence", "no-alignment")
                if cfg.get("require_tf_confluence", True):
                    if sig["signal"] == "BUY_CE":
                        check(confluence in ("strong-bull", "mixed-bull"),
                              f"timeframe confluence for CE ({confluence})")
                    elif sig["signal"] == "BUY_PE":
                        check(confluence in ("strong-bear", "mixed-bear"),
                              f"timeframe confluence for PE ({confluence})")
        # News risk/opportunity scoring (roadmap: replaces the old blanket
        # block window). Only blocks trades whose direction actually
        # CONFLICTS with the news; an aligned direction is treated as an
        # opportunity and is never blocked, and the effect decays over
        # `news_block_minutes` instead of a hard on/off cliff.
        news = self.bus.get("news") or {}
        news_block, news_note, news_score = news_risk_opportunity(
            news, sig.get("signal"), cfg)
        self.bus.set(f"news_score:{job['symbol']}", news_score)
        check(not news_block, news_note)
        max_loss = (sig.get("entry", 0) - sig.get("stoploss", 0)) \
            * cfg["lot_sizes"].get(job["symbol"], 75) * cfg["lots_per_trade"]
        check(self.daily_pnl - max_loss > -abs(cfg.get("daily_loss_limit", 5000)),
              f"daily loss limit (risking ₹{max_loss:.0f}, day P&L ₹{self.daily_pnl:.0f})")
        profit_target = cfg.get("daily_profit_target", 0)
        if profit_target > 0:
            # Bug found 2026-07-24 from live logs: check() logs its label
            # on EVERY call (prefixed ✓/✗), not just on failure — this
            # message was hardcoded to always read as the failure case
            # ("reached... locking in"), so a normal PASSING check (day
            # P&L still under target) printed the nonsensical "✓ daily
            # profit target reached (₹0 ≥ ₹50000)" — literally false
            # arithmetic shown as if it were true. Message now correctly
            # describes whichever state actually holds.
            under_target = self.daily_pnl < profit_target
            check(under_target,
                  (f"daily profit target not yet reached (₹{self.daily_pnl:.0f} < "
                   f"₹{profit_target:.0f})") if under_target else
                  (f"daily profit target reached (₹{self.daily_pnl:.0f} ≥ "
                   f"₹{profit_target:.0f}) — locking in today's gain, no new positions"))
        data_age = time.time() - (self.bus.get(f"chain_ts:{job['symbol']}")
                                  or self.bus.get("chain_ts") or 0)
        check(data_age < 30, f"fresh {job['symbol']} data ({data_age:.0f}s old)")
        return ok, checks

    def cycle(self):
        if time.time() - getattr(self, "_last_shadow_resolve", 0) > 30:
            self._last_shadow_resolve = time.time()
            try:
                _resolve_shadow_signals(self.bus, self.ctx.get("get_chain"))
            except Exception:
                pass
        if not self._queue:
            return
        job = self._queue.popleft()
        ok, checks = self.evaluate(job)
        sig = job["signal"]
        verdict = "APPROVED" if ok else "REJECTED"
        self.summary = f"{verdict}: {sig['signal']} {sig.get('strike','')}"
        trade_params = (f"entry ₹{sig.get('entry')} SL ₹{sig.get('stoploss')} "
                       f"T1 ₹{sig.get('target1')} T2 ₹{sig.get('target2')}")
        self.bus.log(self.name, f"{verdict} — " + " · ".join(checks) + f" · {trade_params}")
        self.bus.set("last_risk_check", {"verdict": verdict, "checks": checks})
        _log_shadow_signal(self.bus, job, verdict, checks)
        if ok:
            self.bus.alert("high", "risk", job["symbol"],
                           f"Order APPROVED: {sig['signal'].replace('_',' ')} "
                           f"{sig.get('strike','')} — awaiting execution")
            self.bus.publish("approved", job)
        elif sig.get("confidence", 0) >= 60:
            failed = [c for c in checks if c.startswith("✗")]
            self.bus.alert("low", "risk", job["symbol"],
                           f"Signal REJECTED ({sig['confidence']}% conf): "
                           + "; ".join(failed))


class ExecutionAgent(Agent):
    name = "execution"
    interval = 2           # fast: enter approved orders + monitor open pos

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._queue = deque()
        bus.subscribe("approved", self._queue.append)

    def cycle(self):
        self._check_portfolio_kill_switch()
        if self._queue:
            self._enter(self._queue.popleft())
        self._monitor()
        self._monitor_spreads()
        self._auto_spreads()

    def _check_portfolio_kill_switch(self):
        """Regression testing (2026-07-20) surfaced a real gap: the
        daily loss limit only gates NEW entries against REALIZED P&L —
        it does nothing if several OPEN positions move against you
        together mid-event (a correlated crash across NIFTY/BANKNIFTY/
        FINNIFTY/SENSEX, exactly the scenario tested). This checks
        combined UNREALIZED P&L across every open position and spread,
        every cycle (2s), and force-closes everything if it breaches
        a configured threshold — independent of and in addition to the
        per-trade risk checks."""
        cfg = config.load()
        if not cfg.get("portfolio_kill_switch_enabled", True):
            return
        halted_until = self.bus.get("portfolio_halt_until", 0)
        if time.time() < halted_until:
            return   # already tripped this cooldown window
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        total_unrealized = (sum(p.get("pnl", 0) for p in positions.values())
                           + sum(s.get("pnl", 0) for s in spreads.values()))
        limit = cfg.get("portfolio_max_drawdown", 15000)
        if not (positions or spreads) or total_unrealized > -abs(limit):
            return
        # breach — force-close everything, no waiting for individual
        # stops/targets to catch up
        self.bus.log(self.name,
                     f"🚨 PORTFOLIO KILL-SWITCH: combined unrealized ₹{total_unrealized:.0f} "
                     f"breached -₹{limit} across {len(positions)} position(s) + "
                     f"{len(spreads)} spread(s) — force-closing everything")
        self.bus.alert("high", self.name, "PORTFOLIO",
                       f"KILL-SWITCH TRIPPED: ₹{total_unrealized:.0f} combined "
                       f"unrealized loss — all positions closed, new entries "
                       f"blocked for {cfg.get('portfolio_halt_cooldown_min', 60)}m")
        for sym in list(positions.keys()):
            self.exit(f"portfolio kill-switch (combined ₹{total_unrealized:.0f})",
                     symbol=sym)
        for sid in list(spreads.keys()):
            self.exit_spread(sid, f"portfolio kill-switch (combined ₹{total_unrealized:.0f})")
        cooldown = cfg.get("portfolio_halt_cooldown_min", 60) * 60
        self.bus.set("portfolio_halt_until", time.time() + cooldown)

    def _auto_spreads(self):
        """Server-side auto-deployment of enabled strategies. Runs whether
        or not the browser is open. Evaluates every symbol each minute.

        Diagnostic visibility added 2026-07-24: every skip path used to
        be a silent `continue` with no logging at all — meaning if
        bull_put_spread/bear_call_spread simply weren't finding eligible
        setups (wall too close to spot, credit too thin, wrong regime),
        there was no way to tell that apart from "auto-deploy isn't
        running." Same skip-reason-counter pattern already used in
        PriceActionAgent.cycle() for exactly this reason. The full
        evaluate() result (including its own `reasons` list) is also
        stashed per symbol+strategy on the bus so the Strategies page
        can show live "why not eligible right now" text, not just the
        backtest version history."""
        import backtester
        cfg = config.load()
        auto = cfg.get("auto_strategies") or []
        if not auto or not cfg["paper_mode"] or not market_open():
            return
        if time.time() - getattr(self, "_last_auto", 0) < 60:
            return
        self._last_auto = time.time()
        import strategies as slib
        spreads = self.bus.get("spreads", {}) or {}
        max_sp = cfg.get("max_concurrent_spreads", 2)
        cooldown = cfg.get("spread_reentry_cooldown_min", 15) * 60
        if not hasattr(self, "_spread_cd"):
            self._spread_cd = {}
        skipped = {"no_analysis": 0, "on_cooldown": 0, "not_eligible": 0,
                  "max_concurrent": 0, "entry_failed": 0}
        fired = []
        for sym in self.bus.get("symbols", []):
            analysis = self.bus.get(f"analysis:{sym}")
            regime = self.bus.get(f"regime:{sym}")
            if not analysis:
                skipped["no_analysis"] += len(auto)
                continue
            for name in auto:
                if len(spreads) >= max_sp:
                    skipped["max_concurrent"] += 1
                    continue
                cd_key = f"{sym}:{name}"
                if time.time() - self._spread_cd.get(cd_key, 0) < cooldown:
                    skipped["on_cooldown"] += 1
                    continue
                # The backtest-profitability gate protects LIVE money —
                # it must never block PAPER auto-deploy, since paper
                # trading is exactly how a strategy earns that proof in
                # the first place. Blocking it here would mean no
                # strategy could ever accumulate enough paper trades to
                # pass the gate.
                if not cfg["paper_mode"] and not backtester.is_live_enabled(name, sym):
                    continue
                ev = slib.evaluate(name, analysis, regime)
                self.bus.set(f"spread_eval:{sym}:{name}", ev)
                if ev and ev.get("eligible"):
                    r = self.enter_spread(ev)
                    if r.get("ok"):
                        self._spread_cd[cd_key] = time.time()
                        spreads = self.bus.get("spreads", {}) or {}
                        fired.append(f"{sym} {name}")
                    else:
                        skipped["entry_failed"] += 1
                        self.bus.log(self.name,
                                    f"{sym} {name}: eligible but entry failed "
                                    f"— {r.get('error', 'unknown reason')}")
                else:
                    skipped["not_eligible"] += 1
        self.summary = ("deployed: " + ", ".join(fired)) if fired else \
            f"scanning {len(auto)} auto strategy(ies) across {len(self.bus.get('symbols', []))} symbols ({skipped})"
        # Diagnostic breadcrumb every ~10 min when nothing fires — same
        # cadence/rationale as PriceActionAgent's equivalent breadcrumb.
        if not fired and time.time() - getattr(self, "_last_spread_diag_log", 0) > 600:
            self._last_spread_diag_log = time.time()
            reasons_seen = []
            for sym in self.bus.get("symbols", []):
                for name in auto:
                    ev = self.bus.get(f"spread_eval:{sym}:{name}")
                    if ev and not ev.get("eligible") and ev.get("reasons"):
                        reasons_seen.append(f"{sym}/{name}: {'; '.join(ev['reasons'])}")
            self.bus.log(self.name,
                        f"no spreads deployed this cycle — {skipped}. "
                        + ("latest ineligibility reasons: " + " | ".join(reasons_seen)
                           if reasons_seen else "no eligibility data yet"))

    # ================= defined-risk spreads (PAPER ONLY, phase 1) =========
    def enter_spread(self, spread):
        """Open a 2-leg credit spread. Refuses in live mode (phase 1)."""
        cfg = config.load()
        if not cfg["paper_mode"]:
            return {"error": "Spreads are paper-mode only in this version. "
                             "Enable Paper mode in Settings to use them."}
        sym = spread["symbol"]
        spreads = self.bus.get("spreads", {}) or {}
        sid = f"{sym}:{spread['name']}:{spread['short_strike']:.0f}"
        if sid in spreads:
            return {"error": f"{spread['name']} already open on {sym} "
                             f"at {spread['short_strike']:.0f}"}
        if len(spreads) >= cfg.get("max_concurrent_spreads", 2):
            return {"error": f"Max concurrent spreads "
                             f"({cfg.get('max_concurrent_spreads', 2)}) reached."}
        lot = cfg["lot_sizes"].get(sym, 75)
        import sizing
        deployed = sizing.deployed_capital(cfg, self.bus.get("positions", {}), spreads)
        n_lots, sizing_why = sizing.size_spread(cfg, sym, spread["max_loss"], deployed)
        self.bus.log(self.name, f"{sym} spread sizing: {sizing_why}")
        if n_lots < 1:
            return {"error": f"Not enough available capital for even 1 lot "
                            f"after existing positions/spreads — {sizing_why}"}
        qty = lot * n_lots
        credit = spread["credit"]
        margin_used = round(cfg.get("margin_per_lot_spread", 85000) * n_lots, 0)
        pos = {
            "id": sid, "strategy": spread["name"], "symbol": sym,
            "legs": [dict(l, entry=l["ltp"]) for l in spread["legs"]],
            "qty": qty, "lots": n_lots,
            "credit": credit, "max_loss": spread["max_loss"],
            "margin_used": margin_used,
            "width": spread["width"], "short_strike": spread["short_strike"],
            # Exit thresholds, configurable in Settings. Bug found
            # 2026-07-22: the old fixed 60% profit target NEVER fired —
            # every spread that day rode to EOD square-off with GOT%
            # between -13% and +20% of the target, nowhere close to 60%.
            # A defined-risk credit spread's value decays with theta
            # over its full life to expiry; expecting 60% of that
            # captured within a single session is unrealistic unless
            # there's a large adverse-to-short-side move. Lowered to a
            # target that's actually reachable intraday from time decay
            # + typical moves, with a matching tighter loss cap so the
            # risk:reward isn't stretched into needing an unrealistic
            # win rate to break even.
            "profit_target": round(credit * cfg.get("spread_profit_target_pct", 30) / 100, 2),
            "loss_limit": round(min(credit * cfg.get("spread_loss_limit_multiple", 1.0),
                                    spread["max_loss"]), 2),
            "opened": now_ist().strftime("%H:%M:%S"), "opened_ts": time.time(),
            "pnl": 0.0, "paper": True, "ai_advice": None, "ai_ts": 0,
        }
        spreads[sid] = pos
        self.bus.set("spreads", spreads)
        self.bus.log(self.name,
                     f"📄 PAPER SPREAD {spread['name']} {sym}: "
                     + " · ".join(f"{l['action']} {l['strike']:.0f} {l['leg']}"
                                  f" @ ₹{l['ltp']}" for l in spread["legs"])
                     + f" · credit ₹{credit} x {qty}")
        self.bus.alert("medium", "execution", sym,
                       f"Spread opened: {spread['name']} credit ₹{credit}")
        return {"ok": True, "spread": pos}

    def _spread_leg_ltp(self, chain, leg):
        row = next((r for r in chain["rows"]
                    if r["strike"] == leg["strike"]), None)
        return row[leg["leg"].lower()].get("ltp") if row else None

    def _monitor_spreads(self):
        cfg = config.load()
        spreads = self.bus.get("spreads", {}) or {}
        if not spreads:
            return
        for sid, sp in list(spreads.items()):
            chain = self.bus.get(f"chain:{sp['symbol']}")
            ltps = ([self._spread_leg_ltp(chain, l) for l in sp["legs"]]
                   if chain else [None] * len(sp["legs"]))
            stale = any(v is None or v == 0 for v in ltps)
            if stale and not market_open():
                # same class of bug as the single-leg fix above: don't
                # let a stale post-close feed block EOD square-off —
                # force it closed using each leg's last known price
                for leg, last in zip(sp["legs"], ltps):
                    if last:
                        leg["ltp"] = last
                pnl_ps = sum((l["entry"] - l["ltp"]) if l["action"] == "SELL"
                            else (l["ltp"] - l["entry"]) for l in sp["legs"])
                sp["pnl"] = round(pnl_ps * sp["qty"], 0)
                sp["pnl_per_share"] = round(pnl_ps, 2)
                self.exit_spread(sid, "market closed — forced square-off (feed stale)")
                continue
            if stale:
                continue
            # combined P&L per share: SELL leg profits as price falls
            pnl_ps = 0.0
            for leg, ltp in zip(sp["legs"], ltps):
                leg["ltp"] = ltp
                d = (leg["entry"] - ltp) if leg["action"] == "SELL" \
                    else (ltp - leg["entry"])
                pnl_ps += d
            sp["pnl"] = round(pnl_ps * sp["qty"], 0)
            sp["pnl_per_share"] = round(pnl_ps, 2)
            sp["mfe"] = max(sp.get("mfe", 0), sp["pnl"])
            sp["mae"] = min(sp.get("mae", 0), sp["pnl"])
            spot = chain.get("spot")
            reason = None
            # Defense zone: act BEFORE a full breach, not only at it. If
            # spot is within defense_zone_pct of the width from the short
            # strike but hasn't crossed it yet, tighten the loss limit so
            # a continued adverse move exits sooner with a smaller loss
            # than waiting for the full defined-risk cap.
            if cfg.get("spread_defense_enabled", True) and spot and not sp.get("defended"):
                short_leg = sp["legs"][0]["leg"]
                dist = (spot - sp["short_strike"] if short_leg == "PE"
                        else sp["short_strike"] - spot)
                zone = sp["width"] * cfg.get("spread_defense_zone_pct", 30) / 100
                if 0 < dist <= zone:
                    old_limit = sp["loss_limit"]
                    sp["loss_limit"] = round(
                        old_limit * cfg.get("spread_defense_tighten_pct", 50) / 100, 2)
                    sp["defended"] = True
                    self.bus.log(self.name,
                                 f"🛡 {sp['symbol']} {sp['strategy']} defense triggered — "
                                 f"spot {spot:.0f} within {dist:.0f}pts of short strike "
                                 f"{sp['short_strike']:.0f}, loss limit tightened "
                                 f"₹{old_limit:.1f} → ₹{sp['loss_limit']:.1f}")
                    self.bus.alert("medium", self.name, sp["symbol"],
                                   f"{sp['strategy']} defense: short strike approached, "
                                   f"stop tightened to ₹{sp['loss_limit']:.1f}")
            # Profit lock-in ratchet: once P&L reaches a fraction of the
            # profit target, start protecting a share of that gain
            # instead of requiring the FULL target (often unrealistic
            # intraday) or letting it fully round-trip to breakeven/loss
            # by end-of-day square-off. Found 2026-07-22: order history
            # showed exactly this pattern — MFE often 2-3x the final P&L
            # on spreads that rode to "market closing" square-off, while
            # the only two spreads exited manually captured profit close
            # to their peak. This ratchets a floor the same way the
            # single-leg trailing SL already does, rising as new peaks
            # are made, never falling back down.
            lock_trigger = sp["profit_target"] * cfg.get("spread_profit_lock_trigger_pct", 80) / 100
            if pnl_ps >= lock_trigger:
                candidate_floor = round(pnl_ps * cfg.get("spread_profit_lock_pct", 75) / 100, 2)
                if candidate_floor > sp.get("profit_floor", 0):
                    sp["profit_floor"] = candidate_floor
            if pnl_ps >= sp["profit_target"]:
                reason = f"captured ₹{pnl_ps:.1f} of ₹{sp['credit']} credit"
            elif sp.get("profit_floor", 0) > 0 and pnl_ps <= sp["profit_floor"] \
                    and (sp["profit_floor"] * sp["qty"]) >= cfg.get(
                        "spread_profit_lock_min_rupees", 250):
                # Absolute-₹ guard: the floor must be worth exiting for.
                # Without this the ratchet fired on ₹0.1-2/share peaks
                # where fees exceeded the entire gain — 26 such exits
                # netted ₹62 total across the 2026-07-16..23 live data.
                reason = (f"profit lock: gave back to floor ₹{sp['profit_floor']:.1f}/sh "
                         f"(₹{sp['profit_floor'] * sp['qty']:.0f}) "
                         f"after peaking near ₹{sp.get('mfe', 0) / sp['qty']:.1f}/sh")
            elif pnl_ps <= -sp["loss_limit"]:
                reason = f"loss limit (₹{pnl_ps:.1f} vs -₹{sp['loss_limit']})"
            elif spot and (
                (sp["legs"][0]["leg"] == "PE" and spot < sp["short_strike"]) or
                (sp["legs"][0]["leg"] == "CE" and spot > sp["short_strike"])):
                reason = f"short strike breached (spot {spot:.0f})"
            elif cfg.get("time_stop_minutes", 0) and sp.get("opened_ts") and \
                    (time.time() - sp["opened_ts"]) / 60 >= cfg["time_stop_minutes"]:
                elapsed = (time.time() - sp["opened_ts"]) / 60
                reason = (f"time stop ({elapsed:.0f}m ≥ {cfg['time_stop_minutes']}m) "
                         f"— forcing a decision rather than waiting indefinitely")
            elif not market_open():
                reason = "market closing — squaring off spread"
            spreads[sid] = sp
            self.bus.set("spreads", spreads)
            if reason:
                self.exit_spread(sid, reason)
            else:
                self._spread_ai_check(sp, chain)

    def _spread_ai_check(self, sp, chain):
        """Periodic LLM advisory for the open spread (HOLD/EXIT + why).
        By default this is advisory only — rule exits (profit target,
        loss limit, time stop, breach, spread defense) remain the only
        thing that actually closes a spread. Found 2026-07-22: this was
        confusing in practice — the AI would confidently analyze a
        position and say "EXIT, 85%, because X" every 5 minutes, but
        nothing ever happened with that beyond a passive alert, which
        looked like the AI was "just watching" and doing nothing.
        `spread_ai_auto_exit_enabled` (Settings) lets a confident EXIT
        call actually close the spread — off by default so this stays
        the same conservative advisory-only behavior unless turned on."""
        cfg = config.load()
        if cfg.get("ai_engine", "local") == "off":
            return
        if time.time() - sp.get("ai_ts", 0) < 300:     # every 5 min max
            return
        sp["ai_ts"] = time.time()
        try:
            import llm, json as _json
            prompt = (
                "You monitor an open Indian index option credit spread. "
                "Reply ONLY JSON: {\"advice\":\"HOLD|EXIT\",\"confidence\":0-100,"
                "\"why\":\"<15 words\"}.\n"
                f"Strategy: {sp['strategy']} on {sp['symbol']}. "
                f"Short strike {sp['short_strike']}, spot {chain.get('spot')}, "
                f"credit taken {sp['credit']}, current P&L/share "
                f"{sp.get('pnl_per_share', 0)}, profit target "
                f"{sp['profit_target']}, loss limit {sp['loss_limit']}.")
            text, engine, err = llm.generate_json(prompt, max_tokens=120)
            if err or not text:
                sp["ai_advice"] = None if err == "ai_off" else f"AI unavailable ({err})"
                return
            j = _json.loads(text)
            if j and j.get("advice"):
                sp["ai_advice"] = (f"{j['advice']} ({j.get('confidence', '?')}%)"
                                   f" — {j.get('why', '')} · {engine}")
                confidence = int(j.get("confidence", 0))
                threshold = cfg.get("spread_ai_exit_confidence_threshold", 75)
                if j["advice"] == "EXIT" and confidence >= threshold:
                    why = j.get("why", "")
                    self.bus.alert("medium", "execution", sp["symbol"],
                                   f"AI suggests exiting {sp['strategy']}: {why}")
                    if cfg.get("spread_ai_auto_exit_enabled", False):
                        sid = sp["id"]
                        self.bus.log(self.name,
                                     f"AI auto-exit ENABLED — closing {sp['strategy']} "
                                     f"{sp['symbol']} on AI advisory ({confidence}%): {why}")
                        self.exit_spread(sid, f"AI advisory EXIT ({confidence}%): {why}")
        except Exception as e:
            sp["ai_advice"] = f"AI check unavailable ({e})"

    def exit_spread(self, sid, reason="manual exit"):
        spreads = self.bus.get("spreads", {}) or {}
        sp = spreads.get(sid)
        if not sp:
            return {"error": "spread not found"}
        cfg = config.load()
        # fees: per lot per transaction; 2 legs x 2 transactions = 4
        fees = round(cfg.get("fee_per_lot", 40) * sp["lots"] * 4, 0)
        gross = sp.get("pnl", 0)
        now = now_ist()
        closed = {
            "symbol": sp["symbol"], "leg": "SPREAD",
            "strike": sp["short_strike"], "qty": sp["qty"],
            "lots": sp["lots"], "strategy": sp["strategy"],
            "entry": sp["credit"], "ltp": sp.get("pnl_per_share", 0),
            "stoploss": -sp["loss_limit"], "target1": sp["profit_target"],
            "target2": sp["credit"],
            "closed": now.strftime("%H:%M:%S"),
            "closed_date": now.strftime("%Y-%m-%d"),
            "closed_at": now.isoformat(),
            "opened": sp["opened"], "opened_ts": sp.get("opened_ts"), "paper": True,
            "gross_pnl": gross, "fees": fees,
            "mfe": sp.get("mfe", 0), "mae": sp.get("mae", 0),
            "pnl": round(gross - fees, 0),
            "reason": f"[{sp['strategy']}] {reason}",
        }
        self.bus.log(self.name,
                     f"📄 SPREAD CLOSED {sp['strategy']} {sp['symbol']} — "
                     f"{reason} · gross ₹{gross:.0f} - fees ₹{fees:.0f} "
                     f"= net ₹{closed['pnl']:.0f}")
        spreads.pop(sid, None)
        self.bus.set("spreads", spreads)
        if not hasattr(self, "_spread_cd"):
            self._spread_cd = {}
        self._spread_cd[f"{sp['symbol']}:{sp['strategy']}"] = time.time()
        trades = self.bus.get("closed_trades", [])
        trades.append(closed)
        self.bus.set("closed_trades", trades)
        _append_trade(closed)
        self.bus.alert("high", "execution", sp["symbol"],
                       f"Spread closed — {reason} — net ₹{closed['pnl']:.0f}")
        self.bus.publish("closed", closed)
        return {"closed": closed}

    def _enter(self, job):
        cfg = config.load()
        if not cfg["auto_execute"]:
            self.bus.set("pending_confirmation", job)
            self.summary = "signal approved — awaiting your confirmation"
            self.bus.log(self.name, "auto-execute OFF: order waits for "
                                    "manual confirm in dashboard")
            return
        self.place(job)

    def place(self, job, manual=False):
        cfg = config.load()
        sig, analysis, sym = job["signal"], job["analysis"], job["symbol"]
        lot = cfg["lot_sizes"].get(sym, 75)
        import sizing
        deployed = sizing.deployed_capital(
            cfg, self.bus.get("positions", {}), self.bus.get("spreads", {}))
        if sig.get("source") == "mtf_confluence" and sig.get("atr"):
            # rinkoo.docx's exact ATR-based formula for this specific
            # strategy — delta=0.5 since this is an ATM option, not a
            # future (see mtf_confluence_strategy.py / size_by_atr_risk
            # docstrings for the reasoning). Every other strategy keeps
            # the existing risk_pct-based size_option_buy — this hook
            # only fires for signals explicitly tagged mtf_confluence.
            n_lots, sizing_why = sizing.size_by_atr_risk(
                cfg, sym, sig["atr"], delta=0.5, deployed=deployed)
        else:
            n_lots, sizing_why = sizing.size_option_buy(
                cfg, sym, sig["entry"], sig["stoploss"], deployed)
        self.bus.log(self.name, f"{sym} sizing: {sizing_why}")
        if n_lots < 1:
            self.bus.log(self.name, f"⚠ {sym} order skipped — not enough "
                                    f"available capital for even 1 lot")
            return {"error": "insufficient available capital"}
        qty = lot * n_lots
        leg = "CE" if sig["signal"] == "BUY_CE" else "PE"
        label = f"{sym} {sig['strike']} {leg}"
        fill = sig.get("option_ltp") or sig["entry"]
        if cfg["paper_mode"]:
            order_id = f"PAPER-{int(time.time())}"
            self.bus.log(self.name, f"📄 PAPER BUY {qty} x {label} @ ₹{fill}")
        else:
            orders = self.ctx["orders_factory"]()
            if orders is None or not sig.get("security_id"):
                self.bus.log(self.name, "⚠ cannot place live order "
                                        "(no broker / security_id)")
                return
            resp = orders.place(sym, sig["security_id"], "BUY", qty, "MARKET")
            order_id = resp.get("orderId", "?")
            self.bus.log(self.name, f"🔴 LIVE BUY {qty} x {label} — "
                                    f"order {order_id}")
        pos = {
            "symbol": sym, "strike": sig["strike"], "leg": leg, "qty": qty,
            "lots": n_lots,
            "entry": fill, "stoploss": sig["stoploss"],
            "target1": sig["target1"], "target2": sig["target2"],
            "spot_invalidation": sig.get("spot_invalidation"),
            "security_id": sig.get("security_id"), "order_id": order_id,
            "opened": now_ist().strftime("%H:%M:%S"), "opened_ts": time.time(), "ltp": fill,
            "pnl": 0.0, "t1_hit": False, "paper": cfg["paper_mode"],
            "manual": manual, "capital_used": round(fill * qty, 0),
        }
        positions = self.bus.get("positions", {}) or {}
        positions[sym] = pos
        self.bus.set("positions", positions)
        self.bus.set("position", pos)   # legacy single-position mirror (most-recent)
        self.bus.set("trades_today", self.bus.get("trades_today", 0) + 1)
        self.bus.set("pending_confirmation", None)
        self.bus.alert("high", "execution", sym,
                       f"{'PAPER' if cfg['paper_mode'] else 'LIVE'} entry: "
                       f"BUY {qty} x {label} @ ₹{fill}")

    def _monitor(self):
        positions = self.bus.get("positions", {}) or {}
        if not positions:
            self.summary = self.summary or "idle"
            return
        summaries = []
        for sym, p in list(positions.items()):
            r = self._monitor_one(p)
            if r:
                summaries.append(r)
        self.summary = " · ".join(summaries) if summaries else "idle"

    def _monitor_one(self, p):
        cfg = config.load()
        sym = p["symbol"]
        chain = self.bus.get(f"chain:{sym}")
        row = (next((r for r in chain["rows"] if r["strike"] == p["strike"]), None)
              if chain else None)
        ltp = row[p["leg"].lower()].get("ltp") if row else None
        if not ltp and not market_open():
            # Market is closed AND the feed has nothing fresh — this is
            # exactly the failure mode that left a position stuck open
            # overnight: the old code returned early on "no LTP" before
            # ever reaching the EOD check below. Force the square-off
            # using the last successfully recorded price rather than
            # waiting forever for a quote that will never arrive once
            # the session has ended.
            ltp = p.get("ltp") or p.get("entry")
            self.exit(f"market closed — forced square-off (feed stale, "
                     f"last known price ₹{ltp})", symbol=sym)
            return f"{sym} {p['strike']} {p['leg']} — forced EOD close (stale feed)"
        if not chain or not row:
            return None
        if not ltp:
            # Dhan sometimes returns no/0 LTP for a strike in a given
            # snapshot — skip this cycle rather than comparing None
            return f"{sym} {p['strike']} {p['leg']} — no LTP; retrying"
        p["ltp"] = ltp
        p["pnl"] = round((ltp - p["entry"]) * p["qty"], 0)
        p["mfe"] = max(p.get("mfe", 0), p["pnl"])
        p["mae"] = min(p.get("mae", 0), p["pnl"])
        spot = chain["spot"]
        summary = f"{sym} {p['strike']} {p['leg']} ₹{ltp} P&L ₹{p['pnl']:.0f}"
        cfg = config.load()

        # Rupee-based step-ratchet trailing (alternative to the %-based
        # trail_sl_* mechanism below) — matches a "when profit reaches X,
        # lock at Y; every further Z gained, raise the floor by W" style
        # ratchet in plain rupees rather than price %. Computed BEFORE
        # the exit-reason chain so a floor raised this cycle is already
        # current when checked for breach this same cycle.
        if cfg.get("step_trail_enabled", False):
            lock_trigger = cfg.get("step_trail_lock_trigger_rupees", 2000)
            lock_profit = cfg.get("step_trail_lock_profit_rupees", 1000)
            step_rupees = cfg.get("step_trail_step_rupees", 1000)
            step_gain = cfg.get("step_trail_step_gain_rupees", 500)
            if p["pnl"] >= lock_trigger:
                floor = lock_profit
                if step_rupees > 0:
                    extra_steps = int((p["pnl"] - lock_trigger) // step_rupees)
                    floor += extra_steps * step_gain
                if floor > p.get("step_floor", 0):
                    p["step_floor"] = floor

        reason = None
        txn_sl = cfg.get("transaction_stop_loss_rupees", 0)
        txn_target = cfg.get("transaction_target_rupees", 0)
        if txn_sl > 0 and p["pnl"] <= -abs(txn_sl):
            reason = f"transaction stop loss (₹{p['pnl']:.0f} ≤ -₹{txn_sl:.0f})"
        elif txn_target > 0 and p["pnl"] >= txn_target:
            reason = f"transaction target (₹{p['pnl']:.0f} ≥ ₹{txn_target:.0f})"
        elif p.get("step_floor", 0) > 0 and p["pnl"] <= p["step_floor"]:
            reason = (f"step-trail: gave back to floor ₹{p['step_floor']:.0f} "
                     f"(peak ₹{p.get('mfe', 0):.0f})")
        elif ltp <= p["stoploss"]:
            reason = f"stoploss (₹{ltp} ≤ ₹{p['stoploss']})"
        elif ltp >= p["target2"]:
            reason = f"target-2 (₹{ltp})"
        elif p["t1_hit"] and ltp <= p["entry"]:
            reason = "gave back gains after T1"
        elif p.get("spot_invalidation") and spot:
            inv = p["spot_invalidation"]
            if (p["leg"] == "CE" and spot < inv) or (p["leg"] == "PE" and spot > inv):
                reason = f"spot invalidation ({spot:.0f} vs {inv})"
        elif cfg.get("time_stop_minutes", 0) and p.get("opened_ts") and \
                (time.time() - p["opened_ts"]) / 60 >= cfg["time_stop_minutes"]:
            elapsed = (time.time() - p["opened_ts"]) / 60
            reason = (f"time stop ({elapsed:.0f}m ≥ {cfg['time_stop_minutes']}m) "
                     f"— neither target nor stop hit, forcing a decision")
        elif not market_open():
            reason = "market closing — squaring off intraday position"

        if not p["t1_hit"] and ltp >= p["target1"]:
            p["t1_hit"] = True
            p["stoploss"] = max(p["stoploss"], p["entry"])
            self.bus.log(self.name, f"✅ {sym} T1 hit ₹{ltp} — SL trailed to "
                                    f"breakeven ₹{p['stoploss']}")
        # ---- trailing stoploss (independent of T1) ----
        # Once the option moves trigger% above entry, the SL follows the
        # peak price at gap% below it (fixed_pct mode) OR at an
        # ATR-scaled distance below it (atr mode) — locks in profit
        # instead of riding a winner all the way back to the original
        # wide SL either way.
        cfg = config.load()
        if cfg.get("trail_sl_enabled", True):
            p["peak"] = max(p.get("peak", p["entry"]), ltp)
            trigger = p["entry"] * (1 + cfg.get("trail_sl_trigger_pct", 5) / 100)
            if p["peak"] >= trigger:
                if cfg.get("trail_sl_mode", "fixed_pct") == "atr":
                    regime = self.bus.get(f"regime:{sym}") or {}
                    atr_pct = regime.get("atr_pct")
                    if atr_pct:
                        gap_pct = atr_pct * cfg.get("atr_trail_multiplier", 1.5)
                        trail_to = round(p["peak"] * (1 - gap_pct / 100), 2)
                        mode_note = f"ATR-based, {atr_pct:.2f}% underlying ATR"
                    else:
                        # no ATR reading available yet (e.g. regime not
                        # computed this cycle) — fall back to fixed_pct
                        # rather than skip trailing entirely
                        trail_to = round(p["peak"] * (1 - cfg.get("trail_sl_gap_pct", 10) / 100), 2)
                        mode_note = "fixed_pct fallback (no ATR reading yet)"
                else:
                    trail_to = round(p["peak"] * (1 - cfg.get("trail_sl_gap_pct", 10) / 100), 2)
                    mode_note = "fixed_pct"
                if trail_to > p["stoploss"]:
                    p["stoploss"] = trail_to
                    self.bus.log(self.name,
                                 f"↗ {sym} trail SL → ₹{trail_to} "
                                 f"(peak ₹{p['peak']:.1f}, {mode_note})")
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            positions[sym] = p
            self.bus.set("positions", positions)
            self.bus.set("position", p)
        if reason:
            self.exit(reason, symbol=sym)
        return summary

    def exit(self, reason="manual exit", symbol=None):
        positions = self.bus.get("positions", {}) or {}
        if symbol is None:
            # backward-compat: no symbol given -> exit the single/most
            # recent position (used by the manual "Exit position" button
            # when only one trade is open)
            symbol = next(iter(positions), None)
        p = positions.get(symbol)
        if not p:
            return {"error": "no open position"}
        if p["paper"]:
            self.bus.log(self.name, f"📄 PAPER SELL {p['qty']} x {p['symbol']} "
                         f"{p['strike']} {p['leg']} @ ₹{p['ltp']} — {reason} "
                         f"· P&L ₹{p['pnl']:.0f}")
        else:
            orders = self.ctx["orders_factory"]()
            if orders and p.get("security_id"):
                try:
                    resp = orders.place(p["symbol"], p["security_id"], "SELL",
                                        p["qty"], "MARKET")
                    self.bus.log(self.name, f"🔴 LIVE SELL — order "
                                 f"{resp.get('orderId','?')} — {reason} "
                                 f"· est P&L ₹{p['pnl']:.0f}")
                except Exception as e:
                    self.bus.log(self.name, f"⚠ LIVE EXIT FAILED: {e} — "
                                            "close manually on Dhan NOW")
                    return {"error": str(e)}
            else:
                self.bus.log(self.name, "⚠ no broker for live exit — close "
                                        "manually on Dhan")
        now = now_ist()
        # fees: ₹fee_per_lot per lot per transaction; entry + exit = 2
        cfg = config.load()
        lots = p.get("lots") or max(1, round(p["qty"] / cfg["lot_sizes"].get(p["symbol"], 75)))
        fees = round(cfg.get("fee_per_lot", 40) * lots * 2, 0)
        gross = p.get("pnl", 0)
        closed = dict(p,
                      closed=now.strftime("%H:%M:%S"),
                      closed_date=now.strftime("%Y-%m-%d"),
                      closed_at=now.isoformat(),
                      gross_pnl=gross,
                      fees=fees,
                      pnl=round(gross - fees, 0),   # NET of fees
                      reason=reason)
        self.bus.set("position", None)
        positions.pop(symbol, None)
        self.bus.set("positions", positions)
        if positions:
            # legacy mirror points at whatever's still open (dashboard
            # code that reads a single "position" still gets something
            # useful rather than None while other trades remain open)
            self.bus.set("position", next(iter(positions.values())))
        trades = self.bus.get("closed_trades", [])
        trades.append(closed)
        self.bus.set("closed_trades", trades)
        _append_trade(closed)          # persist to disk immediately
        self.bus.alert("high", "execution", p["symbol"],
                       f"Exited {p['strike']} {p['leg']} — {reason} — "
                       f"P&L ₹{p['pnl']:.0f}")
        self.bus.publish("closed", closed)
        return {"closed": closed}


class LearningAgent(Agent):
    name = "learning"
    interval = 300

    def cycle(self):
        t = now_ist()
        today = t.strftime("%Y-%m-%d")
        done = self.bus.get("journal_done")
        trades = self.bus.get("closed_trades", [])
        if done == today:
            self.summary = "today's journal written"
            return
        if t.hour * 60 + t.minute < 15 * 60 + 35:
            self.summary = f"tracking {len(trades)} closed trades; journal at 15:35"
            return
        pnl = sum(x["pnl"] for x in trades)
        wins = sum(1 for x in trades if x["pnl"] > 0)
        stats = {"date": today, "trades": len(trades), "wins": wins,
                 "pnl": pnl}
        engine_on = config.load().get("ai_engine","local") != "off"
        critique = ""
        if engine_on and trades:
            try:
                critique = claude(
                    "You are a trading coach. Review today's option trades "
                    "(entry/exit/reason/P&L below). In 5 bullet lines: what "
                    "worked, what didn't, and one concrete adjustment for "
                    "tomorrow.\n\n" + json.dumps(trades, default=str), None, 400)
            except Exception as e:
                critique = f"(AI review unavailable: {e})"
        entry = {**stats, "critique": critique, "trades_detail": trades}
        journal = []
        if os.path.exists(JOURNAL):
            try:
                journal = json.load(open(JOURNAL))
            except Exception:
                pass
        journal.append(entry)
        json.dump(journal, open(JOURNAL, "w"), indent=2, default=str)
        self.bus.set("journal_done", today)
        self.bus.set("journal_latest", entry)
        self.summary = f"journal: {len(trades)} trades, P&L ₹{pnl:.0f}"
        self.bus.log(self.name, self.summary)


# ================================================================== orchestrator



class BacktestAgent(Agent):
    """Daily historical archive + strategy validation (post 15:45 IST).

    Cycle: after close -> archive today's chains + index candles, replay
    every strategy over the full local archive, store metrics, then
    revalidate live results vs backtest (retune -> re-test -> deploy or
    roll back, with reasons logged per requirement 8-11)."""
    name, interval = "backtest", 20

    RESULTS = os.path.expanduser("~/.ltp-monitor/backtests.json")

    def cycle(self):
        import backtester, history
        now = now_ist()
        ran = self.bus.get("bt_last_run")
        today = now.strftime("%Y-%m-%d")
        job = self.bus.get("bt_manual_job")
        if job:
            self.bus.set("bt_manual_job", None)
            self._run(job.get("sync", False))
            return
        if now.hour * 60 + now.minute < 15 * 60 + 45 or ran == today:
            last = self.bus.get("bt_last_summary")
            self.summary = ("idle · " + last) if last else \
                "idle — daily run at 15:45; use Run backtest for manual run"
            return
        self.bus.set("bt_last_run", today)
        self._run(sync=True)

    def _run(self, sync):
        import backtester, history
        dhan = self.ctx["dhan_client"]()
        syms = self.bus.get("symbols", ["NIFTY"])
        if sync and dhan:
            for sym in syms:
                try:
                    def prog(m, _s=sym):
                        self.summary = f"archiving {_s}: {m}"
                    # Bug found 2026-07-22: this was `log=lambda m: None`
                    # — a no-op. sync_day_chain() has detailed diagnostic
                    # logging for exactly this kind of failure ("no
                    # candles today" vs "chain sync FAILED after
                    # retries" vs "N legs failed"), but the automated
                    # daily run threw all of it away, which is exactly
                    # why SENSEX repeatedly showing 0 chain days had no
                    # visible cause anywhere in the logs.
                    history.sync_day_chain(self.ctx["get_chain"], dhan, sym,
                                           log=lambda m: self.bus.log(self.name, m),
                                           progress=prog)
                except Exception as e:
                    self.bus.log(self.name, f"sync {sym}: {str(e)[:200]}")
        self.bus.set("bt_coverage", history.coverage())
        results = {}
        for sym in syms:
            if not history.chain_days(sym) and not history.index_days(sym, 1):
                continue
            self.summary = f"backtesting {sym}..."
            try:
                results[sym] = backtester.run_all(sym, log=lambda m: None)
            except Exception as e:
                self.bus.log(self.name, f"backtest {sym}: {str(e)[:70]}")
        json.dump({"at": now_ist().isoformat(), "results": results},
                  open(self.RESULTS, "w"), indent=1)
        self.bus.set("bt_results", results)
        self._revalidate(results)
        self._tune_pa(results)
        self.summary = "backtest complete: " + ", ".join(results) if results             else "no archived chain days yet — archive builds daily from close"
        self.bus.log(self.name, self.summary)

    def _revalidate(self, results):
        import strategies as slib
        """Per-symbol adaptive tuning for the credit-spread strategies —
        same mechanics and profitability gate as price-action strategies:
        under-trading relaxes entry filters, losing money tightens them,
        and a version only goes LIVE once its OWN backtest is net
        profitable with enough trades to mean something."""
        import backtester, history
        cfg = config.load()
        target = cfg.get("pa_min_trades_per_day", 0.3)
        min_conf = cfg.get("pa_min_trades_for_confidence", 15)
        improve_thresh = cfg.get("pa_tuning_improvement_threshold", 0.15)
        max_attempts = cfg.get("pa_tuning_max_attempts", 4)
        cooldown_days = cfg.get("pa_retune_cooldown_days", 7)
        today_str = now_ist().strftime("%Y-%m-%d")
        vers = backtester.load_versions()
        for sym in self.bus.get("symbols", []):
            total_days = max(1, len(history.chain_days(sym)) or
                             len(history.index_days(sym, 250)))
            m_by_name = results.get(sym) or {}
            for name in ("bull_put_spread", "bear_call_spread"):
                m = m_by_name.get(name) or {}
                entry = backtester._symbol_entry(vers, name, sym)
                for ver in entry["versions"]:
                    if ver["v"] == entry["active"] and m.get("trades") is not None:
                        ver["results"] = m
                        ver["last_tested"] = backtester._now()
                trades = m.get("trades") or 0
                net_pnl = m.get("net_pnl") or 0
                profitable_now = trades >= min_conf and net_pnl > 0
                if entry.get("manually_disabled"):
                    entry["live_enabled"] = False
                else:
                    entry["live_enabled"] = profitable_now
                if not history.chain_days(sym):
                    continue   # spreads need real chain data; nothing to tune yet
                if profitable_now:
                    self.bus.log(self.name,
                                 f"{sym} {name}: v{entry['active']} already "
                                 f"profitable (₹{net_pnl:.0f}/{trades}t) — "
                                 "leaving parameters as-is")
                    continue
                tpd = trades / total_days
                if tpd < target:
                    direction, why = +1, (f"only {tpd:.2f} trades/day over "
                                          f"{total_days}d (target {target}) "
                                          "— relaxing entry filters")
                elif net_pnl < 0 and tpd > target * 2:
                    direction, why = -1, (f"net ₹{net_pnl:.0f} over "
                                          f"{total_days}d at {tpd:.2f} t/d "
                                          "— tightening")
                else:
                    continue
                if entry.get("tuning_exhausted"):
                    next_at = entry.get("next_tune_at")
                    if next_at and today_str < next_at:
                        continue
                    entry["tuning_exhausted"] = False
                    entry["tuning_attempts"] = 0
                last = entry["versions"][-1]
                tuned, changes = slib.tune(name, last["params"], direction)
                if not changes:
                    self.bus.log(self.name,
                                 f"{sym} {name}: {why} but already at filter "
                                 "bound")
                    continue
                new_m = backtester.metrics(
                    backtester.replay_spreads(sym, name, params=tuned))
                new_trades = new_m.get("trades") or 0
                new_pnl = new_m.get("net_pnl") or 0
                new_profitable = new_trades >= min_conf and new_pnl > 0
                worth_keeping = new_profitable or backtester.meaningful_improvement(
                    net_pnl, new_pnl, improve_thresh)
                if not worth_keeping:
                    entry["tuning_attempts"] = entry.get("tuning_attempts", 0) + 1
                    self.bus.log(self.name,
                                 f"{sym} {name}: candidate (₹{new_pnl:.0f}) "
                                 f"didn't clear the {improve_thresh*100:.0f}% "
                                 f"improvement bar over ₹{net_pnl:.0f} — not kept")
                    if entry["tuning_attempts"] >= max_attempts:
                        from datetime import timedelta
                        entry["tuning_exhausted"] = True
                        entry["next_tune_at"] = (now_ist() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                        self.bus.log(self.name,
                                     f"{sym} {name}: pausing auto-tuning "
                                     f"until {entry['next_tune_at']}")
                    continue
                newv = max(x["v"] for x in entry["versions"]) + 1
                entry["tuning_attempts"] = 0
                entry["versions"].append({
                    "v": newv, "params": tuned,
                    "reason": why + " | " + "; ".join(changes),
                    "created": backtester._now(), "last_tested": backtester._now(),
                    "results": new_m, "deployed": new_profitable})
                if new_profitable and not entry.get("manually_disabled"):
                    entry["active"] = newv
                    entry["live_enabled"] = True
                    self.bus.alert("medium", self.name, f"{sym}:{name}",
                                   f"{sym} {name} v{newv} is profitable "
                                   f"(₹{new_pnl:.0f}/{new_trades}t) — "
                                   "enabled for live trading")
                else:
                    self.bus.log(self.name,
                                 f"{sym} {name} v{newv} kept but NOT "
                                 f"enabled — net ₹{new_pnl:.0f} / {new_trades}t")
            backtester.save_versions(vers)
    def _tune_pa(self, results):
        """Adaptive gating, per (strategy, SYMBOL) independently — one
        index's tuning must never affect another's, since backtests can
        diverge sharply between them. Hard rule: a version is only
        marked live_enabled if its OWN backtest is net PROFITABLE with
        enough trades for the number to mean something; a "smaller loss"
        is never treated as good enough to trade live money on."""
        import backtester, pa_strategies as pa, history
        cfg = config.load()
        target = cfg.get("pa_min_trades_per_day", 0.3)
        min_trades_for_confidence = cfg.get("pa_min_trades_for_confidence", 15)
        improve_thresh = cfg.get("pa_tuning_improvement_threshold", 0.15)
        max_attempts = cfg.get("pa_tuning_max_attempts", 4)
        cooldown_days = cfg.get("pa_retune_cooldown_days", 7)
        today_str = now_ist().strftime("%Y-%m-%d")
        vers = backtester.load_versions()
        for sym in self.bus.get("symbols", []):
            total_days = max(1, len(history.index_days(sym, 250)))
            if total_days < 5:
                continue
            for name in pa.PA_NAMES:
                m = (results.get(sym) or {}).get(name) or {}
                entry = backtester._symbol_entry(vers, name, sym)
                # keep the active version's stored results current, so the
                # dashboard never shows "not yet backtested" once we have
                # real numbers, and so the profitability check below uses
                # the freshest data even when no new version is proposed
                for ver in entry["versions"]:
                    if ver["v"] == entry["active"] and m.get("trades") is not None:
                        ver["results"] = m
                        ver["last_tested"] = backtester._now()
                trades = m.get("trades") or 0
                net_pnl = m.get("net_pnl") or 0
                profitable_now = trades >= min_trades_for_confidence and net_pnl > 0
                entry["live_enabled"] = profitable_now and not entry.get("manually_disabled")
                if profitable_now:
                    # already working — don't keep tuning for more frequency
                    # at the risk of eroding a proven edge
                    self.bus.log(self.name,
                                 f"{sym} {name}: v{entry['active']} already "
                                 f"profitable (₹{net_pnl:.0f}/{trades}t) — "
                                 "leaving parameters as-is")
                    continue
                tpd = trades / total_days
                if tpd < target:
                    direction, why = +1, (f"only {tpd:.2f} trades/day over "
                                          f"{total_days}d (target {target}) "
                                          "— relaxing filters")
                elif net_pnl < 0 and tpd > target * 2:
                    direction, why = -1, (f"net ₹{net_pnl:.0f} over "
                                          f"{total_days}d at {tpd:.2f} t/d "
                                          "— tightening")
                else:
                    backtester.save_versions(vers)
                    continue
                # boundary: stop retrying every single day once several
                # attempts show no real improvement — wait out a cooldown
                # instead of spawning an endless chain of versions
                if entry.get("tuning_exhausted"):
                    next_at = entry.get("next_tune_at")
                    if next_at and today_str < next_at:
                        continue
                    entry["tuning_exhausted"] = False
                    entry["tuning_attempts"] = 0
                last = entry["versions"][-1]
                tuned, changes = pa.tune(name, last["params"], direction)
                if not changes:
                    self.bus.log(self.name,
                                 f"{sym} {name}: {why} but already at filter "
                                 "bound — no further tuning possible")
                    backtester.save_versions(vers)
                    continue
                new_m = backtester.metrics(
                    backtester.replay_pa(sym, name, params=tuned))
                new_trades = new_m.get("trades") or 0
                new_pnl = new_m.get("net_pnl") or 0
                new_profitable = (new_trades >= min_trades_for_confidence
                                  and new_pnl > 0)
                worth_keeping = new_profitable or backtester.meaningful_improvement(
                    net_pnl, new_pnl, improve_thresh)
                if not worth_keeping:
                    entry["tuning_attempts"] = entry.get("tuning_attempts", 0) + 1
                    self.bus.log(self.name,
                                 f"{sym} {name}: candidate (₹{new_pnl:.0f}) "
                                 f"didn't clear the {improve_thresh*100:.0f}% "
                                 f"improvement bar over ₹{net_pnl:.0f} — not "
                                 f"kept (attempt {entry['tuning_attempts']}/"
                                 f"{max_attempts})")
                    if entry["tuning_attempts"] >= max_attempts:
                        from datetime import timedelta
                        entry["tuning_exhausted"] = True
                        entry["next_tune_at"] = (now_ist() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                        self.bus.log(self.name,
                                     f"{sym} {name}: no improvement after "
                                     f"{max_attempts} attempts — pausing "
                                     f"auto-tuning until {entry['next_tune_at']}")
                    backtester.save_versions(vers)
                    continue
                newv = max(x["v"] for x in entry["versions"]) + 1
                entry["tuning_attempts"] = 0
                entry["versions"].append({
                    "v": newv, "params": tuned,
                    "reason": why + " | " + "; ".join(changes),
                    "created": backtester._now(), "last_tested": backtester._now(),
                    "results": new_m, "deployed": new_profitable})
                if new_profitable:
                    entry["active"] = newv
                    entry["live_enabled"] = True
                    self.bus.alert("medium", self.name, f"{sym}:{name}",
                                   f"{sym} {name} v{newv} is profitable "
                                   f"(₹{new_pnl:.0f}/{new_trades}t) — "
                                   "enabled for live trading")
                else:
                    self.bus.log(self.name,
                                 f"{sym} {name} v{newv} kept "
                                 f"({improve_thresh*100:.0f}%+ improvement to "
                                 f"₹{new_pnl:.0f}) but still not profitable "
                                 "enough for live trading")
                backtester.save_versions(vers)


class PriceActionAgent(Agent):
    """Live ORB / anchor-pullback / EMA-MTF setups on session candles.
    Emits standard BUY_CE/BUY_PE signals into the normal risk pipeline —
    capital gates (loss limits, caps, cooldowns) always apply; only the
    direction filters are adaptively tuned by the backtest agent."""
    name, interval = "price_action", 60

    def cycle(self):
        import backtester, pa_strategies as pa
        if not market_open():
            self.summary = "market closed"
            return
        cfg = config.load()
        enabled = cfg.get("pa_enabled", list(pa.PA_NAMES))
        if not enabled:
            self.summary = "disabled (pa_enabled empty)"
            return
        if not hasattr(self, "_taken"):
            self._taken, self._cool, self._day = {}, {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._cool, self._day = {}, {}, today
        fired = []
        skipped = {"no_position_free": 0, "stale_or_missing_pack": 0,
                  "no_analysis": 0, "on_cooldown": 0, "no_setup": 0}
        # Per-strategy breakdown added 2026-07-24: the aggregate
        # "no_setup" counter above made it structurally impossible to
        # tell WHICH of orb/vwap_pullback/ema_mtf was actually silent —
        # exactly the question this was built to answer. Kept the
        # aggregate too (existing consumers may read skipped["no_setup"]).
        no_setup_by_strategy = {name: 0 for name in enabled}
        positions = self.bus.get("positions", {}) or {}
        for sym in self.bus.get("symbols", []):
            if sym in positions:
                skipped["no_position_free"] += 1
                continue
            pack = self.bus.get(f"pa_candles:{sym}")
            if not pack or time.time() - pack["ts"] > 240:
                skipped["stale_or_missing_pack"] += 1
                continue
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis:
                skipped["no_analysis"] += 1
                continue
            for name in enabled:
                key = f"{sym}:{name}"
                if time.time() - self._cool.get(key, 0) < 1800:
                    skipped["on_cooldown"] += 1
                    continue
                if not cfg["paper_mode"] and not backtester.is_live_enabled(name, sym):
                    continue   # not yet proven profitable in backtest for THIS symbol
                p = backtester.get_params(name, sym)
                # Bug found 2026-07-24 from live logs: ema_mtf never fired
                # a single signal, on any day — not a market-conditions
                # issue as it first looked (vwap_pullback's confluence
                # rejections that same session made it plausible). Root
                # cause: c15_today was computed above but never stored in
                # pa_candles:{sym} (only c1/c5 were), AND this call passed
                # a hardcoded None as the 15-min candles argument
                # regardless. ema_mtf's mtf_confirm (the DEFAULT setting)
                # requires both c5 AND c15 to be present — with c15
                # always None, it bailed at that check on every single
                # call, permanently, independent of whether a real 5/13
                # EMA cross was happening. Both the missing storage and
                # this hardcoded None are now fixed.
                ev = pa.evaluate(name, pack["c1"], pack["c5"], pack.get("c15"),
                                 params=p, taken_today=self._taken.get(key, 0))
                if not ev:
                    skipped["no_setup"] += 1
                    no_setup_by_strategy[name] = no_setup_by_strategy.get(name, 0) + 1
                    continue
                leg = "ce" if ev["dir"] > 0 else "pe"
                row = next((r for r in analysis.get("strikes", [])
                            if r["strike"] == analysis.get("atm")), None)
                entry = row and row[leg].get("ltp")
                if not entry:
                    continue
                # Risk distance: fixed 15% by default, or ATR-scaled when
                # stop_mode="atr" (uses the regime engine's already-computed
                # atr_pct — underlying ATR as % of spot — scaled onto the
                # option premium via atr_stop_multiplier, since a live
                # per-strike ATR series isn't maintained). Clamped to a
                # 5-30% band so a near-zero or extreme ATR reading can't
                # produce a degenerate stop. target1 is kept at EXACTLY
                # 2x the risk distance (rr=2.0) regardless of mode — the
                # RiskAgent's risk-reward gate requires rr>=1.95, and a
                # naive fixed-ratio-independent-of-stop version would
                # silently reject every ATR-mode signal the same way an
                # earlier 20%/25% attempt did for the fixed-pct version.
                if cfg.get("stop_mode", "fixed_pct") == "atr":
                    regime = self.bus.get(f"regime:{sym}") or {}
                    atr_pct = regime.get("atr_pct")
                    if atr_pct:
                        risk_pct = min(0.30, max(0.05, atr_pct * cfg.get(
                            "atr_stop_multiplier", 2.5) / 100))
                    else:
                        risk_pct = 0.15  # no ATR reading yet — fixed fallback
                else:
                    risk_pct = 0.15
                sig = {"signal": "BUY_CE" if ev["dir"] > 0 else "BUY_PE",
                       "strike": analysis["atm"], "entry": entry,
                       "stoploss": round(entry * (1 - risk_pct), 2),
                       "target1": round(entry * (1 + risk_pct * 2), 2),
                       "target2": round(entry * (1 + risk_pct * 2.67), 2),
                       "spot_invalidation": round(ev["stop_spot"], 1),
                       "confidence": 74, "timeframe": "intraday",
                       "security_id": row[leg].get("security_id"),
                       "reasons": [f"[{name}] {ev['why']}"],
                       "source": name}
                self._taken[key] = self._taken.get(key, 0) + 1
                self._cool[key] = time.time()
                fired.append(f"{sym} {name} {sig['signal']}")
                self.bus.log(self.name,
                             f"{sym}: {name} -> {sig['signal']} ({ev['why']})")
                self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                            "analysis": analysis})
        self.summary = " · ".join(fired) if fired else \
            f"scanning {len(enabled)} setups across symbols ({skipped}, " \
            f"by strategy: {no_setup_by_strategy})"
        # Diagnostic breadcrumb every ~10 min when nothing fired — this is
        # exactly the visibility gap that made "why didn't ORB/vwap/ema
        # ever fire today" impossible to answer from the logs alone.
        if not fired and time.time() - getattr(self, "_last_diag_log", 0) > 600:
            self._last_diag_log = time.time()
            self.bus.log(self.name, f"no PA signals this cycle — {skipped} "
                         f"(no-setup by strategy: {no_setup_by_strategy})")


class MTFConfluenceAgent(Agent):
    """MACD+Stoch Confluence strategy (rinkoo.docx, 2026-07-23).
    Daily/weekly MTF confluence on MACD/RSI/Stochastic/Bollinger Bands
    -> BUY_CE/BUY_PE signal into the standard risk pipeline (same
    capital gates, position caps, daily loss limit etc. as every other
    signal source — this strategy gets no special exemption).

    Runs every 15 min during market hours — daily/weekly data doesn't
    change intraday, so this cadence is already far more often than
    the underlying data can meaningfully change; it just keeps the
    strategy responsive to a fresh signal appearing without hammering
    the historical-data endpoint (which also self-caches 6h internally
    in broker_adapter.py regardless).

    Requires Dhan as the active broker — historical_daily() (true
    daily-timeframe candles, needed for weekly MACD resampling) only
    exists on DhanClient today. Degrades to a clear "requires Dhan"
    status rather than erroring for other brokers.
    """
    name, interval = "mtf_confluence", 900

    def cycle(self):
        import mtf_confluence_strategy as mcs
        cfg = config.load()
        if not cfg.get("mtf_confluence_enabled", True):
            self.summary = "disabled (Settings -> Strategies)"
            return
        if not market_open():
            self.summary = "market closed"
            return
        if cfg.get("broker", "dhan") != "dhan":
            self.summary = "requires Dhan (historical_daily not yet built for other brokers)"
            return
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            self.summary = "no Dhan client available"
            # Worth a periodic log entry (not just self.summary) since
            # this is a genuinely surprising failure when broker=dhan
            # and the strategy is enabled — every other gate here
            # (disabled/market-closed/wrong-broker) is self-explanatory
            # and expected some of the time; this one specifically
            # means something is actually wrong with the broker client.
            if time.time() - getattr(self, "_last_diag_log", 0) > 1800:
                self._last_diag_log = time.time()
                self.bus.log(self.name, "no Dhan client available — "
                             "mtf_confluence_enabled and broker=dhan are "
                             "both set, but the client factory returned "
                             "nothing (check the Dhan connection itself)")
            return
        if not hasattr(self, "_taken"):
            self._taken, self._day = {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._day = {}, today

        positions = self.bus.get("positions", {}) or {}
        max_per_day = cfg.get("mtf_max_trades_per_day", 1)
        min_conf = cfg.get("mtf_min_confidence", 70)
        results = {}
        for sym in self.bus.get("symbols", []):
            if sym in positions:
                results[sym] = "position open"
                continue
            if self._taken.get(sym, 0) >= max_per_day:
                results[sym] = "max trades/day reached"
                continue
            try:
                candles = d.historical_daily(sym)["candles"]
            except Exception as e:
                results[sym] = f"data fetch failed: {e}"
                continue
            # Futures OI buildup (supportive-only, see mtf_confluence_
            # strategy.py's module docstring) — not yet wired into the
            # live loop (MarketDataAgent's hybrid feed only handles
            # index+option instruments today, see ROADMAP.md). Reading
            # a bus key that nothing populates yet is deliberate: it
            # degrades to None (unavailable) automatically the moment
            # that wiring lands, with no change needed here.
            future_buildup = self.bus.get(f"future_oi_trend:{sym}")
            global_sentiment = self.bus.get("global_risk_sentiment")
            result = mcs.evaluate(candles, future_buildup=future_buildup,
                                  global_sentiment=global_sentiment)
            self.bus.set(f"mtf_confluence:{sym}", result)   # Strategies-page visibility even when not firing
            if not result:
                results[sym] = "no confluence"
                continue
            if result["confidence"] < min_conf:
                results[sym] = f"confidence {result['confidence']} < {min_conf}"
                continue
            analysis = self.bus.get(f"chain:{sym}")
            if not analysis or not analysis.get("rows") or not analysis.get("spot"):
                results[sym] = "no chain data yet"
                continue
            atm = min(analysis["rows"], key=lambda r: abs(r["strike"] - analysis["spot"]))
            leg = "ce" if result["direction"] == "bullish" else "pe"
            entry = atm[leg].get("ltp")
            if not entry:
                results[sym] = "no ATM ltp"
                continue
            atr = result["daily_atr14"] or 0
            # SL distance for OPTIONS per rinkoo.docx's explicit note:
            # an ATM option's premium moves roughly HALF the index-point
            # distance (delta ~0.5), so the 1.5xATR stop buffer computed
            # in index points is halved when expressed as a premium
            # distance. Bug found in testing: an unclamped ATR-scaled
            # distance can exceed the ENTIRE premium when ATR is large
            # relative to that day's IV/premium (e.g. ATR=200 index pts
            # on a cheap 100-premium option -> a "150 premium" stop,
            # nonsensical since you can't lose more than you paid).
            # Clamped to 10%-60% of entry, same defensive-bounds pattern
            # already used for the fixed ATR-stop-mode elsewhere in this
            # codebase (single-leg positions' stop_mode="atr").
            sl_pts_index = 1.5 * atr if atr else entry * 0.30
            sl_pts_premium = sl_pts_index * 0.5
            sl_pts_premium = min(max(sl_pts_premium, entry * 0.10), entry * 0.60)
            stoploss = round(entry - sl_pts_premium, 2)
            risk = entry - stoploss
            if risk <= 0:
                results[sym] = "degenerate stop distance"
                continue
            target1 = round(entry + risk * 2, 2)     # rr=2.0, same convention as PA strategies
            target2 = round(entry + risk * 2.67, 2)
            sig = {
                "signal": "BUY_CE" if result["direction"] == "bullish" else "BUY_PE",
                "strike": atm["strike"], "entry": entry,
                "stoploss": stoploss, "target1": target1, "target2": target2,
                "confidence": result["confidence"], "timeframe": "swing",
                "security_id": atm[leg].get("security_id"),
                "reasons": [f"[mtf_confluence] {r}" for r in result["reasons"]],
                "source": "mtf_confluence",
                "atr": atr,   # carried through so ExecutionAgent.place()
                             # can use size_by_atr_risk() for this source
                "also_consider": mcs.RECOMMENDED_ACTIONS.get(result["direction"], []),
            }
            self._taken[sym] = self._taken.get(sym, 0) + 1
            self.bus.log(self.name,
                        f"{sym}: MTF confluence {result['direction']} "
                        f"(confidence {result['confidence']}) -> {sig['signal']} "
                        f"@ {atm['strike']} — also consider: "
                        f"{', '.join(sig['also_consider'])}")
            self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                        "analysis": analysis})
            results[sym] = f"FIRED {sig['signal']} conf={result['confidence']}"
        self.summary = "; ".join(f"{k}: {v}" for k, v in results.items()) or "no symbols configured"
        # Diagnostic breadcrumb added 2026-07-24: this agent previously
        # only wrote to the activity log when it actually fired a
        # signal — meaning a full day of silence gave zero visibility
        # into WHY (no qualifying setup all day — plausible, this is a
        # deliberately demanding 5-condition confluence — vs. a data/
        # config problem silently preventing evaluation entirely, e.g.
        # historical_daily() failing, or the broker/enabled gates
        # tripping). Same "why is X silent" gap already fixed for PA
        # strategies and spread auto-deploy; this agent had been missed.
        if not any("FIRED" in v for v in results.values()) and \
                time.time() - getattr(self, "_last_diag_log", 0) > 1800:
            self._last_diag_log = time.time()
            self.bus.log(self.name, f"no confluence fired this cycle — {results}")


AGENT_CLASSES = [MarketDataAgent, TechnicalAgent, RegimeAgent, NewsAgent,
                 SocialAgent, FundamentalAgent, StrategyAgent, RiskAgent,
                 ExecutionAgent, LearningAgent, BacktestAgent, PriceActionAgent,
                 MTFConfluenceAgent]
if NewsMacroAgent is not None:
    AGENT_CLASSES.append(NewsMacroAgent)


class Orchestrator:
    def __init__(self, get_chain, orders_factory):
        self.bus = Bus()
        self.ctx = {"get_chain": get_chain, "orders_factory": orders_factory}
        self.agents = []
        self.running = False
        # Restore historical trades so P&L view survives restarts/updates
        history = load_persisted_trades()
        self.bus.set("closed_trades", history)
        # Only "today's" trades count toward the daily cap
        today = now_ist().strftime("%Y-%m-%d")
        todays = [t for t in history
                  if str(t.get("closed_date", "")) == today
                  or str(t.get("opened", "")).startswith(today)]
        self.bus.set("trades_today", len(todays))
        if history:
            realized = sum(t.get("pnl", 0) for t in todays)
            self.bus.log("orchestrator",
                         f"restored {len(history)} historical trades "
                         f"({len(todays)} today, ₹{realized:.0f} realized today)")
        # Restore open positions/spreads so a restart (e.g. to apply an
        # update) doesn't silently lose track of anything currently open.
        # Data (premium, spot, etc.) captured at save time may now be
        # stale — agents re-fetch live prices on their next cycle as
        # normal, this just re-seeds WHICH positions exist to manage.
        open_positions, open_spreads = load_open_state()
        if open_positions:
            self.bus.set("positions", open_positions)
        if open_spreads:
            self.bus.set("spreads", open_spreads)
        if open_positions or open_spreads:
            self.bus.log("orchestrator",
                         f"restored {len(open_positions)} open position(s) and "
                         f"{len(open_spreads)} open spread(s) from before restart "
                         f"— re-validating live prices on next cycle")

    def start(self, symbol="NIFTY", symbols=None):
        symbols = [s.upper() for s in (symbols or
                   ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])]
        self.bus.set("symbols", symbols)
        self.bus.set("active_symbol", symbol.upper())
        if self.running:
            return self.status()
        self.bus.set("trades_today", self.bus.get("trades_today", 0))
        self.agents = [cls(self.bus, self.ctx) for cls in AGENT_CLASSES]
        for a in self.agents:
            a.start()
        self.running = True
        self.bus.log("orchestrator",
                     f"all {len(self.agents)} agents started on "
                     f"{'+'.join(symbols)} "
                     f"({'PAPER' if config.load()['paper_mode'] else 'LIVE'})")
        return self.status()

    def stop(self):
        for a in self.agents:
            a.stop_evt.set()
        self.running = False
        self.bus.log("orchestrator", "agents stopped")
        return self.status()

    def confirm_pending(self):
        """Manual confirmation of a risk-approved order."""
        job = self.bus.get("pending_confirmation")
        if not job:
            return {"error": "no risk-approved order pending"}
        ex = next((a for a in self.agents if a.name == "execution"), None)
        if not ex:
            return {"error": "execution agent not running"}
        ex.place(job, manual=True)
        return self.status()

    def exit_position(self, reason="manual exit from dashboard", symbol=None):
        ex = next((a for a in self.agents if a.name == "execution"), None)
        if ex:
            return {**(ex.exit(reason, symbol=symbol) or {}), **self.status()}
        # not running: nothing to exit
        return {"error": "agents not running", **self.status()}

    def manual_trade(self, symbol: str):
        """Manual 'Confirm & place': still goes through the risk agent."""
        if not self.running:
            return {"error": "Start the agents first — every order must "
                             "pass the risk agent."}
        # a risk-approved order may already be waiting
        if self.bus.get("pending_confirmation"):
            return self.confirm_pending()
        sym = symbol.upper()
        positions = self.bus.get("positions", {}) or {}
        cfg = config.load()
        if sym in positions:
            return {"error": f"Already have an open position on {sym}."}
        if len(positions) >= cfg.get("max_concurrent_positions", 1):
            return {"error": f"Max concurrent positions "
                             f"({cfg.get('max_concurrent_positions', 1)}) reached."}
        analysis = self.bus.get(f"analysis:{sym}")
        # Bug found 2026-07-22: two issues compounded into a confusing
        # "regime doesn't allow this" rejection for what looked like a
        # plain WAIT signal on screen.
        #   1) last_signal was a single GLOBAL bus key shared across
        #      every symbol, not namespaced like analysis:{sym} already
        #      is — checking one symbol's signal then confirming a
        #      DIFFERENT symbol could silently combine the wrong
        #      symbol's stale signal with the current symbol's analysis.
        #      Now reads the already-namespaced signal_cache:{sym}.
        #   2) the WAIT guard used a denylist (`sig["signal"] == "WAIT"`)
        #      instead of an allowlist. Any unexpected/malformed signal
        #      value that wasn't literally "WAIT" slipped straight past
        #      this guard into the risk agent, which then correctly
        #      rejected it for not being a real direction — but the UI's
        #      own label rendering defaults anything non-CE/PE to
        #      DISPLAY as "WAIT" too, so the user only ever saw what
        #      looked like a harmless WAIT card with an inexplicable
        #      rejection behind it.
        sig = self.bus.get(f"signal_cache:{sym}")
        if not (analysis and sig) or sig.get("signal") not in ("BUY_CE", "BUY_PE"):
            return {"error": "No actionable signal — press Get signal first."}
        job = {"symbol": sym, "signal": sig, "analysis": analysis}
        risk = next((a for a in self.agents if a.name == "risk"), None)
        ex = next((a for a in self.agents if a.name == "execution"), None)
        ok, checks = risk.evaluate(job)
        self.bus.set("last_risk_check",
                     {"verdict": "APPROVED" if ok else "REJECTED",
                      "checks": checks})
        if not ok:
            self.bus.log("risk", "REJECTED manual order — " + " · ".join(checks))
            return {"error": "Risk agent rejected the order",
                    "checks": checks}
        ex.place(job, manual=True)
        return self.status()

    def webhook_signal(self, symbol, direction, strategy_name="tradingview",
                       atr=None, confidence=70):
        """Turn a TradingView webhook alert into an actual option trade.

        TradingView's Pine Script strategies compute everything in
        INDEX POINTS (its own candle engine, its own indicators) — it
        has no concept of option strikes/premiums/security_ids. This
        method does the SAME translation MTFConfluenceAgent already
        does: pick the current ATM strike from the live chain, size
        the stop/target in premium terms (ATR-scaled by delta=0.5 for
        an ATM option if a Pine-computed ATR was sent, else a sane
        fixed-% fallback), then route through the IDENTICAL risk
        pipeline every other signal source uses — no special exemption
        for a webhook-sourced signal, same as every strategy in this
        codebase.

        Not called directly from an agent thread — this runs on the
        FastAPI request thread when the webhook POST arrives, so it
        must be safe to call anytime (same expectation as
        MarketDataAgent._on_ws_tick, which also runs off its own
        thread)."""
        if not self.running:
            return {"error": "Agents aren't running — start them first."}
        sym = symbol.upper()
        direction = direction.lower()
        if direction not in ("bullish", "bearish", "buy", "sell", "long", "short"):
            return {"error": f"Unrecognized direction {direction!r} — expected "
                             f"bullish/bearish (or buy/sell, long/short)"}
        bullish = direction in ("bullish", "buy", "long")
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            return {"error": f"Already have an open position on {sym} — "
                             f"webhook signal not acted on"}
        analysis = self.bus.get(f"analysis:{sym}")
        chain = self.bus.get(f"chain:{sym}")
        if not chain or not chain.get("rows") or not chain.get("spot"):
            return {"error": f"No live chain data for {sym} yet"}
        atm = min(chain["rows"], key=lambda r: abs(r["strike"] - chain["spot"]))
        leg = "ce" if bullish else "pe"
        entry = atm[leg].get("ltp")
        if not entry:
            return {"error": f"No ATM {leg.upper()} price available for {sym}"}
        # Same ATR-scaled premium-stop approach as MTFConfluenceAgent,
        # including the same sanity clamp (a bug found there earlier:
        # an unclamped ATR-scaled distance can exceed the ENTIRE
        # premium when ATR is large relative to that day's IV).
        sl_pts_index = 1.5 * atr if atr else entry * 0.30
        sl_pts_premium = sl_pts_index * 0.5
        sl_pts_premium = min(max(sl_pts_premium, entry * 0.10), entry * 0.60)
        stoploss = round(entry - sl_pts_premium, 2)
        risk = entry - stoploss
        if risk <= 0:
            return {"error": "Degenerate stop distance — refusing to trade"}
        sig = {
            "signal": "BUY_CE" if bullish else "BUY_PE",
            "strike": atm["strike"], "entry": entry, "stoploss": stoploss,
            "target1": round(entry + risk * 2, 2),
            "target2": round(entry + risk * 2.67, 2),
            "confidence": confidence, "timeframe": "swing",
            "security_id": atm[leg].get("security_id"),
            "reasons": [f"[tradingview:{strategy_name}] webhook alert, "
                       f"direction={direction}" + (f", atr={atr}" if atr else "")],
            "source": f"tradingview_{strategy_name}",
            "atr": atr,
        }
        job = {"symbol": sym, "signal": sig, "analysis": analysis or {}}
        risk_agent = next((a for a in self.agents if a.name == "risk"), None)
        ex = next((a for a in self.agents if a.name == "execution"), None)
        ok, checks = risk_agent.evaluate(job)
        self.bus.log("risk", f"TradingView webhook ({strategy_name}) {sym} "
                            f"{'APPROVED' if ok else 'REJECTED'} — " +
                            " · ".join(checks))
        if not ok:
            return {"error": "Risk agent rejected the webhook signal",
                    "checks": checks}
        ex.place(job, manual=False)
        return {"ok": True, "symbol": sym, "signal": sig["signal"],
                "strike": sig["strike"], "entry": entry}

    def status(self):
        cfg = config.load()
        import sizing
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        total_capital = cfg.get("backtest_capital", 200000)
        capital_used = sizing.deployed_capital(cfg, positions, spreads)
        today = now_ist().strftime("%Y-%m-%d")
        closed_today = [t for t in self.bus.get("closed_trades", [])
                        if str(t.get("closed_date", "")) == today
                        or str(t.get("opened", "")).startswith(today)]
        realized_today = sum(t.get("pnl", 0) for t in closed_today)
        unrealized_today = (sum(p.get("pnl", 0) for p in positions.values())
                            + sum(sp.get("pnl", 0) for sp in spreads.values()))
        return {
            "running": self.running,
            "symbol": self.bus.get("active_symbol"),
            "market_open": market_open(),
            "paper_mode": cfg["paper_mode"],
            "auto_execute": cfg["auto_execute"],
            "trades_today": self.bus.get("trades_today", 0),
            "max_trades_per_day": cfg["max_trades_per_day"],
            "agents": [a.info() for a in self.agents],
            "symbols": self.bus.get("symbols"),
            "ticker": self.bus.get("ticker", {}),
            "position": self.bus.get("position"),
            "positions": positions,
            "spreads": spreads,
            "max_concurrent_positions": cfg.get("max_concurrent_positions", 1),
            "last_signal": self.bus.get("last_signal"),
            "last_risk_check": self.bus.get("last_risk_check"),
            "pending_confirmation": bool(self.bus.get("pending_confirmation")),
            "news": self.bus.get("news"),
            "social": self.bus.get("social"),
            "macro": self.bus.get("macro"),
            "journal_latest": self.bus.get("journal_latest"),
            "ai_budget": _ai_budget(),
            "refreshed_at": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "risk_halted": any(getattr(a, "halted", False) for a in self.agents),
            "consecutive_losses": next(
                (a.consecutive_losses for a in self.agents
                 if a.name == "risk"), 0),
            "storage_dir": STORE_DIR,
            "regime": self.bus.get(f"regime:{self.bus.get('active_symbol')}"),
            "regimes": {s: self.bus.get(f"regime:{s}")
                        for s in self.bus.get("symbols", [])
                        if self.bus.get(f"regime:{s}")},
            "alerts": list(self.bus.alerts)[-30:][::-1],
            "log": list(self.bus.feed)[-50:],
            "capital": {
                "total": total_capital,
                "used": round(capital_used, 0),
                "remaining": round(max(0.0, total_capital - capital_used), 0),
                "day_pnl": round(realized_today + unrealized_today, 0),
                "day_pnl_realized": round(realized_today, 0),
                "day_pnl_unrealized": round(unrealized_today, 0),
            },
        }
