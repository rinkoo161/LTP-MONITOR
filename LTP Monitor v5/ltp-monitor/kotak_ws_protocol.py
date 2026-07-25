"""kotak_ws_protocol.py — binary wire protocol for Kotak Neo's HSM
(market data) websocket, reverse-engineered from the vendor's own
hslib.js (Hypersync). Kept as pure functions with NO networking, so
the encode/decode logic can be unit-tested in isolation before ever
touching a live socket — the same validate-first discipline we used
for the REST API.

HONEST STATUS: this is reverse-engineered from a JS client library,
not from byte-level protocol documentation with worked examples.
The frame structure (length-prefix, type byte, tagged fields) and the
field-mapping table are taken directly from hslib.js's own encode/
decode functions, so they should be correct — but "should be correct
from reading someone else's JS" is not the same as "confirmed against
a live server." Treat first live connection as validation, not
as a foregone conclusion.

Frame format:  [2-byte BE length][1-byte type][fields...]
  length = byte count AFTER these first 2 length bytes.
Field format (used inside CONNECTION/SUBSCRIBE frames):
  [1-byte field id][2-byte BE length][value bytes]
"""
import struct

# ---- message type bytes (BinRespTypes in hslib.js) ----------------
CONNECTION_TYPE = 1
THROTTLING_TYPE = 2
ACK_TYPE = 3
SUBSCRIBE_TYPE = 4
UNSUBSCRIBE_TYPE = 5
DATA_TYPE = 6
CHPAUSE_TYPE = 7
CHRESUME_TYPE = 8
SNAPSHOT_TYPE = 9

# ---- topic prefixes (prepended to each scrip before encoding) -----
SCRIP_PREFIX = "sf"    # option/stock feed (mws)
INDEX_PREFIX = "if"    # index feed (ifs)
DEPTH_PREFIX = "dp"    # market depth (dps)

# ---- sub-type bytes inside DATA_TYPE frames ------------------------
RESP_SNAP = 83   # ord('S')
RESP_UPDATE = 85  # ord('U')

# Field layout per tick message (name -> (offset, kind)). Reverse-
# engineered from hslib.js's SCRIP_MAPPING table. kind is one of
# "float32", "long", "string" — each consumed in this fixed order
# from the payload immediately after the sub-type byte.
SCRIP_FIELDS = [
    ("tk", "string"), ("ts", "string"), ("e", "string"),
    ("ltp", "float32"), ("ltq", "long"), ("tbq", "long"), ("tsq", "long"),
    ("bp", "float32"), ("bq", "long"), ("sp", "float32"), ("bs", "long"),
    ("ap", "float32"), ("to", "long"), ("oi", "long"), ("ltt", "long"),
    ("fdtm", "long"), ("prec", "long"), ("op", "float32"), ("h", "float32"),
    ("lo", "float32"), ("c", "float32"), ("cng", "float32"), ("nc", "float32"),
    ("lcl", "float32"), ("ucl", "float32"), ("yh", "float32"),
    ("yl", "float32"), ("mul", "float32"), ("name", "string"),
]


def _append_field(buf, field_id, value_bytes):
    buf.extend(struct.pack(">B", field_id))
    buf.extend(struct.pack(">H", len(value_bytes)))
    buf.extend(value_bytes)


def _frame(msg_type, field_bytes, field_count):
    """Wrap fields in the [len][type][count][fields...] envelope."""
    body = bytearray()
    body.extend(struct.pack(">B", msg_type))
    body.extend(struct.pack(">B", field_count))
    body.extend(field_bytes)
    return struct.pack(">H", len(body)) + bytes(body)


def encode_connection(auth_token: str, sid: str, src: str = "JS_API") -> bytes:
    """CONNECTION_TYPE frame — sent immediately after the socket opens."""
    fields = bytearray()
    _append_field(fields, 1, auth_token.encode())
    _append_field(fields, 2, sid.encode())
    _append_field(fields, 3, src.encode())
    return _frame(CONNECTION_TYPE, fields, 3)


def _encode_scrip_list(scrips, prefix) -> bytes:
    """[2-byte count][per scrip: 1-byte len + utf8 bytes], each scrip
    prefixed with its topic type (e.g. 'sf|nse_fo|12345')."""
    tagged = [f"{prefix}|{s}" for s in scrips]
    out = bytearray(struct.pack(">H", len(tagged)))
    for s in tagged:
        b = s.encode()
        out.extend(struct.pack(">B", len(b)))
        out.extend(b)
    return bytes(out)


def encode_subscribe(scrips, prefix, channel_num: int, unsubscribe=False) -> bytes:
    """SUBSCRIBE_TYPE / UNSUBSCRIBE_TYPE frame for a list of scrips
    (already in 'exchange_segment|token' form, e.g. 'nse_fo|48521')."""
    scrip_bytes = _encode_scrip_list(scrips, prefix)
    fields = bytearray()
    _append_field(fields, 1, scrip_bytes)
    _append_field(fields, 2, struct.pack(">B", channel_num))
    msg_type = UNSUBSCRIBE_TYPE if unsubscribe else SUBSCRIBE_TYPE
    return _frame(msg_type, fields, 2)


def encode_heartbeat() -> bytes:
    """THROTTLING_TYPE keepalive — must be sent roughly every 30s or
    the server drops the connection."""
    return _frame(THROTTLING_TYPE, b"", 0)


def _read_field_value(payload, offset, kind):
    if kind == "float32":
        val = struct.unpack_from(">f", payload, offset)[0]
        return val, offset + 4
    if kind == "long":
        val = struct.unpack_from(">q", payload, offset)[0]
        return val, offset + 8
    if kind == "string":
        strlen = struct.unpack_from(">H", payload, offset)[0]
        offset += 2
        val = payload[offset:offset + strlen].decode(errors="replace")
        return val, offset + strlen
    raise ValueError(f"unknown field kind {kind}")


def decode_tick(payload: bytes) -> dict:
    """Decode a single scrip/index tick payload (the bytes AFTER the
    DATA_TYPE frame's sub-type byte) into a field-name dict."""
    out = {}
    offset = 0
    for name, kind in SCRIP_FIELDS:
        if offset >= len(payload):
            break
        try:
            val, offset = _read_field_value(payload, offset, kind)
        except struct.error:
            break
        out[name] = val
    return out


def encode_channel_control(channel_num: int, resume=True) -> bytes:
    """CHRESUME_TYPE / CHPAUSE_TYPE — never actually sent before now.
    Hypothesis worth testing (2026-07-21): subscribe gets acknowledged
    but zero ticks arrive even during confirmed market hours for both
    an index and an option subscription — a channel starting in a
    paused state, needing this explicit resume, would fully explain
    that exact symptom."""
    fields = bytearray()
    _append_field(fields, 1, struct.pack(">B", channel_num))
    msg_type = CHRESUME_TYPE if resume else CHPAUSE_TYPE
    return _frame(msg_type, fields, 1)


def decode_index_tick(raw: bytes):
    """CONFIRMED against real captured frames (2026-07-21, NIFTY 50
    index subscription): for an UPDATE (sub-type 'U') DATA_TYPE frame,
    LTP is a 4-byte big-endian unsigned int at absolute offset 23,
    scaled by 100 (paise-style). Verified across 5 consecutive live
    ticks that reconstructed a plausible, smoothly-moving NIFTY price
    matching the real index level at capture time.

    honest gaps, not yet solved:
      - the exact meaning of several surrounding bytes (offsets 14-22,
        33+) is still unconfirmed — likely a per-tick counter/timestamp
        component and static OHLC reference fields, based on their
        change patterns, but not verified byte-for-byte
      - this layout is proven for an INDEX (nse_cm) subscription only.
        the option (nse_fo) subscription produced ZERO frames of any
        kind in the same test run — a separate, unresolved problem,
        not a decode issue like this one was
      - SNAPSHOT (sub-type 'S', the first frame after subscribing)
        has a variable-length name string inserted before this same
        field layout — offset needs to shift past it, not yet fully
        mapped
    """
    if len(raw) < 27:
        return None
    sub_type = raw[9] if len(raw) > 9 else None
    if sub_type == RESP_UPDATE:
        try:
            ltp_raw = struct.unpack_from(">I", raw, 23)[0]
            return {"ltp": round(ltp_raw / 100, 2), "sub_type": "update"}
        except struct.error:
            return None
    if sub_type == RESP_SNAP:
        # snapshot has a 1-byte string-length field (observed value 0x12
        # for "if|nse_cm|Nifty 50", 18 bytes) starting at offset 14,
        # followed by that many bytes of the scrip identifier string,
        # THEN the same field layout as update — offset shifts by
        # (1 + string_length) relative to the update case's offset 23
        try:
            str_len = raw[14]
            ltp_offset = 14 + 1 + str_len + 9   # +9 = same relative
                                                 # position as update's
                                                 # payload[9:13]
            ltp_raw = struct.unpack_from(">I", raw, ltp_offset)[0]
            return {"ltp": round(ltp_raw / 100, 2), "sub_type": "snapshot"}
        except (struct.error, IndexError):
            return None
    return None


def decode_frame(raw: bytes):
    """Decode ONE complete frame (length prefix already stripped —
    caller is responsible for buffering/splitting on the 2-byte
    length prefix, since websocket messages may contain multiple
    frames or partial frames depending on how the server batches).
    Returns (msg_type, sub_type_or_None, ticks:[dict]).
    """
    if len(raw) < 1:
        return None, None, []
    msg_type = raw[0]
    if msg_type != DATA_TYPE:
        return msg_type, None, []
    if len(raw) < 2:
        return msg_type, None, []
    sub_type = raw[1]
    body = raw[2:]
    ticks = []
    offset = 0
    # multiple ticks can be packed into one DATA_TYPE frame — keep
    # decoding fixed-size records until the buffer is exhausted
    while offset < len(body):
        tick = decode_tick(body[offset:])
        if not tick:
            break
        ticks.append(tick)
        # advance by however many bytes decode_tick actually consumed —
        # recompute via a second pass since decode_tick doesn't return
        # the offset; simplest safe approach: stop after one tick per
        # frame unless we confirm multi-tick packing empirically.
        break
    return msg_type, sub_type, ticks


def split_frames(buf: bytearray):
    """Given an accumulating byte buffer, pull out every COMPLETE
    frame (2-byte length prefix + that many bytes) and return
    (list_of_frame_bodies, remaining_buf). Handles partial frames
    split across websocket message boundaries."""
    frames = []
    while len(buf) >= 2:
        length = struct.unpack_from(">H", buf, 0)[0]
        if len(buf) < 2 + length:
            break   # incomplete frame — wait for more data
        frames.append(bytes(buf[2:2 + length]))
        del buf[:2 + length]
    return frames, buf
