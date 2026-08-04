"""test_dhan_ws.py — validate the Dhan Live Market Feed websocket against
Dhan's REAL server before trusting it live. Run this FIRST, tonight,
with your real Dhan access token, and read the output carefully.

    python test_dhan_ws.py

What "success" looks like:
  [1] connects and receives an on_connect callback (not just a TCP connect)
  [2] subscribing to a NIFTY index-adjacent instrument or NIFTY option
      leg produces a real tick within ~15 seconds
  [3] the tick's LTP is a plausible number for that instrument (not 0,
      not NaN) and OI is present (non-None) for the option leg
  [4] merge_tick_into_chain() correctly updates a mock REST-shaped
      chain dict without touching its REST-only fields (iv/greeks)

This test needs your Dhan client_id + access_token in
~/.ltp-monitor/config.json (same keys the REST path already uses:
dhan_client_id, dhan_access_token) and the market to be open — Dhan's
feed won't produce ticks outside trading hours.

If [1] fails: check the access token hasn't expired (Dhan tokens are
short-lived, same as the REST path already handles) and that the
`dhanhq` package is actually installed (`pip install dhanhq`).

If [1]/[2] pass but [3] shows LTP=0 or OI=None: the connection/
subscription mechanics work but the specific security_id may be wrong,
or the market is closed. Not a protocol failure — paste this output
back so we can pin down which.
"""
import os
import sys
import time

import config
import dhan_ws


REPORT = []


def log(msg):
    print(msg)
    REPORT.append(msg)


def main():
    # 2026-08-04 — this OPENS A REAL WEBSOCKET to Dhan, and its only gate
    # was "are credentials present in config". Under run_tests.py every
    # test shares ONE temp store, and test_client_cache_reset (which sorts
    # before this file) writes dhan_client_id="1234567890" /
    # dhan_access_token="token-AAA" into it. So the skip was defeated by a
    # sibling's fixture and this dialled Dhan with a bogus token — fast to
    # fail out of hours, but during a LIVE SESSION it hung and hit the 90s
    # timeout, which is how it was found.
    #
    # Presence of credentials is not consent to make a network call from
    # the default suite. Opt in explicitly, the same shape test_kotak uses.
    if not os.environ.get("LTP_LIVE_WS_TEST"):
        log("[SKIP] live websocket test — set LTP_LIVE_WS_TEST=1 to run it. "
            "It opens a real connection to Dhan and is not part of the "
            "default suite.")
        sys.exit(77)   # env-gated skip, not a failure
    cfg = config.load()
    client_id = cfg.get("dhan_client_id")
    access_token = cfg.get("dhan_access_token")
    if not client_id or not access_token:
        log("[SKIP] No dhan_client_id/dhan_access_token in config — "
            "add them via Settings first (same fields the REST path uses).")
        sys.exit(77)   # env-gated skip, not a failure

    if dhan_ws.MarketFeed is None:
        log("[SKIP] dhanhq package not installed. Run: pip install dhanhq")
        sys.exit(77)   # env-gated skip, not a failure

    log(f"[1] Building DhanWebsocketClient for client_id={client_id[:4]}...")
    ticks_received = []
    statuses = []

    def on_tick(sym, sec_id, tick):
        ticks_received.append((sym, sec_id, tick))
        log(f"    TICK  {sym} [{sec_id}] LTP={tick['ltp']} OI={tick['oi']} "
            f"bid={tick['bid']} ask={tick['ask']}")

    def on_status(msg):
        statuses.append(msg)
        log(f"    STATUS: {msg}")

    client = dhan_ws.DhanWebsocketClient(
        client_id, access_token, on_tick=on_tick, on_status=on_status,
        verbose=True)

    # Step 2a: NIFTY 50 INDEX itself (IDX_I segment, permanent
    # security_id 13) — zero setup, no REST call, no option-chain
    # lookup. If this alone produces a tick, connection/auth/subscribe
    # mechanics are all confirmed working, independent of anything
    # option-chain related.
    client.add_index_instrument("NIFTY")
    log(f"[2a] Subscribing to the NIFTY INDEX itself "
       f"(security_id={dhan_ws.INDEX_SECURITY_ID['NIFTY']}, zero setup "
       f"needed), connecting...")

    # Step 2b (optional, additive): also try a real NIFTY OPTION leg so
    # the OI field specifically gets validated (the index has no OI).
    # Uses dhan_ws_test_security_id from config if set, else auto-fetches
    # the current ATM strike via the REST path you already have working.
    option_security_id = cfg.get("dhan_ws_test_security_id", "")
    if not option_security_id:
        try:
            import broker_adapter
            dhan = broker_adapter.DhanClient(client_id, access_token)
            chain = dhan.option_chain("NIFTY")
            spot = chain.get("spot")
            rows = chain.get("rows") or []
            if rows and spot:
                atm_row = min(rows, key=lambda r: abs(r["strike"] - spot))
                option_security_id = atm_row["ce"].get("security_id")
                if option_security_id:
                    log(f"[2b] Also subscribing ATM strike {atm_row['strike']} "
                       f"CE, security_id={option_security_id} (spot={spot}) "
                       f"— validates OI, which the index doesn't have")
        except Exception as e:
            log(f"[2b] Skipped — couldn't auto-fetch an option security_id "
               f"({e}). Index-only test above still stands on its own; "
               f"add \"dhan_ws_test_security_id\" to config.json to force "
               f"an option-leg check too.")
    if option_security_id:
        client.add_instrument("NIFTY", option_security_id)

    client.start()

    log("[3] Waiting up to 20s for ticks (market must be open)...")
    for _ in range(20):
        time.sleep(1)
        if len(ticks_received) >= (2 if option_security_id else 1):
            break

    if not statuses:
        log("[FAIL] No on_connect/on_status callback fired at all — "
            "check network/token before anything else.")
    elif not ticks_received:
        log("[PARTIAL] Connected but received zero ticks in 20s. Either "
           "the market is closed, or the subscription silently failed — "
           "check the STATUS lines above for an on_error message.")
    else:
        by_sec_id = {}
        for sym, sec_id, tick in ticks_received:
            by_sec_id[sec_id] = tick   # keep the latest per instrument
        idx_id = int(dhan_ws.INDEX_SECURITY_ID["NIFTY"])
        if idx_id in by_sec_id:
            t = by_sec_id[idx_id]
            ok = t["ltp"] and t["ltp"] > 0
            log(f"[3a] NIFTY INDEX tick: LTP={t['ltp']} "
               f"({'OK' if ok else 'SUSPICIOUS — zero/missing'})")
        else:
            log("[3a] No tick for the NIFTY INDEX itself — this is the "
               "more concerning gap since it needs zero setup.")
        if option_security_id and int(option_security_id) in by_sec_id:
            t = by_sec_id[int(option_security_id)]
            ok_ltp = t["ltp"] and t["ltp"] > 0
            ok_oi = t["oi"] is not None
            log(f"[3b] NIFTY OPTION tick: LTP={t['ltp']} "
               f"({'OK' if ok_ltp else 'SUSPICIOUS'}), OI={t['oi']} "
               f"({'OK' if ok_oi else 'MISSING'})")
        elif option_security_id:
            log("[3b] No tick yet for the option leg (index tick above "
               "may still confirm the connection works).")

    client.stop()

    # [4] merge_tick_into_chain — pure function, no live connection needed
    log("[4] Testing merge_tick_into_chain() against a mock REST-shaped chain...")
    mock_chain = {
        "symbol": "NIFTY", "spot": 24000,
        "rows": [{"strike": 24000,
                  "ce": {"ltp": 100.0, "oi": 500, "iv": 15.2, "delta": 0.5,
                        "security_id": "12345"},
                  "pe": {"ltp": 90.0, "oi": 400, "iv": 14.8, "delta": -0.5,
                        "security_id": "12346"}}],
    }
    updated = dhan_ws.merge_tick_into_chain(
        mock_chain, "12345", {"ltp": 105.5, "oi": 520, "volume": 1000,
                              "bid": 105.0, "ask": 106.0})
    ce = mock_chain["rows"][0]["ce"]
    checks = [
        ("merge returned True", updated is True),
        ("ltp updated to 105.5", ce["ltp"] == 105.5),
        ("oi updated to 520", ce["oi"] == 520),
        ("iv (REST-only field) preserved at 15.2", ce["iv"] == 15.2),
        ("delta (REST-only field) preserved at 0.5", ce["delta"] == 0.5),
        ("pe leg untouched (ltp still 90.0)", mock_chain["rows"][0]["pe"]["ltp"] == 90.0),
    ]
    all_pass = True
    for desc, result in checks:
        log(f"    {'PASS' if result else 'FAIL'}: {desc}")
        all_pass = all_pass and result
    log(f"[4] merge_tick_into_chain: {'ALL PASS' if all_pass else 'SOME FAILED'}")

    log("")
    log("=" * 60)
    log("Paste this full output back for the next iteration.")


if __name__ == "__main__":
    main()
