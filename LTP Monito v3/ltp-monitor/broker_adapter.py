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
        data = self._post("/optionchain/expirylist", {
            "UnderlyingScrip": UNDERLYINGS[symbol],
            "UnderlyingSeg": "IDX_I",
        })
        if not data:
            raise RuntimeError(f"No active expiries for {symbol}")
        exp = sorted(data)[0]
        self._expiry[symbol] = (time.time(), exp)
        return exp

    # ------------------------------------------------------------- fast quotes
    def quote_ltp(self, symbols=None) -> dict:
        """All index spots in ONE call via /marketfeed/ltp (separate, faster
        rate limit than the option chain). Cached ~2.5s."""
        symbols = symbols or list(UNDERLYINGS.keys())
        cached = self._cache.get("ltp_all")
        if cached and time.time() - cached[0] < 2.5:
            return cached[1]
        body = {"IDX_I": [UNDERLYINGS[s] for s in symbols]}
        r = requests.post(API + "/marketfeed/ltp", json=body,
                          headers=self._h, timeout=10)
        r.raise_for_status()
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
    def option_chain(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in UNDERLYINGS:
            raise RuntimeError(f"Unknown symbol {symbol}")

        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]

        expiry = self._nearest_expiry(symbol)
        data = self._post("/optionchain", {
            "UnderlyingScrip": UNDERLYINGS[symbol],
            "UnderlyingSeg": "IDX_I",
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

    def _get(self, url):
        req = urllib.request.Request(url, headers={
            "Authorization": self.api_token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _load_master(self):
        if self._master is not None:
            return
        import csv, io, os
        from datetime import date, timedelta
        cache = os.path.expanduser("~/.ltp-monitor/kotak_nse_fo.csv")
        today = date.today().isoformat()
        text = None
        if os.path.exists(cache) and open(cache).readline().strip() == today:
            text = open(cache).read().split("\n", 1)[1]
        else:
            for back in range(6):
                dt = (date.today() - timedelta(days=back)).isoformat()
                try:
                    with urllib.request.urlopen(
                        f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/{dt}/transformed/nse_fo.csv",
                        timeout=60) as r:
                        text = r.read().decode(errors="ignore")
                    open(cache, "w").write(today + "\n" + text)
                    break
                except Exception:
                    continue
        if not text:
            raise RuntimeError("Kotak scrip master unavailable")
        self._master = {}
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("pSymbolName") or "").strip()
            if name in ("NIFTY", "BANKNIFTY", "FINNIFTY") and \
               (row.get("pOptionType") or "").strip() in ("CE", "PE"):
                self._master.setdefault(name, []).append({
                    "token": row["pSymbol"],
                    "type": row["pOptionType"].strip(),
                    "strike": float(row.get("dStrikePrice;") or 0) / 100.0,
                    "lot": int(row.get("lLotSize") or 0),
                    "expiry": int(row.get("lExpiryDate ") or row.get("lExpiryDate") or 0),
                    "tsym": row.get("pTrdSymbol"),
                })

    IDX = {"NIFTY": "nse_cm|Nifty 50", "BANKNIFTY": "nse_cm|Nifty Bank",
           "FINNIFTY": "nse_cm|Nifty Fin Service", "SENSEX": "bse_cm|SENSEX"}

    def _quotes(self, queries):
        url = f"{self.base}/script-details/1.0/quotes/neosymbol/{','.join(queries)}/all"
        out = self._get(urllib.parse.quote(url, safe=":/|,"))
        if isinstance(out, dict) and out.get("stat") == "Not_Ok":
            raise RuntimeError(f"Kotak quote error: {out.get('emsg')}")
        return out

    def option_chain(self, symbol: str) -> dict:
        if symbol == "SENSEX":
            raise RuntimeError("Kotak nse_fo master lacks SENSEX (BFO) — "
                               "use Dhan for SENSEX or extend to bse_fo master")
        self._load_master()
        spot = float(self._quotes([self.IDX[symbol]])[0]["ltp"])
        opts = self._master.get(symbol) or []
        if not opts:
            raise RuntimeError("no contracts for " + symbol)
        import time as _t
        expiry = min(o["expiry"] for o in opts if o["expiry"] > _t.time() - 86400)
        cur = [o for o in opts if o["expiry"] == expiry]
        strikes = sorted({o["strike"] for o in cur})
        atm = min(strikes, key=lambda x: abs(x - spot))
        i = strikes.index(atm)
        window = set(strikes[max(0, i - 8):i + 9])
        sel = [o for o in cur if o["strike"] in window]
        qmap = {}
        qs = [f"nse_fo|{o['token']}" for o in sel]
        for j in range(0, len(qs), 25):
            for q in self._quotes(qs[j:j + 25]):
                qmap[str(q.get("exchange_token"))] = q
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
                   "oi": int(float(oi_val or 0)) or None,
                   "oi_chg": None,
                   "volume": int(float(q.get("last_volume") or 0)) or None,
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
                "rows": [rows[k] for k in sorted(rows)], "atm": atm}

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
