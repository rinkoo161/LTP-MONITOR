"""
DhanHQ v2 option-chain adapter -- true live data for all four indices,
including SENSEX (BSE) and FINNIFTY (monthly).

Setup:
  1. Log in at web.dhan.co -> My Profile -> DhanHQ Trading APIs
     -> generate an Access Token (validity up to 24h/30d depending on plan).
     Note: the market **Data APIs pack** (~Rs 499/month) must be active
     on your Dhan account for the option-chain endpoint to return data.
  2. Set environment variables before starting the app:
        export DHAN_CLIENT_ID="1000000001"
        export DHAN_ACCESS_TOKEN="eyJ..."
  3. python app.py   (the app auto-detects Dhan and prefers it over NSE)

API reference: https://dhanhq.co/docs/v2/option-chain/
Rate limit: 1 unique option-chain request per 3 seconds -- this adapter
serializes and caches requests to stay inside that.
"""

import os
import store
import time
import threading
import requests

API = "https://api.dhan.co/v2"

# Dhan security IDs for index underlyings (segment IDX_I).
# If one ever changes, look it up in the instrument master:
# https://images.dhan.co/api-data/api-scrip-master.csv  (SEM_SMST_SECURITY_ID
# for the index row) or https://dhanhq.co/docs/v2/instruments/
UNDERLYINGS = {
    "NIFTY":      13,
    "BANKNIFTY":  25,
    "FINNIFTY":   27,
    "MIDCPNIFTY": 442,
    "SENSEX":     51,
}

CACHE_TTL = 12          # seconds; well inside the 1-per-3s limit per symbol
MIN_GAP = 3.2           # seconds between any two option-chain calls


class DhanClient:
    def __init__(self, client_id=None, access_token=None):
        import config as _cfg
        cfg = _cfg.load()
        self.client_id = client_id or cfg.get("dhan_client_id") or os.environ.get("DHAN_CLIENT_ID")
        self.token = access_token or cfg.get("dhan_access_token") or os.environ.get("DHAN_ACCESS_TOKEN")
        if not (self.client_id and self.token):
            raise RuntimeError(
                "Dhan credentials missing — add them in the dashboard "
                "Settings panel (gear icon) or set DHAN_CLIENT_ID / "
                "DHAN_ACCESS_TOKEN environment variables."
            )
        self._h = {
            "Content-Type": "application/json",
            "access-token": self.token,
            "client-id": str(self.client_id),
        }
        self._expiry = {}        # symbol -> (ts, expiry string)
        self._cache = {}         # symbol -> (ts, chain dict)
        self._lock = threading.Lock()
        self._last_call = 0.0

    @staticmethod
    def available() -> bool:
        import config as _cfg
        cfg = _cfg.load()
        return bool(cfg.get("dhan_client_id") and cfg.get("dhan_access_token"))

    # ------------------------------------------------------------- internals
    def _post(self, path, body):
        # global pacing: Dhan allows 1 option-chain request / 3 s
        with self._lock:
            wait = MIN_GAP - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
        r = requests.post(API + path, json=body, headers=self._h, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("Dhan says 401 Unauthorized — your access "
                               "token has expired; generate a new one at "
                               "web.dhan.co and update DHAN_ACCESS_TOKEN.")
        if r.status_code == 429:
            raise RuntimeError("Dhan rate limit hit — slow down polling.")
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "success":
            raise RuntimeError(f"Dhan error: {j}")
        return j["data"]

    def _nearest_expiry(self, symbol):
        cached = self._expiry.get(symbol)
        if cached and time.time() - cached[0] < 1800:
            return cached[1]
        scrip, seg = self._scrip_and_seg(symbol)
        data = self._post("/optionchain/expirylist", {
            "UnderlyingScrip": scrip,
            "UnderlyingSeg": seg,
        })
        if not data:
            raise RuntimeError(f"No active expiries for {symbol}")
        exp = sorted(data)[0]
        self._expiry[symbol] = (time.time(), exp)
        return exp

    # ------------------------------------------------------------- fast quotes
    def quote_batch(self, segment_to_ids: dict) -> dict:
        """Raw wrapper for Dhan's /marketfeed/quote (Market Quote API,
        confirmed schema 2026-07-25 from https://dhanhq.co/docs/v2/
        market-quote/): up to 1000 instruments across segments in ONE
        call, rate-limited to 1 request/SECOND — a completely separate,
        much faster limit than the option-chain endpoint's ~3.2s gap
        (MIN_GAP above), since this is a different Dhan API family.
        `segment_to_ids`: {"NSE_FNO": [id1, id2], "BSE_FNO": [id3],
        "IDX_I": [id4]}. Returns the raw {segment: {sec_id: {...}}}
        response Dhan documents directly (last_price, oi, volume, ohlc,
        depth, etc.) — callers interpret whichever fields they need.
        Used as a network-independent-of-live-ticks source for futures
        LTP/OI (see MarketDataAgent._poll_futures_via_rest), since a
        websocket subscription only produces data when a genuine trade
        actually prints — for a less-liquid contract that can mean long
        gaps with nothing to show, even though the subscription itself
        is working correctly."""
        body = {seg: [int(i) for i in ids] for seg, ids in
               (segment_to_ids or {}).items() if ids}
        if not body:
            return {}
        r = requests.post(API + "/marketfeed/quote", json=body,
                         headers=self._h, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "success":
            raise RuntimeError(f"Dhan error: {j}")
        return j.get("data", {})

    def quote_ltp(self, symbols=None) -> dict:
        """All index spots in ONE call via /marketfeed/ltp (separate, faster
        rate limit than the option chain). Cached ~2.5s.

        2026-07-25 — added a 429 cooldown after a live log showed
        sustained "429 Too Many Requests" on the sibling /marketfeed/
        quote endpoint for ~9 minutes straight, with no backoff, so
        each retry just hit the same wall again. This endpoint is
        polled by /api/ticker roughly every 3s from the browser (just
        over this method's own 2.5s cache) — without a cooldown here
        too, a 429 here would fall into the identical self-sustaining
        retry loop. On a 429, returns the last known-good (if stale)
        quotes for a cooldown window instead of hammering Dhan again
        immediately."""
        symbols = symbols or list(UNDERLYINGS.keys())
        cached = self._cache.get("ltp_all")
        if cached and time.time() - cached[0] < 2.5:
            return cached[1]
        # v58.49 — the SHARED cooldown wins over the local one. Two
        # independent backoffs on the same endpoint meant this path
        # could hammer a surface that prev_close had already been
        # refused by, so the server saw no net reduction in pressure.
        import rate_limit
        if rate_limit.is_limited("quote"):
            return {}
        fail_until = self._cache.get("ltp_all_fail_until", 0)
        if time.time() < fail_until:
            return cached[1] if cached else {}
        try:
            body = {"IDX_I": [UNDERLYINGS[s] for s in symbols]}
            r = requests.post(API + "/marketfeed/ltp", json=body,
                              headers=self._h, timeout=10)
            r.raise_for_status()
        except Exception as e:
            is_429 = "429" in str(e) or "Too Many Requests" in str(e)
            import rate_limit
            rate_limit.note_failure(e if "e" in dir() else "ltp_all failed",
                                    "quote", on_429=60, otherwise=10)
            self._cache["ltp_all_fail_until"] = time.time() + (60 if is_429 else 10)
            raise
        data = (r.json().get("data") or {}).get("IDX_I", {})
        id2sym = {str(v): k for k, v in UNDERLYINGS.items()}
        out = {}
        for sec_id, q in data.items():
            sym = id2sym.get(str(sec_id))
            if sym:
                out[sym] = float(q.get("last_price") or 0)
        self._cache["ltp_all"] = (time.time(), out)
        return out

    # ------------------------------------------------------------- candles
    def intraday(self, symbol: str, interval: str = "5") -> dict:
        """Index OHLC candles for today (Dhan /charts/intraday)."""
        symbol = symbol.upper()
        key = f"candles:{symbol}:{interval}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < 55:
            return cached[1]
        import datetime as _dt
        today = _dt.date.today()
        frm = (today - _dt.timedelta(days=4)).isoformat()  # covers weekends
        body = {
            "securityId": str(UNDERLYINGS[symbol]),
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "interval": str(interval),
            "fromDate": frm,
            "toDate": today.isoformat(),
        }
        r = requests.post(API + "/charts/intraday", json=body,
                          headers=self._h, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("Dhan token expired — paste a fresh Access "
                               "Token in Settings (gear icon)")
        r.raise_for_status()
        d = r.json()
        candles = [
            {"time": int(t), "open": o, "high": h, "low": l, "close": c}
            for t, o, h, l, c in zip(d.get("timestamp", []), d.get("open", []),
                                     d.get("high", []), d.get("low", []),
                                     d.get("close", []))
        ][-350:]
        out = {"symbol": symbol, "interval": interval, "candles": candles}
        self._cache[key] = (time.time(), out)
        return out

    # ------------------------------------------------------------- public
    def historical_daily(self, symbol: str, days_back: int = 400) -> dict:
        """True daily OHLC candles (Dhan's dedicated /charts/historical
        endpoint — NOT the same as intraday() aggregated to daily,
        this is a real daily-timeframe series going back as far as the
        instrument's inception). Needed for the MTF confluence strategy
        (rinkoo.docx): daily MACD/RSI/Stochastic/BB need ~30+ daily
        bars minimum; weekly MACD (resampled from these same daily
        bars in Python, no separate weekly API call needed) needs
        26+ weeks, hence the 400-calendar-day default (~270 trading
        days, comfortably covering both)."""
        symbol = symbol.upper()
        key = f"daily:{symbol}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < 6 * 3600:  # daily bars — long cache
            return cached[1]
        import datetime as _dt
        today = _dt.date.today()
        frm = (today - _dt.timedelta(days=days_back)).isoformat()
        body = {
            "securityId": str(UNDERLYINGS[symbol]),
            "exchangeSegment": "IDX_I",
            "instrument": "INDEX",
            "expiryCode": 0,
            "oi": False,
            "fromDate": frm,
            "toDate": today.isoformat(),
        }
        r = requests.post(API + "/charts/historical", json=body,
                          headers=self._h, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("Dhan token expired — paste a fresh Access "
                               "Token in Settings (gear icon)")
        r.raise_for_status()
        d = r.json()
        candles = [
            {"time": int(t), "open": o, "high": h, "low": l, "close": c}
            for t, o, h, l, c in zip(d.get("timestamp", []), d.get("open", []),
                                     d.get("high", []), d.get("low", []),
                                     d.get("close", []))
        ]
        out = {"symbol": symbol, "candles": candles}
        self._cache[key] = (time.time(), out)
        return out

    def _hist_raw(self, body):
        """Shared ranged-candle fetch → [{ts,o,h,l,c,v,oi}] for history.py."""
        r = requests.post(API + "/charts/intraday", json=body,
                          headers=self._h, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("Dhan token expired — paste a fresh Access "
                               "Token in Settings")
        r.raise_for_status()
        d = r.json()
        return [{"ts": int(t), "o": o, "h": h, "l": l, "c": c,
                 "v": v, "oi": oi}
                for t, o, h, l, c, v, oi in zip(
                    d.get("timestamp", []), d.get("open", []),
                    d.get("high", []), d.get("low", []), d.get("close", []),
                    d.get("volume", []) or [None] * len(d.get("timestamp", [])),
                    d.get("open_interest", []) or [None] * len(d.get("timestamp", [])))]

    def _intraday_range(self, symbol, interval, from_date, to_date):
        return self._hist_raw({
            "securityId": str(UNDERLYINGS[symbol.upper()]),
            "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": str(interval),
            "fromDate": from_date, "toDate": to_date})

    def _intraday_range_sid(self, security_id, interval, from_date, to_date,
                            segment="NSE_FNO", instrument="OPTIDX"):
        return self._hist_raw({
            "securityId": str(security_id),
            "exchangeSegment": segment, "instrument": instrument,
            "interval": str(interval),
            "fromDate": from_date, "toDate": to_date})

    def _scrip_and_seg(self, symbol):
        """(UnderlyingScrip, UnderlyingSeg) for the option-chain calls.

        2026-08-04, Phase 1 of the stock-options work. The four indices
        keep their EXISTING hardcoded path — same ids, same IDX_I, no
        dependency on the 34 MB scrip master — so a live trading path
        cannot regress because a CSV fetch failed. Only symbols the old
        table does not know are resolved through instrument_registry.

        The two segments genuinely differ: an index chain is quoted
        against IDX_I, a stock's against its exchange F&O segment.
        Verified live — NIFTY/IDX_I returns 18 expiries, ADANIENSOL
        (10217)/NSE_FNO returns 3.
        """
        sym = symbol.upper()
        if sym in UNDERLYINGS:
            return UNDERLYINGS[sym], "IDX_I"
        import instrument_registry as _ir
        d = _ir.resolve(sym)
        if not d or not d.get("underlying_id"):
            raise RuntimeError(
                f"Unknown symbol {symbol} — not a known index and no "
                f"option-bearing underlying of that name in the scrip master")
        return int(d["underlying_id"]), d["underlying_seg"]

    def option_chain(self, symbol: str) -> dict:
        symbol = symbol.upper()
        scrip, seg = self._scrip_and_seg(symbol)

        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]

        expiry = self._nearest_expiry(symbol)
        data = self._post("/optionchain", {
            "UnderlyingScrip": scrip,
            "UnderlyingSeg": seg,
            "Expiry": expiry,
        })

        spot = data.get("last_price")
        rows = []
        for strike_str, legs in (data.get("oc") or {}).items():
            strike = float(strike_str)
            rows.append({
                "strike": strike,
                "ce": _leg(legs.get("ce") or {}),
                "pe": _leg(legs.get("pe") or {}),
            })
        rows.sort(key=lambda r: r["strike"])

        chain = {
            "symbol": symbol,
            "spot": spot,
            "expiry": expiry,
            "timestamp": time.strftime("%d-%b-%Y %H:%M:%S"),
            "rows": rows,
            "totals": {},
            "source": "Dhan live",
        }
        self._cache[symbol] = (time.time(), chain)
        return chain


def _leg(d: dict) -> dict:
    oi = int(d.get("oi") or 0)
    prev_oi = int(d.get("previous_oi") or 0)
    ltp = float(d.get("last_price") or 0)
    prev_close = float(d.get("previous_close_price") or 0)
    return {
        "ltp": ltp,
        "oi": oi,
        "oi_chg": oi - prev_oi,
        "volume": int(d.get("volume") or 0),
        "iv": round(float(d.get("implied_volatility") or 0), 2),
        "chg": round(ltp - prev_close, 2) if prev_close else 0,
        "bid": float(d.get("top_bid_price") or 0),
        "ask": float(d.get("top_ask_price") or 0),
        "security_id": d.get("security_id"),
        # extra fields (Dhan provides greeks; analyzer ignores unknown keys)
        "delta": (d.get("greeks") or {}).get("delta"),
        "theta": (d.get("greeks") or {}).get("theta"),
        "gamma": (d.get("greeks") or {}).get("gamma"),
        "vega": (d.get("greeks") or {}).get("vega"),
    }


# ---------------------------------------------------------------- orders

FNO_SEGMENT = {
    "NIFTY": "NSE_FNO", "BANKNIFTY": "NSE_FNO", "FINNIFTY": "NSE_FNO",
    "MIDCPNIFTY": "NSE_FNO", "SENSEX": "BSE_FNO",
}


class DhanOrders:
    """Thin wrapper over Dhan v2 order endpoints. Every call here moves
    REAL MONEY when paper mode is off — the app gates these behind
    explicit user confirmation / autopilot opt-in."""

    def __init__(self, client: "DhanClient"):
        self.c = client

    def place(self, symbol: str, security_id, side: str, qty: int,
              order_type: str = "MARKET", price: float = 0.0) -> dict:
        body = {
            "dhanClientId": str(self.c.client_id),
            "transactionType": side,             # BUY / SELL
            "exchangeSegment": FNO_SEGMENT.get(symbol.upper(), "NSE_FNO"),
            "productType": "INTRADAY",
            "orderType": order_type,             # MARKET / LIMIT
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(qty),
            "price": float(price) if order_type == "LIMIT" else 0,
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
            # v59.69 (third-eye Tier 3) — client-side order tag. Dhan
            # echoes correlationId back in the order book, so a timed-out
            # placement can be identified there instead of guessed at.
            # (Dhan does not dedupe on it — the retry protection is the
            # exit-side cooldown in agents.exit(); this makes the manual
            # and future order_status() reconciliation possible.)
            "correlationId": f"LTP{int(time.time() * 1000) % 10 ** 12}",
        }
        r = requests.post(API + "/orders", json=body, headers=self.c._h,
                          timeout=15)
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Dhan order rejected [{r.status_code}]: {j}")
        return j

    def order_status(self, order_id: str) -> dict:
        r = requests.get(API + f"/orders/{order_id}", headers=self.c._h,
                         timeout=15)
        r.raise_for_status()
        return r.json()

    def positions(self) -> list:
        r = requests.get(API + "/positions", headers=self.c._h, timeout=15)
        r.raise_for_status()
        return r.json()


# ===================== multi-broker support =====================
# Common interface every adapter implements:
#   available() -> bool           (credentials configured?)
#   option_chain(symbol) -> dict  (same shape DhanClient returns)
#   intraday(symbol, interval) -> dict
# Orders class: place(...), like DhanOrders.
#
# NOTE: KotakNeo and Zerodha adapters follow their public API docs but
# could not be tested against live endpoints from the build environment.
# First live run should be in PAPER mode; errors will surface clearly.

import config as _config
import json
import urllib.request
import urllib.parse
import urllib.error

IDX_MAP_KITE = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
                "FINNIFTY": "NIFTY FIN SERVICE", "SENSEX": "SENSEX"}


class ZerodhaClient:
    """Zerodha Kite Connect. Needs api_key + access_token (generate daily
    via Kite login flow). Option chain is assembled from the instruments
    dump + batched quote calls."""
    BASE = "https://api.kite.trade"

    def __init__(self):
        cfg = _config.load()
        self.api_key = cfg.get("zerodha_api_key", "")
        self.access_token = cfg.get("zerodha_access_token", "")
        self._instruments = None

    @staticmethod
    def available():
        cfg = _config.load()
        return bool(cfg.get("zerodha_api_key") and cfg.get("zerodha_access_token"))

    def _req(self, path, params=None):
        import urllib.parse
        url = self.BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())["data"]
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise RuntimeError("Zerodha token expired — regenerate today's "
                                   "access token and paste it in Settings")
            raise

    def _load_instruments(self):
        if self._instruments is not None:
            return
        # NFO+BFO instrument dump (CSV). Cached for the session.
        import csv, io
        req = urllib.request.Request(self.BASE + "/instruments",
            headers={"X-Kite-Version": "3",
                     "Authorization": f"token {self.api_key}:{self.access_token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
        self._instruments = list(csv.DictReader(io.StringIO(text)))

    def option_chain(self, symbol: str) -> dict:
        self._load_instruments()
        name = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY",
                "FINNIFTY": "FINNIFTY", "SENSEX": "SENSEX"}[symbol]
        opts = [i for i in self._instruments
                if i["name"] == name and i["instrument_type"] in ("CE", "PE")]
        if not opts:
            raise RuntimeError("no option instruments found for " + symbol)
        expiry = min(i["expiry"] for i in opts)
        cur = [i for i in opts if i["expiry"] == expiry]
        # spot from index quote
        spot_key = ("BSE:" if symbol == "SENSEX" else "NSE:") + IDX_MAP_KITE[symbol]
        spot = self._req("/quote/ltp", {"i": spot_key})[spot_key]["last_price"]
        # nearest 17 strikes around ATM
        strikes = sorted({float(i["strike"]) for i in cur})
        atm = min(strikes, key=lambda s: abs(s - spot))
        idx = strikes.index(atm)
        window = strikes[max(0, idx - 8): idx + 9]
        sel = [i for i in cur if float(i["strike"]) in window]
        keys = [f"NFO:{i['tradingsymbol']}" if symbol != "SENSEX"
                else f"BFO:{i['tradingsymbol']}" for i in sel]
        quotes = {}
        for chunk_start in range(0, len(keys), 250):
            quotes.update(self._req("/quote", {"i": keys[chunk_start:chunk_start + 250]}))
        rows = {}
        for inst, key in zip(sel, keys):
            q = quotes.get(key) or {}
            strike = float(inst["strike"])
            leg = {
                "ltp": q.get("last_price"),
                "oi": q.get("oi"), "oi_chg": (q.get("oi") or 0) - (q.get("oi_day_low") or 0),
                "volume": q.get("volume"),
                "iv": None,   # Kite quotes don't include IV
                "chg": (q.get("last_price") or 0) - ((q.get("ohlc") or {}).get("close") or 0),
                "bid": ((q.get("depth") or {}).get("buy") or [{}])[0].get("price"),
                "ask": ((q.get("depth") or {}).get("sell") or [{}])[0].get("price"),
                "security_id": inst["instrument_token"],
                "delta": None, "theta": None, "gamma": None, "vega": None,
            }
            rows.setdefault(strike, {"strike": strike, "ce": {}, "pe": {}})
            rows[strike][inst["instrument_type"].lower()] = leg
        return {"symbol": symbol, "spot": spot, "expiry": expiry,
                "rows": [rows[k] for k in sorted(rows)],
                "atm": atm}

    def intraday(self, symbol: str, interval: str = "5") -> dict:
        # Kite historical API needs instrument token of the index
        tok = {"NIFTY": 256265, "BANKNIFTY": 260105,
               "FINNIFTY": 257801, "SENSEX": 265}.get(symbol)
        from datetime import datetime, timedelta
        frm = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        to = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        iv = {"1": "minute", "5": "5minute", "15": "15minute"}[interval]
        data = self._req(f"/instruments/historical/{tok}/{iv}",
                         {"from": frm, "to": to})
        candles = [{"time": c[0], "open": c[1], "high": c[2],
                    "low": c[3], "close": c[4]} for c in data["candles"]]
        return {"candles": candles[-120:]}


class ZerodhaOrders:
    def __init__(self, client: "ZerodhaClient"):
        self.c = client

    def place(self, symbol, security_id, side, qty, price=None):
        import urllib.parse
        body = urllib.parse.urlencode({
            "tradingsymbol": security_id, "exchange":
                "BFO" if symbol == "SENSEX" else "NFO",
            "transaction_type": side, "order_type": "MARKET",
            "quantity": qty, "product": "MIS", "validity": "DAY"}).encode()
        req = urllib.request.Request(self.c.BASE + "/orders/regular",
            data=body, headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {self.c.api_key}:{self.c.access_token}",
                "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["data"]["order_id"]


class KotakNeoClient:
    """Kotak Neo — validated spec. Run `python kotak_login.py` each morning
    to create the day's session (TOTP+MPIN); it stores session token, sid
    and baseUrl in config. Quotes are batched (all strikes in ONE call)."""

    def __init__(self):
        cfg = _config.load()
        self.api_token = cfg.get("kotak_access_token", "")
        self.base = (cfg.get("kotak_base_url") or "").rstrip("/")
        self._master = None

    @staticmethod
    def available():
        cfg = _config.load()
        return bool(cfg.get("kotak_access_token") and cfg.get("kotak_base_url"))

    _rate_lock = threading.Lock()
    _last_call = [0.0]
    # Kotak's documented limit is 10 requests/second ACROSS ALL APIs
    # (confirmed in their API docs, Q24). A per-method sleep() only
    # protects calls made sequentially through that one method — it does
    # nothing when several agents/endpoints call concurrently from
    # different threads, which is exactly what caused the 429 storm.
    # This lock+timestamp gate is shared at the CLASS level so every
    # Kotak HTTP call in the process — regardless of which method or
    # thread issues it — respects one global minimum spacing.
    _MIN_INTERVAL = 1.0 / 8   # 8/s ceiling, safely under the documented 10/s

    def _get(self, url):
        with KotakNeoClient._rate_lock:
            wait = KotakNeoClient._last_call[0] + KotakNeoClient._MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            KotakNeoClient._last_call[0] = time.time()
        req = urllib.request.Request(url, headers={
            "Authorization": self.api_token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _load_master(self):
        if self._master is not None:
            return
        import csv, io, os
        from datetime import date
        cache = store.path("kotak_master_urls.json")
        today = date.today().isoformat()
        urls = None
        if os.path.exists(cache):
            try:
                cached = json.loads(open(cache).read())
                if cached.get("date") == today:
                    urls = cached["urls"]
            except Exception:
                pass
        if urls is None:
            # Official, authenticated discovery endpoint — returns the
            # exact current file paths (including bse_fo.csv, needed for
            # SENSEX) rather than us guessing the date-stamped public URL
            # and probing backward when it's wrong.
            resp = self._get(f"{self.base}/script-details/1.0/masterscrip/file-paths")
            urls = (resp.get("data") or {}).get("filesPaths") or []
            if not urls:
                raise RuntimeError("Kotak masterscrip/file-paths returned no URLs")
            json.dump({"date": today, "urls": urls}, open(cache, "w"))

        def _fetch_csv(name):
            url = next((u for u in urls if u.endswith(f"/{name}")), None)
            if not url:
                return None
            cache_csv = os.path.expanduser(f"~/.ltp-monitor/kotak_{name}")
            if os.path.exists(cache_csv) and open(cache_csv).readline().strip() == today:
                return open(cache_csv).read().split("\n", 1)[1]
            with urllib.request.urlopen(url, timeout=60) as r:
                text = r.read().decode(errors="ignore")
            open(cache_csv, "w").write(today + "\n" + text)
            return text

        text = _fetch_csv("nse_fo.csv")
        bse_text = _fetch_csv("bse_fo.csv")
        if not text:
            raise RuntimeError("Kotak scrip master unavailable (nse_fo.csv not found)")

        def _find(row, *substrings):
            """Column names in Kotak's CSV have inconsistent trailing
            whitespace (e.g. 'lExpiryDate ' vs 'lExpiryDate') and this can
            vary between exports — match by substring, case-insensitive,
            instead of guessing exact key spellings."""
            for k, v in row.items():
                if k and all(s.lower() in k.lower() for s in substrings):
                    return v
            return None

        def _parse_expiry(row, segment):
            """CONFIRMED via Kotak's official Scrip Master documentation
            (column-mapping notes): 'lExpiryDate' encodes expiry
            differently by segment —
              nse_fo / cde_fo : epoch + 315511200 seconds, then treat as IST
              bse_fo / mcx_fo : epoch used directly, no correction
            This replaces the earlier ~10-year heuristic (315532800 s),
            which was off by exactly 6 hours from the documented value —
            close enough to have looked plausible, but not the real spec."""
            import time as _t
            now = _t.time()
            plausible = lambda ts: (now - 5 * 86400) < ts < (now + 500 * 86400)
            try:
                raw_num = int(_find(row, "expirydate") or 0)
            except (ValueError, TypeError):
                raw_num = 0
            if not raw_num:
                return 0, "unavailable"
            OFFSET = 315511200
            expiry = raw_num + OFFSET if segment in ("nse_fo", "cde_fo") else raw_num
            if plausible(expiry):
                return expiry, "documented-offset" if segment in ("nse_fo", "cde_fo") else "numeric"
            # fall back to the string date column only if the documented
            # formula somehow produces an implausible result (shouldn't
            # normally happen, but keeps a safety net rather than a crash)
            from datetime import datetime as _dt
            raw_str = (row.get("pExpiryDate") or "").strip()
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
                       "%d%b%Y", "%b %d, %Y", "%d-%b-%y"):
                try:
                    ts = _dt.strptime(raw_str, fmt).timestamp()
                    if plausible(ts):
                        return int(ts), "string-fallback"
                except (ValueError, TypeError):
                    continue
            return 0, "unavailable"

        self._master = {}
        skipped_bad_expiry = {}
        method_counts = {}

        def _process(csv_text, segment, names):
            for row in csv.DictReader(io.StringIO(csv_text)):
                name = (row.get("pSymbolName") or "").strip()
                if name in names and \
                   (row.get("pOptionType") or "").strip() in ("CE", "PE"):
                    expiry, src = _parse_expiry(row, segment)
                    if expiry <= 0:
                        skipped_bad_expiry[name] = skipped_bad_expiry.get(name, 0) + 1
                        continue
                    method_counts.setdefault(name, {}).setdefault(src, 0)
                    method_counts[name][src] += 1
                    self._master.setdefault(name, []).append({
                        "token": row["pSymbol"],
                        "type": row["pOptionType"].strip(),
                        "strike": float(row.get("dStrikePrice;") or 0) / 100.0,
                        "lot": int(row.get("lLotSize") or 0),
                        "expiry": expiry,
                        "tsym": row.get("pTrdSymbol"),
                        "segment": segment,
                    })

        _process(text, "nse_fo", ("NIFTY", "BANKNIFTY", "FINNIFTY"))
        if bse_text:
            # SENSEX's pSymbolName in the bse_fo master is UNCONFIRMED —
            # trying the obvious candidate. If SENSEX still shows "no
            # contracts found" after this update, run test_kotak.py and
            # check the actual pSymbolName values in bse_fo.csv.
            _process(bse_text, "bse_fo", ("SENSEX",))
        import sys
        if method_counts:
            print(f"  [kotak] expiry resolution methods used: {method_counts} "
                 f"(documented-offset = confirmed via Kotak's official "
                 f"scrip-master docs; string-fallback = safety net, "
                 f"shouldn't normally trigger)", file=sys.stderr)
        if skipped_bad_expiry:
            print(f"  [kotak] skipped rows with no plausible expiry "
                 f"(neither date string nor numeric field usable): "
                 f"{skipped_bad_expiry}", file=sys.stderr)

    IDX = {"NIFTY": "nse_cm|Nifty 50", "BANKNIFTY": "nse_cm|Nifty Bank",
           "FINNIFTY": "nse_cm|Nifty Fin Service", "SENSEX": "bse_cm|SENSEX"}

    def _quotes(self, queries, filter_name="all"):
        # pacing now handled once, globally, in _get() — see _rate_lock
        url = f"{self.base}/script-details/1.0/quotes/neosymbol/{','.join(queries)}/{filter_name}"
        out = self._get(urllib.parse.quote(url, safe=":/|,"))
        if isinstance(out, dict) and out.get("stat") == "Not_Ok":
            raise RuntimeError(f"Kotak quote error: {out.get('emsg')}")
        return out

    def option_chain(self, symbol: str) -> dict:
        self._load_master()
        idx_q = self._quotes([self.IDX[symbol]])[0]
        spot = float(idx_q["ltp"])
        # Kotak's index quote includes native change/% directly — use it
        # instead of deriving from candles (which Kotak doesn't provide),
        # so the ticker's % change works without needing Dhan at all.
        try:
            idx_chg = float(idx_q.get("change") or 0) or None
            idx_pct = float(idx_q.get("per_change") or 0) or None
        except (TypeError, ValueError):
            idx_chg = idx_pct = None
        opts = self._master.get(symbol) or []
        if not opts:
            raise RuntimeError(
                f"no {symbol} contracts found in the cached Kotak master — "
                f"the pSymbolName filter may not match (expected "
                f"'{symbol}' in the CSV's pSymbolName column)")
        import time as _t
        now = _t.time()
        valid_expiries = [o["expiry"] for o in opts if o["expiry"] > now - 86400]
        if not valid_expiries:
            sample = sorted({o["expiry"] for o in opts})[:5]
            raise RuntimeError(
                f"found {len(opts)} {symbol} contracts but none has a "
                f"usable expiry (all parsed as 0 or already past) — sample "
                f"raw expiry values: {sample}. Likely cause: the "
                f"'lExpiryDate' column wasn't read correctly for this "
                f"symbol's rows, or {symbol} weekly contracts have been "
                f"discontinued (SEBI moved several indices to monthly-only "
                f"weeklies) and only far-dated monthly rows exist under a "
                f"different name. Re-run test_kotak.py and inspect the raw "
                f"{symbol} rows to confirm.")
        expiry = min(valid_expiries)
        cur = [o for o in opts if o["expiry"] == expiry]
        strikes = sorted({o["strike"] for o in cur})
        if not strikes:
            raise RuntimeError(f"{symbol}: nearest expiry {expiry} has no "
                               "strikes — master data looks incomplete")
        atm = min(strikes, key=lambda x: abs(x - spot))
        i = strikes.index(atm)
        window = set(strikes[max(0, i - 8):i + 9])
        sel = [o for o in cur if o["strike"] in window]
        qmap = {}
        qs = [f"{o.get('segment', 'nse_fo')}|{o['token']}" for o in sel]
        for j in range(0, len(qs), 25):
            for q in self._quotes(qs[j:j + 25]):
                qmap[str(q.get("exchange_token"))] = q
        # Kotak documents 'oi' as a filter SEPARATE from 'all' (not a
        # field included in the /all response) — if the /all pass came
        # back with no OI on any strike, make one supplementary /oi call
        # per batch rather than assuming OI is genuinely all-zero, which
        # is not realistic for a liquid index chain.
        any_oi = any(
            (q.get("open_interest") or q.get("oi")) for q in qmap.values())
        if not any_oi and qmap:
            oimap = {}
            for j in range(0, len(qs), 25):
                try:
                    for q in self._quotes(qs[j:j + 25], filter_name="oi"):
                        oimap[str(q.get("exchange_token"))] = q
                except Exception:
                    pass
            for tok, oi_q in oimap.items():
                if tok in qmap:
                    qmap[tok] = {**qmap[tok], **oi_q}
        rows = {}
        for o in sel:
            q = qmap.get(str(o["token"])) or {}
            # OI field name unconfirmed under /all — detect any oi-ish key
            oi_val = q.get("open_interest") or q.get("oi")
            if oi_val is None:
                for k, v in q.items():
                    if isinstance(k, str) and ("oi" == k.lower() or
                       "open_interest" in k.lower() or "openinterest" in k.lower()):
                        oi_val = v; break
            leg = {"ltp": float(q.get("ltp") or 0) or None,
                   "oi": int(float(oi_val or 0)),   # 0 is a valid OI, not "missing"
                   "oi_chg": None,
                   "volume": int(float(q.get("last_volume") or 0)),
                   "iv": None,
                   "chg": float(q.get("change") or 0),
                   "bid": None, "ask": None,
                   "security_id": o["token"],
                   "delta": None, "theta": None, "gamma": None, "vega": None}
            d = (q.get("depth") or {})
            try:
                leg["bid"] = float(((d.get("buy") or [{}])[0]).get("price") or 0) or None
                leg["ask"] = float(((d.get("sell") or [{}])[0]).get("price") or 0) or None
            except Exception:
                pass
            rows.setdefault(o["strike"], {"strike": o["strike"], "ce": {}, "pe": {}})
            rows[o["strike"]][o["type"].lower()] = leg
        from datetime import datetime
        return {"symbol": symbol, "spot": spot,
                "expiry": datetime.fromtimestamp(expiry).strftime("%Y-%m-%d"),
                "rows": [rows[k] for k in sorted(rows)], "atm": atm,
                "chg": idx_chg, "chg_pct": idx_pct}

    def intraday(self, symbol: str, interval: str = "5") -> dict:
        raise RuntimeError("Kotak REST has no candle endpoint here — regime "
                           "agent needs Dhan; select Dhan or run without regime")


class KotakNeoOrders:
    def __init__(self, client: "KotakNeoClient"):
        self.c = client

    def place(self, symbol, security_id, side, qty, price=None):
        body = json.dumps({"tk": str(security_id), "tt": "B" if side == "BUY" else "S",
                           "qt": str(qty), "pt": "MKT", "pc": "MIS",
                           "es": "bfo_fo" if symbol == "SENSEX" else "nse_fo"}).encode()
        req = urllib.request.Request(
            self.c.BASE + "/Orders/2.0/quick/order/rule/ms/place",
            data=body, headers=self.c._headers())
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read())
        return (out.get("nOrdNo") or out.get("data", {}).get("orderId")
                or str(out))


BROKERS = {
    "dhan":    (DhanClient, DhanOrders),
    "zerodha": (ZerodhaClient, ZerodhaOrders),
    "kotak":   (KotakNeoClient, KotakNeoOrders),
}


def get_active_broker():
    """Return (ClientClass, OrdersClass) for the broker chosen in Settings."""
    name = _config.load().get("broker", "dhan")
    return BROKERS.get(name, BROKERS["dhan"])
