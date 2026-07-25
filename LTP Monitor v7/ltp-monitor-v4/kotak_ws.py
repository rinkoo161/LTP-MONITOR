"""kotak_ws.py — persistent Kotak Neo market-data websocket client.

Feeds live ticks directly into the same bus keys the REST polling
path already populates ("chain:{symbol}"), so every existing consumer
(analyzer, strategies, dashboard) works unchanged regardless of
whether data arrives via REST or websocket.

HONEST STATUS: the wire protocol (kotak_ws_protocol.py) is reverse-
engineered from Kotak's own JS library, unit-tested for internal
consistency, but NOT yet confirmed against a live connection. Run
test_kotak_ws.py FIRST and read its output before trusting this for
live trading — same validation discipline as the REST integration.
"""
import json
import threading
import time

import websocket

import config
import kotak_ws_protocol as proto

WS_URL = "wss://mlhsm.kotaksecurities.com"
HEARTBEAT_SECS = 30
MAX_RETRIES_BEFORE_COOLDOWN = 5
COOLDOWN_SECS = 300


class KotakWebsocketClient:
    """One persistent connection, subscribed to a set of instruments.
    Call start() once; it manages its own reconnect loop in a
    background thread. on_tick(token, tick_dict) is called for every
    decoded tick.
    """

    def __init__(self, on_tick=None, on_status=None, verbose=False,
                resume_on_ack=True):
        self.verbose = verbose
        self._resume_on_ack = resume_on_ack
        self._data_frames_logged = 0
        self._resumed_channels = set()
        self.on_tick = on_tick or (lambda tok, tick: None)
        self.on_status = on_status or (lambda msg: None)
        self._ws = None
        self._thread = None
        self._heartbeat_thread = None
        self._stop = threading.Event()
        self._buf = bytearray()
        self._subscriptions = {}   # channel_num -> (scrips, prefix)
        self._connected = threading.Event()
        self._retry_count = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        """Join background threads before returning, so the caller can
        safely let the process exit right after — without this, a
        daemon thread waking mid-shutdown to write a status line can
        race with interpreter teardown and crash with a fatal I/O
        error (confirmed 2026-07-20: '_enter_buffered_busy' on exit)."""
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run_forever(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
                self._retry_count = 0
            except Exception as e:
                self.on_status(f"connection error: {e}")
            if self._stop.is_set():
                return
            self._retry_count += 1
            if self._retry_count >= MAX_RETRIES_BEFORE_COOLDOWN:
                self.on_status(f"{MAX_RETRIES_BEFORE_COOLDOWN} failed attempts — "
                              f"cooling down {COOLDOWN_SECS}s before retrying")
                self._stop.wait(COOLDOWN_SECS)
                self._retry_count = 0
            else:
                self._stop.wait(min(30, 2 ** self._retry_count))

    def _connect_once(self):
        cfg = config.load()
        token = cfg.get("kotak_session_token") or cfg.get("kotak_access_token")
        sid = cfg.get("kotak_sid")
        if not token or not sid:
            raise RuntimeError("no Kotak session token/sid in config — "
                              "run kotak_login.py first")
        self._connected.clear()
        self._data_frames_logged = 0
        self._resumed_channels = set()
        self._buf = bytearray()
        self.on_status(f"connecting to {WS_URL} ...")
        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=lambda ws: self._on_open(ws, token, sid),
            on_message=self._on_message,
            on_error=lambda ws, e: self.on_status(f"socket error: {e}"),
            on_close=lambda ws, code, msg: self.on_status(
                f"closed (code={code} msg={msg})"))
        self._ws.run_forever(ping_interval=0)

    def _on_open(self, ws, token, sid):
        frame = proto.encode_connection(token, sid)
        ws.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)
        self.on_status("connection frame sent, awaiting server ack")
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while (not self._stop.is_set() and self._ws and self._ws.sock
              and self._ws.sock.connected):
            # interruptible wait — wakes immediately on stop(), unlike
            # time.sleep() which would keep this daemon thread alive
            # for up to HEARTBEAT_SECS after the caller tries to exit
            if self._stop.wait(HEARTBEAT_SECS):
                return
            try:
                self._ws.send(proto.encode_heartbeat(),
                             opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception:
                return

    def _on_message(self, ws, message):
        if isinstance(message, str):
            try:
                obj = json.loads(message)
                self.on_status(f"text message: {obj}")
                if obj.get("type") == "cn" or "connected" in str(obj).lower():
                    self._connected.set()
                    self._resubscribe_all()
            except Exception:
                self.on_status(f"unrecognized text message: {message[:200]}")
            return
        self._buf.extend(message)
        frames, self._buf = proto.split_frames(self._buf)
        for raw in frames:
            self._handle_frame(raw)

    def _handle_frame(self, raw):
        if not raw:
            return
        msg_type = raw[0]
        if self.verbose:
            self.on_status(f"raw frame: type={msg_type} len={len(raw)} "
                          f"first_bytes={raw[:12].hex()}")
        if msg_type == proto.CONNECTION_TYPE:
            self._connected.set()
            self.on_status("connected — server acknowledged connection frame")
            self._resubscribe_all()
            return
        if msg_type in (proto.ACK_TYPE, proto.THROTTLING_TYPE):
            return
        if msg_type in (proto.SUBSCRIBE_TYPE, proto.UNSUBSCRIBE_TYPE):
            # server appears to echo the subscribe/unsubscribe frame type
            # back as its acknowledgment — treat as a successful ack, not
            # an unknown frame (confirmed 2026-07-20: fires right after
            # we send a SUBSCRIBE_TYPE frame, before any ticks)
            self.on_status(f"subscribe/unsubscribe acknowledged "
                          f"(type={msg_type}, {len(raw)} bytes)")
            if msg_type == proto.SUBSCRIBE_TYPE and self._resume_on_ack:
                # BUG FOUND 2026-07-21: this used to resend CHRESUME for
                # EVERY tracked channel on EVERY subscribe ack — so
                # subscribing a 2nd/3rd channel re-sent resume for the
                # 1st channel too, even though it was already streaming
                # fine. That exact run showed the index (channel 1) go
                # completely silent the moment channels 2/3 subscribed —
                # consistent with a redundant resume confusing the
                # server's per-channel state rather than helping it.
                # Now: resume each channel exactly once, ever.
                for chan in self._subscriptions:
                    if chan in self._resumed_channels:
                        continue
                    try:
                        self._ws.send(proto.encode_channel_control(chan, resume=True),
                                     opcode=websocket.ABNF.OPCODE_BINARY)
                        self.on_status(f"sent CHRESUME for channel {chan} (first time only)")
                        self._resumed_channels.add(chan)
                    except Exception as e:
                        self.on_status(f"resume send failed: {e}")
            return
        if msg_type in (proto.CHPAUSE_TYPE, proto.CHRESUME_TYPE):
            self.on_status(f"channel pause/resume ack (type={msg_type})")
            return
        if msg_type == proto.SNAPSHOT_TYPE:
            self.on_status(f"snapshot frame ({len(raw)} bytes) — logging only, not yet parsed")
            return
        if msg_type == proto.DATA_TYPE:
            if self.verbose and self._data_frames_logged < 6:
                self._data_frames_logged += 1
                self.on_status(f"FULL DATA_TYPE frame #{self._data_frames_logged} "
                              f"({len(raw)} bytes): {raw.hex()}")
            # Confirmed 2026-07-21 against real captured frames: LTP is
            # reliably decodable for index (nse_cm) ticks. Option (nse_fo)
            # decoding is NOT yet confirmed — that subscription produced
            # zero frames of any kind in testing, a separate unresolved
            # problem from this decode logic.
            tick = proto.decode_index_tick(raw)
            if tick and len(raw) >= 7:
                import struct
                channel_num = struct.unpack_from(">H", raw, 5)[0]
                scrips, _ = self._subscriptions.get(channel_num, ([], None))
                # single-scrip-per-channel is the pattern we've validated;
                # if a channel ever carries multiple scrips this mapping
                # is ambiguous and needs the (still-unconfirmed) per-tick
                # token field instead
                tok = scrips[0] if len(scrips) == 1 else f"channel{channel_num}"
                self.on_tick(tok, tick)
            return
        self.on_status(f"unhandled frame type {msg_type} ({len(raw)} bytes)")

    def subscribe(self, scrips, prefix=proto.SCRIP_PREFIX, channel_num=1):
        """scrips: list of 'exchange_segment|token' strings, e.g.
        'nse_fo|48521'. Safe to call before the connection is up —
        queued and sent once connected."""
        self._subscriptions[channel_num] = (list(scrips), prefix)
        if self._connected.is_set() and self._ws:
            self._send_subscribe(scrips, prefix, channel_num)

    def _send_subscribe(self, scrips, prefix, channel_num):
        for i in range(0, len(scrips), 50):
            batch = scrips[i:i + 50]
            frame = proto.encode_subscribe(batch, prefix, channel_num)
            try:
                self._ws.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                self.on_status(f"subscribe send failed: {e}")

    def _resubscribe_all(self):
        for chan, (scrips, prefix) in self._subscriptions.items():
            self._send_subscribe(scrips, prefix, chan)
        if self._subscriptions:
            total = sum(len(s) for s, _ in self._subscriptions.values())
            self.on_status(f"resubscribed to {total} scrips across "
                          f"{len(self._subscriptions)} channel(s)")
