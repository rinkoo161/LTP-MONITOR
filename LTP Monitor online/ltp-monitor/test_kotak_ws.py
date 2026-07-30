"""test_kotak_ws.py — validate the websocket feed against Kotak's REAL
server before trusting it live. Run this FIRST, tonight, and read the
output carefully.

    python test_kotak_ws.py

What "success" looks like:
  [1] connects and gets a CONNECTION_TYPE ack (not just a TCP connect)
  [2] subscribing to NIFTY 50 index produces real ticks with a
      plausible LTP (matches roughly what NIFTY actually is right now)
  [3] subscribing to one NIFTY option token produces ticks that
      include a non-None OI value

If [3] fails but [1]/[2] pass: the connection/protocol basics work,
but the field layout for OI specifically may need adjustment — that's
useful, actionable information, not a full failure.

If [1] fails: the reverse-engineered protocol has a real mismatch
against Kotak's live server and needs another iteration before this
is usable — paste this script's output back for that iteration,
exactly like we did for the REST API bugs.
"""
import sys
import time

import config
import kotak_ws
import kotak_ws_protocol as proto

REPORT = []


def log(msg):
    print(msg)
    REPORT.append(msg)


def main():
    cfg = config.load()
    token = cfg.get("kotak_session_token") or cfg.get("kotak_access_token")
    sid = cfg.get("kotak_sid")
    log(f"[0] session check — token={'yes' if token else 'NO'} "
       f"sid={'yes' if sid else 'NO'}")
    from datetime import datetime
    now = datetime.now()
    market_hours = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
    log(f"[0b] current time {now.strftime('%H:%M')} local, weekday={now.weekday()} "
       f"(0=Mon) — market hours (Mon-Fri 9:15-15:30 IST): {market_hours}")
    if not market_hours:
        log("    NOTE: outside market hours — zero ticks below likely means "
           "'nothing to send', not 'broken subscription'. Connection/ack "
           "steps below are still meaningful; tick delivery needs a "
           "market-hours re-run to actually confirm.")
    if not token or not sid:
        log("    !! run kotak_login.py first, this needs today's session")
        return finish()

    ticks_seen = {}

    def on_tick(tok, tick):
        ticks_seen.setdefault(tok, []).append(tick)

    def on_status(msg):
        ts = time.strftime("%H:%M:%S")
        line = f"    [{ts}] {msg}"
        print(line)
        REPORT.append(line)

    client = kotak_ws.KotakWebsocketClient(on_tick=on_tick, on_status=on_status,
                                           verbose=True, resume_on_ack=True)

    log("\n[1] connecting to " + kotak_ws.WS_URL)
    client.start()

    log("    waiting up to 15s for connection ack...")
    connected = client._connected.wait(15)
    log(f"    connected={connected}")
    if not connected:
        log("    !! no connection ack within 15s — see status log above for "
           "the raw close/error reason. This is the first thing to fix "
           "before anything else can be validated.")
        client.stop()
        return finish()

    log("\n[2] subscribing to NIFTY 50 index")
    client.subscribe(["nse_cm|Nifty 50"], prefix=proto.INDEX_PREFIX, channel_num=1)
    log("    waiting 20s (was 5s — ruling out a simple 'needs more time' explanation)")
    time.sleep(20)
    log(f"    ticks received across all subscriptions so far: "
       f"{sum(len(v) for v in ticks_seen.values())}")
    for tok, ts in ticks_seen.items():
        sample = ts[-1]
        log(f"    token={tok} last_tick={sample}")
        prices = [t.get("ltp") for t in ts if t.get("ltp")]
        if prices:
            log(f"    LTP history ({len(prices)} ticks): {prices}")
            log(f"    if this looks like real, smoothly-moving NIFTY prices "
               "— the decoder is confirmed working end to end")

    # 2026-07-27 — added after a real run's option step happened to
    # fall entirely BEFORE 9:15 IST (the earlier market_hours check, at
    # the top of main(), was already stale by the time execution
    # reached here — 20s of index-waiting plus setup time had passed).
    # Worth a SEPARATE, fresh check specifically here: equity has a
    # 9:00-9:08 pre-open call auction, but F&O (where options live) has
    # NO pre-open session at all — trading starts flat at 9:15. A zero-
    # tick result from a window that ended before 9:15 is not
    # informative and was nearly misread as "options broken" in
    # exactly that run.
    now2 = datetime.now()
    fo_open = now2.weekday() < 5 and (now2.hour, now2.minute) >= (9, 15)
    log(f"    F&O-specific check: current time {now2.strftime('%H:%M:%S')}, "
       f"F&O session open (>= 9:15 IST, no pre-open auction unlike equity): {fo_open}")
    if not fo_open:
        log("    !! this step's test window will end before F&O opens — "
           "zero option ticks below will be UNINFORMATIVE (nothing could "
           "have traded yet), not evidence of a broken subscription. "
           "Re-run after 9:15 (ideally a few minutes after, to let at "
           "least one real trade occur) for a real verdict.")
    log("\n[3] fetching real NIFTY option tokens (both CE and PE of the "
       "nearest ATM-ish strike) from the cached master")
    try:
        from broker_adapter import KotakNeoClient
        kc = KotakNeoClient()
        kc._load_master()
        opts = kc._master.get("NIFTY", [])
        now = time.time()
        candidates = [o for o in opts if o["expiry"] > now]
        picked = []
        if candidates:
            nearest_expiry = min(o["expiry"] for o in candidates)
            near_term = sorted(
                [o for o in candidates if o["expiry"] == nearest_expiry],
                key=lambda o: o["strike"])
            if near_term:
                mid = near_term[len(near_term) // 2]
                # both legs of the same strike — an index updates
                # continuously (a computed value), but an option only
                # ticks when actually TRADED. Subscribing to both CE
                # and PE of one strike roughly doubles the chance of
                # catching real activity in a short test window rather
                # than concluding "broken" from one quiet contract.
                same_strike = [o for o in near_term if o["strike"] == mid["strike"]]
                picked = same_strike[:2]
        if picked:
            for i, o in enumerate(picked):
                chan = 2 + i
                log(f"    channel {chan}: token {o['token']} ({o['tsym']})")
                client.subscribe([f"nse_fo|{o['token']}"],
                                prefix=proto.SCRIP_PREFIX, channel_num=chan)
            log("    waiting 60s (options are trade-driven, not "
               "continuously computed like the index — needs more "
               "patience than the index test did)")
            time.sleep(60)
            log(f"    all subscription keys with any data: {list(ticks_seen.keys())}")
            found_any = False
            for o in picked:
                key = f"nse_fo|{o['token']}"
                opt_ticks = ticks_seen.get(key, [])
                log(f"    {o['tsym']}: {len(opt_ticks)} ticks")
                if opt_ticks:
                    found_any = True
                    log(f"    sample: {opt_ticks[-1]}")
            if not found_any:
                if fo_open:
                    log("    still zero for both legs after 60s, WITH F&O "
                       "confirmed open the whole time — now genuinely "
                       "suggestive of a real subscription/streaming gap for "
                       "options specifically, not just contract quietness")
                else:
                    log("    zero for both legs, but F&O wasn't open for "
                       "(all or part of) this 60s window — INCONCLUSIVE, "
                       "not evidence of a bug. Re-run entirely after 9:15.")
        else:
            log("    !! no live NIFTY option token found in cached master")
    except Exception as e:
        log(f"    !! option subscribe step failed: {e}")

    log("\n[4] total unique tokens with data: " + str(len(ticks_seen)))
    log("    stopping client")
    client.stop()
    finish()


def finish():
    print("\n----- KOTAK WEBSOCKET DIAGNOSTIC REPORT START -----")
    for line in REPORT:
        print(line)
    print("----- KOTAK WEBSOCKET DIAGNOSTIC REPORT END -----")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
