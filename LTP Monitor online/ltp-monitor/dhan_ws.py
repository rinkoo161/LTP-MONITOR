"""dhan_ws.py — persistent Dhan Live Market Feed websocket client.

Wraps the OFFICIAL `dhanhq` package's MarketFeed class
(github.com/dhan-oss/DhanHQ-py, PyPI: dhanhq) rather than hand-rolling
the binary protocol — Dhan's feed is well-documented and the library is
first-party, so unlike the Kotak websocket (reverse-engineered from a
JS bundle) there is no protocol-decoding risk here.

Feeds live ticks into the same bus keys the REST polling path already
populates ("chain:{symbol}"), so every existing consumer (analyzer,
strategies, dashboard) works unchanged regardless of whether data
arrives via REST or websocket. See merge_tick_into_chain() below for
the exact field mapping — built directly from the leg schema in
broker_adapter.py's _leg() helper so REST-only fields (iv, delta,
theta, gamma, vega — the feed doesn't carry greeks) are preserved
across a websocket update rather than being wiped out.

HONEST STATUS: built by installing the actual `dhanhq` package
(v2.2.0) and reading its marketfeed.py source directly — the response
dict field names below (LTP, OI, security_id, exchange_segment, etc.)
are copied from process_full()/process_quote() in that file, not
guessed from documentation. This has NOT been run against a live Dhan
account/connection. Run test_dhan_ws.py FIRST with real credentials
and read its output before trusting this for live trading — same
validation discipline used for the Kotak websocket integration.

Dependencies (see requirements.txt): `dhanhq` pulls in pandas,
pyOpenSSL, and websockets (asyncio-based — a different package from
websocket-client, which the Kotak client uses; both coexist fine).

LIVE VALIDATION (2026-07-23): confirmed against a real Dhan account.
NIFTY option leg (23850 CE) streamed correct real-time LTP (147.5),
OI (3,479,775), and bid/ask (146.55/147.2) within seconds — this is
exactly the data the OI-wall/credit-spread strategies need. NIFTY
index itself also confirmed streaming (LTP only, Ticker mode — see
add_index_instrument()'s docstring for the Full-vs-Ticker bug this
surfaced and fixed).

DESIGN REVIEW — the rest of Dhan's data-API surface (2026-07-23):
this system uses four Dhan data surfaces total; here's what each is
for and why the other two websocket-capable surfaces are NOT part of
this build yet.

  - Live Market Feed (websocket) — THIS FILE. Built and validated above.
  - Option Chain (REST only, no websocket variant exists) — already
    used via broker_adapter.py's option_chain(). Gives the periodic
    snapshot: full strike list, security_id per leg, IV, greeks, OI.
    The right architecture is exactly what's built here: REST for the
    slower-changing shape (new strikes as spot moves, IV/greeks), this
    websocket overlaying fast-changing fields (LTP/OI/depth) on top via
    merge_tick_into_chain() — never replacing the REST call, since
    greeks aren't available over the feed at all.
  - Historical Data (REST only, no websocket variant exists — it's
    inherently backward-looking) — already used via broker_adapter.py
    for RegimeAgent/backtester. Unaffected by this work.
  - Full Market Depth, 20/200-level (a SEPARATE websocket:
    wss://depth-api-feed.dhan.co/twentydepth, up to 50 instruments,
    or /twohundreddepth, 1 instrument per connection) — deliberately
    NOT built. Two reasons: (1) Dhan's own docs state "Only NSE Equity
    and Derivatives segments are enabled" — BSE segments are excluded,
    meaning SENSEX (BSE_FNO) would have no depth coverage while
    NIFTY/BANKNIFTY/FINNIFTY (NSE_FNO) would, breaking the same-logic-
    across-all-four-symbols design this whole system is built on;
    (2) the OI-wall/credit-spread strategies price off best bid/ask,
    which the 5-level depth already bundled in this Full-mode Live
    Feed packet already provides — 20+ levels adds real-order-book
    detail (useful for detecting liquidity/demand-supply zones) that
    nothing in the current strategy set actually consumes. Worth
    revisiting if a future strategy specifically needs deep-book
    analysis, not as a default "more data is better" addition.
  - Futures — this system trades OPTIONS ONLY; there is no futures
    position type, P&L accounting, or strategy anywhere in agents.py/
    strategies.py/pa_strategies.py. Dhan's option_chain() REST endpoint
    does not return futures contracts at all — a futures security_id
    would need a separate scrip-master CSV lookup (INSTRUMENT=FUTIDX,
    matched by UNDERLYING_SECURITY_ID + expiry). Building futures data
    streaming with no strategy to consume it would be speculative
    scope creep. This is a strategy-level decision (do you want to
    trade/reference futures at all?), not a data-plumbing gap — flag
    it explicitly if wanted, and the scrip-master lookup path above is
    the concrete next step at that point.
"""
import threading
import time

try:
    from dhanhq import DhanContext, MarketFeed
except ImportError:
    DhanContext = None
    MarketFeed = None

# Exchange segment constants (from the installed package's marketfeed.py —
# also matches the official DhanHQ v2 API annexure exactly):
#   IDX=0, NSE=1, NSE_FNO=2, NSE_CURR=3, BSE=4, MCX=5, BSE_CURR=7, BSE_FNO=8
# NIFTY/BANKNIFTY/FINNIFTY options trade on NSE_FNO; SENSEX options on
# BSE_FNO (SENSEX itself is a BSE index) — this mirrors UNDERLYINGS/segment
# handling already established in broker_adapter.py for the REST path.
SEGMENT_FOR_SYMBOL = {
    "NIFTY": "NSE_FNO", "BANKNIFTY": "NSE_FNO", "FINNIFTY": "NSE_FNO",
    "MIDCPNIFTY": "NSE_FNO", "SENSEX": "BSE_FNO",
}

# The underlying INDEX itself (not an option leg) — IDX_I segment,
# permanent security_ids that never expire/change, unlike option legs
# which are per-strike-per-expiry. Same values as UNDERLYINGS in
# broker_adapter.py. Useful for a zero-setup connectivity smoke test:
# no option-chain lookup needed, and the index always trades when the
# market is open, so a missing tick means a connection problem, not a
# stale/wrong strike.
INDEX_SECURITY_ID = {
    "NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27",
    "MIDCPNIFTY": "442", "SENSEX": "51",
}


class DhanWebsocketClient:
    """One persistent connection subscribed to a set of option-leg
    instruments (by security_id). Call add_instrument() for each strike
    leg you want live ticks for, then start() once.

    MarketFeed.start() already manages its own background thread and
    has an internal reconnect-on-drop loop (_run_async) — we don't need
    to reimplement that (unlike the Kotak client, which had to build
    reconnect/heartbeat by hand against a reverse-engineered protocol).
    We add only an outer error-cooldown so a persistently failing
    token/account doesn't spin retrying forever against Dhan's servers.
    """

    MAX_ERRORS_BEFORE_COOLDOWN = 5
    COOLDOWN_SECS = 300

    def __init__(self, client_id, access_token, on_tick=None, on_status=None,
                verbose=False):
        if MarketFeed is None:
            raise RuntimeError(
                "dhanhq package not installed — run: pip install dhanhq")
        self.on_tick = on_tick or (lambda sym, sec_id, tick: None)
        self.on_status = on_status or (lambda msg: None)
        self.verbose = verbose
        self._dhan_context = DhanContext(client_id, access_token)
        self._feed = None
        self._thread = None
        self._instruments = []      # [(segment_str, security_id_str, mode_int), ...]
        # v58.41 — chunked subscription. See subscribe_more().
        self._pending_subs = []
        self._sub_lock = threading.Lock()
        self._flusher = None
        self.subscribe_chunk_size = 100      # Dhan's documented per-message cap
        self.subscribe_delay_ms = 250
        self.skip_option_symbols = ("SENSEX",)   # never ticked; see subscribe_more()
        self._sec_to_sym = {}       # int(security_id) -> our symbol name
        self._error_count = 0
        self._cooling_down = threading.Event()
        self._connected = threading.Event()

    def add_instrument(self, symbol, security_id):
        """Register an option-leg security_id to subscribe to, in Full
        (LTP + OI + depth) mode. Call before start(); for a live
        connection use subscribe_more() instead."""
        segment = SEGMENT_FOR_SYMBOL.get(symbol.upper())
        if not segment:
            raise ValueError(f"no exchange segment mapping for symbol {symbol!r}")
        sid = str(security_id)
        self._instruments.append((segment, sid, MarketFeed.Full))
        self._sec_to_sym[int(security_id)] = symbol.upper()

    def add_index_instrument(self, symbol):
        """Subscribe to the underlying INDEX itself (IDX_I segment,
        permanent security_id — see INDEX_SECURITY_ID) rather than an
        option leg. No option-chain lookup needed; useful as a
        zero-setup connectivity smoke test since the index always
        trades when the market is open.

        Bug found 2026-07-23: this used to subscribe in Full mode
        (same as option legs) — Full's payload includes market depth
        and OI, neither of which exist for an index (it's not a traded
        instrument; no order book, no open interest). Live test showed
        the option leg subscribed alongside it streamed perfectly
        (correct LTP+OI) while the index produced zero ticks in 20s
        despite a clean "connected" ack — consistent with Dhan's server
        having nothing to send for that request/instrument combination.
        Now uses Ticker mode (LTP + LTT only), which is what an index
        actually has."""
        symbol = symbol.upper()
        sid = INDEX_SECURITY_ID.get(symbol)
        if not sid:
            raise ValueError(f"no index security_id known for symbol {symbol!r}")
        self._instruments.append(("IDX_I", sid, MarketFeed.Ticker))
        self._sec_to_sym[int(sid)] = symbol

    def add_future_instrument(self, symbol, security_id=None):
        """Subscribe to the CURRENT-MONTH futures contract for
        `symbol`, in Full mode (futures have real OI and depth, same
        as option legs — this is NOT the index case above).

        Unlike option legs (looked up per-strike from the REST option
        chain, which is already fetched every cycle) and the index
        (a permanent security_id), a future's security_id changes
        every month and there is no formula for it — it must be looked
        up from Dhan's scrip master CSV. If `security_id` isn't passed
        explicitly, this calls dhan_scrip_master.get_current_future()
        to resolve it dynamically (self-updating through every monthly
        rollover with no manual maintenance).

        HONEST STATUS: dhan_scrip_master.py's CSV-parsing logic is
        tested against a constructed sample, not the live file (this
        sandbox can't reach images.dhan.co) — see that module's
        docstring. Run test_dhan_scrip_master.py against the real file
        before relying on the auto-lookup path here for live trading.
        """
        symbol = symbol.upper()
        segment = SEGMENT_FOR_SYMBOL.get(symbol)
        if not segment:
            raise ValueError(f"no exchange segment mapping for symbol {symbol!r}")
        if security_id is None:
            import dhan_scrip_master as dsm
            future, detail = dsm.get_current_future_detailed(symbol)
            if not future:
                raise RuntimeError(f"could not resolve current future for "
                                   f"{symbol}: {detail}")
            security_id = future["security_id"]
            self.on_status(f"{symbol} future resolved to "
                          f"{future.get('symbol_name', '?')} "
                          f"(security_id={security_id}, "
                          f"expiry={future['expiry'].date()})")
        sid = str(security_id)
        self._instruments.append((segment, sid, MarketFeed.Full))
        self._sec_to_sym[int(sid)] = symbol

    def start(self):
        """Connect and subscribe in a MarketFeed-managed background
        thread. Returns that thread (daemon=True, matches the Kotak
        client's start() contract)."""
        if not self._instruments:
            raise RuntimeError("no instruments added — call add_instrument() first")
        # MarketFeed wants (exchange_segment_enum_int, security_id, mode) —
        # but its own get_exchange_segment() maps int->string, and the
        # constructor stores our tuples as-is for validate_and_process_tuples,
        # which expects (exchange, instrument_id) where "exchange" is
        # whatever we pass through to get_exchange_segment(exchange_code) —
        # that function is only ever called with the INT segment codes
        # (0/1/2/...), so we must pass the int form, not the string.
        seg_int = {"NSE_FNO": MarketFeed.NSE_FNO, "BSE_FNO": MarketFeed.BSE_FNO,
                  "IDX_I": MarketFeed.IDX}
        instruments = [(seg_int[seg], sid, mode)
                       for seg, sid, mode in self._instruments]
        # 2026-07-25 — diagnostic added per live report: only NIFTY was
        # ever receiving live ticks despite all 4 indices showing
        # "connected — 4 instrument(s) subscribed". Inspected the real
        # dhanhq 2.2.0 source directly (not guessed): the subscription
        # batching in validate_and_process_tuples/subscribe_instruments
        # correctly keeps distinct security_ids as separate entries (no
        # accidental dedup across different IDs), so a client-side
        # batching bug looks unlikely from the library code alone — but
        # that can't be confirmed without seeing which security_ids
        # actually arrive over the wire. Logging the exact resolved
        # subscription list here, plus a per-security_id first-tick
        # tracker below, so the NEXT live capture shows definitively
        # whether Dhan's server is sending ticks for all 4 index IDs at
        # all (server/account-side gap) or the client is receiving but
        # mis-routing them (would show up as ticks for unmapped
        # security_ids in _handle_tick).
        self._seen_sec_ids = set()
        self.on_status(f"subscribing: {[(seg, sid, mode) for seg, sid, mode in self._instruments]}")

        def _on_message(feed, data):
            self._handle_tick(data)

        def _on_error(feed, err):
            self._error_count += 1
            self.on_status(f"error: {err}")
            if self._error_count >= self.MAX_ERRORS_BEFORE_COOLDOWN:
                self.on_status(f"{self._error_count} errors — cooling down "
                              f"{self.COOLDOWN_SECS}s before further retries")
                self._cooling_down.set()
                time.sleep(self.COOLDOWN_SECS)
                self._error_count = 0
                self._cooling_down.clear()

        def _on_connect(feed):
            self._error_count = 0
            self._connected.set()
            self.on_status(f"connected — {len(instruments)} instrument(s) subscribed")
            # Bug found from a live log 2026-07-25: the original version
            # of this check compared against self._sec_to_sym LIVE, 30s
            # later — but that dict keeps growing after connect as
            # subscribe_more() adds option legs and futures contracts
            # (which can land just seconds before the 30s mark). A
            # contract subscribed 3s before the check fired was flagged
            # as "never received a tick", which isn't a fair test — it
            # never had time to. Fixed: snapshot exactly the security_
            # ids that were part of THIS start() call (the index
            # instruments) before anything else gets added, and check
            # coverage against that fixed snapshot only.
            expected_at_connect = dict(self._sec_to_sym)
            def _coverage_check():
                time.sleep(30)
                missing = {sec: sym for sec, sym in expected_at_connect.items()
                          if sec not in self._seen_sec_ids}
                seen_syms = sorted({self._sec_to_sym.get(s) for s in
                                    self._seen_sec_ids & set(expected_at_connect)})
                if missing:
                    self.on_status(f"tick coverage after 30s (initial "
                                  f"subscription only, {len(expected_at_connect)} "
                                  f"instruments): received for {seen_syms}, "
                                  f"NEVER received for {list(missing.values())} "
                                  f"(security_ids {list(missing.keys())})")
                else:
                    self.on_status(f"tick coverage after 30s: all "
                                  f"{len(expected_at_connect)} initially-"
                                  f"subscribed instruments received at "
                                  f"least one tick")
            threading.Thread(target=_coverage_check, daemon=True).start()

        def _on_close(feed):
            self._connected.clear()
            self.on_status("connection closed")

        self._feed = MarketFeed(
            self._dhan_context, instruments, version="v2",
            on_connect=_on_connect, on_message=_on_message,
            on_close=_on_close, on_error=_on_error,
        )
        self._thread = self._feed.start()
        return self._thread

    def is_connected(self):
        """True only once on_connect has actually fired. Bug found
        2026-07-24 while wiring the hybrid MarketDataAgent path:
        MarketFeed.subscribe_symbols() silently no-ops if the websocket
        isn't open yet (`if self.ws and not self._is_ws_closed()`) — it
        does NOT queue the request for once the connection comes up.
        A caller adding instruments via subscribe_more() right after
        start() (self._feed being non-None) could have that request
        silently dropped forever, with no error and no signal that it
        happened. Callers should check this before subscribe_more()."""
        return self._connected.is_set()

    def subscribe_more(self, symbol, security_id):
        """Queue an instrument for subscription on an open connection.

        2026-07-29 — this used to send ONE websocket frame per
        instrument, synchronously, on every call. A live log showed the
        subscription reaching 2,080 instruments, then:

            09:26:01  ws: error: no close frame received or sent
            09:26:03  ws: connected — 4 instrument(s) subscribed

        i.e. the server tore the connection down without a close
        handshake, and the in-flight send onto the dead socket is the
        `socket.send() raised exception` seen on the console. Dhan
        documents ~100 instruments per subscribe message; two thousand
        individual frames in a burst is well outside that.

        Requests are now BUFFERED and flushed in chunks by a background
        thread. Return value keeps its original contract — True means
        "accepted and will be sent", False means "dropped, do not treat
        as subscribed" — because callers rely on it.
        """
        symbol = symbol.upper()
        segment = SEGMENT_FOR_SYMBOL.get(symbol)
        if not segment or self._feed is None or not self.is_connected():
            return False
        if symbol in (self.skip_option_symbols or ()):
            # ~115 SENSEX option instruments were subscribed and NEVER
            # produced a single tick, while consuming subscription slots
            # and contributing to the burst that drops the connection.
            return False
        seg_int = {"NSE_FNO": MarketFeed.NSE_FNO, "BSE_FNO": MarketFeed.BSE_FNO}[segment]
        self._sec_to_sym[int(security_id)] = symbol
        with self._sub_lock:
            self._pending_subs.append((seg_int, str(security_id), MarketFeed.Full))
            self._start_flusher()
        return True

    def _start_flusher(self):
        """Lazily start the chunked-subscription flusher. Caller must
        already hold _sub_lock."""
        if self._flusher and self._flusher.is_alive():
            return
        self._flusher = threading.Thread(target=self._flush_subs, daemon=True,
                                         name="dhan-ws-sub-flush")
        self._flusher.start()

    def _flush_subs(self):
        """Send buffered subscriptions in chunks, pausing between them.

        Deliberately tolerant: a failed chunk is logged and the rest are
        still attempted, because losing one chunk is far better than
        losing the whole feed — which is what the unbatched version did.
        """
        while True:
            time.sleep(self.subscribe_delay_ms / 1000.0)
            with self._sub_lock:
                if not self._pending_subs:
                    self._flusher = None
                    return
                chunk = self._pending_subs[:self.subscribe_chunk_size]
                del self._pending_subs[:self.subscribe_chunk_size]
            if self._feed is None or not self.is_connected():
                continue
            try:
                self._feed.subscribe_symbols(chunk)
            except Exception as e:
                self.on_status(f"subscribe chunk of {len(chunk)} failed "
                               f"({type(e).__name__}: {e}) — remaining chunks "
                               "still queued")

    def stop(self):
        if self._feed:
            self._feed.close_connection()

    def _handle_tick(self, data):
        """data is the dict returned by MarketFeed.process_full() /
        process_quote() / process_ticker() — see marketfeed.py in the
        installed dhanhq package. Full/Quote (option legs) carry
        OI+depth; Ticker (index instruments, see add_index_instrument)
        carries only LTP+LTT — both produce a usable tick here, just
        with fewer fields for Ticker."""
        if not data or not isinstance(data, dict):
            return
        if data.get("type") not in ("Full Data", "Quote Data", "Ticker Data"):
            return   # ignore OI-only/depth-only/status packets for now
        sec_id = data.get("security_id")
        sym = self._sec_to_sym.get(sec_id)
        if sym is None:
            # 2026-07-25 — log once per unexpected security_id rather
            # than silently dropping it, so a client-side routing bug
            # (tick arrives but doesn't match our expected int key,
            # e.g. a type mismatch) is distinguishable from Dhan's
            # server genuinely never sending anything for that
            # instrument in the first place.
            unmapped = getattr(self, "_unmapped_sec_ids_logged", None)
            if unmapped is None:
                unmapped = set()
                self._unmapped_sec_ids_logged = unmapped
            if sec_id not in unmapped:
                unmapped.add(sec_id)
                self.on_status(f"⚠ tick received for unmapped security_id "
                              f"{sec_id!r} ({type(sec_id).__name__}) — not in "
                              f"our subscribed set, dropped")
            return
        seen = getattr(self, "_seen_sec_ids", None)
        if seen is not None and sec_id not in seen:
            seen.add(sec_id)
        try:
            ltp = float(data.get("LTP", 0) or 0)
        except (TypeError, ValueError):
            return
        tick = {
            "security_id": sec_id,
            "ltp": ltp,
            "oi": data.get("OI"),
            "volume": data.get("volume"),
            "bid": None, "ask": None,   # populated below if depth present
        }
        depth = data.get("depth")
        if depth:
            # best bid/ask = first level of the 5-level depth array
            try:
                tick["bid"] = float(depth[0]["bid_price"])
                tick["ask"] = float(depth[0]["ask_price"])
            except (KeyError, IndexError, ValueError, TypeError):
                pass
        if self.verbose:
            self.on_status(f"{sym} [{sec_id}] LTP={tick['ltp']} OI={tick['oi']}")
        self.on_tick(sym, sec_id, tick)


def merge_tick_into_chain(chain, security_id, tick):
    """Update a REST-fetched `chain:{symbol}` dict in place with a live
    websocket tick, WITHOUT touching REST-only fields the feed doesn't
    carry (iv, delta/theta/gamma/vega, oi_chg needs previous_oi context
    the feed doesn't give us either — left untouched, only refreshed on
    the next REST snapshot).

    This is the integration point a future MarketDataAgent change would
    call from its tick-received callback; not yet wired into the live
    agent loop — see the module docstring's HONEST STATUS note.
    Returns True if a matching leg was found and updated, else False.
    """
    if not chain or not chain.get("rows"):
        return False
    for row in chain["rows"]:
        for leg_key in ("ce", "pe"):
            leg = row.get(leg_key)
            if leg and leg.get("security_id") == security_id:
                leg["ltp"] = tick["ltp"]
                if tick.get("oi") is not None:
                    leg["oi"] = tick["oi"]
                if tick.get("volume") is not None:
                    leg["volume"] = tick["volume"]
                if tick.get("bid") is not None:
                    leg["bid"] = tick["bid"]
                if tick.get("ask") is not None:
                    leg["ask"] = tick["ask"]
                return True
    return False
