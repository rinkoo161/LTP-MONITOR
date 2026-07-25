"""Kotak Neo API diagnostic v2 — matches the current mis.kotaksecurities.com
trade API flow (the one you validated with curl). Run: python test_kotak.py
Report is secret-safe: key names and statuses only, never values.
"""
import getpass, json, sys, urllib.error, urllib.request

REPORT = []
def log(m):
    print(m); REPORT.append(m)

def http(method, url, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try: return r.status, json.loads(raw)
            except Exception: return r.status, raw[:200]
    except urllib.error.HTTPError as e:
        raw = e.read()[:400]
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw
    except Exception as e:
        return None, str(e)

def keys_only(o, d=0):
    if d > 3: return "..."
    if isinstance(o, dict): return {k: keys_only(v, d+1) for k, v in o.items()}
    if isinstance(o, list): return [keys_only(o[0], d+1)] if o else []
    return type(o).__name__

BASE = "https://mis.kotaksecurities.com"

def main():
    print("Kotak Neo diagnostic v2 — credentials stay on this machine")
    access_token = getpass.getpass("Access Token from Kotak portal (hidden): ").strip()
    mobile = input("Mobile (+91XXXXXXXXXX): ").strip()
    ucc = input("UCC / Client Code (e.g. W1DZT): ").strip()
    totp = input("TOTP from authenticator (6 digits): ").strip()

    # ---- STEP 1: tradeApiLogin (you already proved this works) ------
    log("\n[1] POST /login/1.0/tradeApiLogin")
    st, resp = http("POST", BASE + "/login/1.0/tradeApiLogin",
        headers={"Authorization": access_token,
                 "neo-fin-key": "neotradeapi",
                 "Content-Type": "application/json"},
        body={"mobileNumber": mobile, "ucc": ucc, "totp": totp})
    log(f"    status={st} keys={keys_only(resp)}")
    d = (resp or {}).get("data", {}) if isinstance(resp, dict) else {}
    view_token, view_sid = d.get("token"), d.get("sid")
    log(f"    view_token={'yes' if view_token else 'NO'} sid={'yes' if view_sid else 'NO'} kType={d.get('kType')}")
    if not view_token: return finish()

    # ---- STEP 2: upgrade View -> Trade session (MPIN) ---------------
    mpin = getpass.getpass("MPIN (6-digit trading PIN, hidden — press Enter to skip): ").strip()
    trade_token, trade_sid = view_token, view_sid
    if mpin:
        log("\n[2] POST /login/1.0/tradeApiValidate (mpin)")
        st, resp = http("POST", BASE + "/login/1.0/tradeApiValidate",
            headers={"Authorization": access_token, "Auth": view_token,
                     "sid": view_sid, "neo-fin-key": "neotradeapi",
                     "Content-Type": "application/json"},
            body={"mpin": mpin})
        log(f"    status={st} keys={keys_only(resp)}")
        d2 = (resp or {}).get("data", {}) if isinstance(resp, dict) else {}
        if d2.get("token"):
            trade_token, trade_sid = d2["token"], d2.get("sid") or view_sid
            log(f"    trade session kType={d2.get('kType')}")
            log(f"    baseUrl={d2.get('baseUrl')}  (datacenter URL — not secret)")
            log(f"    hsServerId={d2.get('hsServerId')}")
            globals()["BASEURL"] = (d2.get("baseUrl") or "").rstrip("/")
        else:
            log("    !! no trade token — orders would be blocked; quotes may still work")
    else:
        log("\n[2] skipped (no MPIN entered) — using View session")

    hdrs = {"Authorization": access_token, "Auth": trade_token,
            "sid": trade_sid, "neo-fin-key": "neotradeapi",
            "Content-Type": "application/json"}

    # ---- STEP 3: scrip master — lapi public CSVs (date-stamped) -----
    log("\n[3] scrip master via lapi.kotaksecurities.com (public)")
    from datetime import date, timedelta
    fo = []
    for back in range(0, 6):
        dt = (date.today() - timedelta(days=back)).isoformat()
        url = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/{dt}/transformed/nse_fo.csv"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:  # GET follows the 303
                peek = r.read(1024)
                log(f"    {dt}: status={r.status} bytes_ok={len(peek)>100}  <-- FOUND")
                fo = [url]; break
        except Exception as e:
            log(f"    {dt}: {getattr(e,'code',e)}")
    if not fo:
        log("    fallback: gw-napi file-paths with trade session")
        st, resp = http("GET",
            "https://gw-napi.kotaksecurities.com/Files/1.0/masterscrip/v2/file-paths",
            headers=hdrs)
        log(f"    status={st} keys={keys_only(resp)}")

    # ---- STEP 4: download FO master slice, find a NIFTY option ------
    tok = None
    if fo:
        log("\n[4] scan full nse_fo master for NIFTY options")
        try:
            import csv, io
            with urllib.request.urlopen(fo[0], timeout=60) as r:
                text = r.read().decode(errors="ignore")
            rdr = csv.DictReader(io.StringIO(text))
            log(f"    full header: {rdr.fieldnames}")
            nifty = [row for row in rdr
                     if (row.get("pSymbolName","").strip() == "NIFTY"
                         and row.get("pOptionType","").strip() in ("CE","PE"))]
            log(f"    NIFTY option rows: {len(nifty)}")
            if nifty:
                r0 = nifty[0]
                log(f"    sample: pSymbol={r0.get('pSymbol')} pTrdSymbol={r0.get('pTrdSymbol')} "
                    f"opt={r0.get('pOptionType')} lot={r0.get('lLotSize')} "
                    f"expiry={r0.get('lExpiryDate ') or r0.get('lExpiryDate')}")
                log(f"    strike: {r0.get('dStrikePrice;')}")
                tok = r0.get("pSymbol")
        except Exception as e:
            log(f"    !! scan failed: {e}")

    # ---- STEP 5: quotes — try baseUrl first, then gw-napi ------------
    log("\n[5] quotes")
    base = globals().get("BASEURL","")
    cands = ([base + "/quotes/2.0/quotes", base + "/Quotes/2.0/quotes"] if base else []) + \
            ["https://gw-napi.kotaksecurities.com/Quotes/2.0/quotes",
             BASE + "/quotes/1.0/quotes"]
    if not tok:
        log("    skipped (no token from step 4)")
    else:
        for url in cands:
            st, resp = http("POST", url, headers=hdrs,
                body={"instrumentTokens": [{"instrumentToken": tok,
                                            "exchangeSegment": "nse_fo"}]})
            log(f"    POST {url} -> status={st} keys={keys_only(resp)}")
            if st == 200: break
    finish()

def finish():
    print("\n----- KOTAK DIAGNOSTIC REPORT START -----")
    for l in REPORT: print(l)
    print("----- KOTAK DIAGNOSTIC REPORT END -----")

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: sys.exit(1)
