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
