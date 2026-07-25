"""
Data clients for NSE (NIFTY / BANKNIFTY / FINNIFTY) and BSE (SENSEX) option chains.

NSE's public JSON endpoints require a browser-like session (cookies from the
homepage) and will throttle aggressive polling. Data on these endpoints is a
snapshot refreshed roughly every 1-3 minutes by the exchange -- it is "near
real-time". For tick-level real-time you must plug in a broker feed
(see broker_adapter.py stub).
"""

import time
import threading
import requests

# curl_cffi impersonates a real Chrome browser's TLS fingerprint, which is
# what NSE's Akamai bot-protection actually checks. Plain `requests` gets
# blocked even with browser headers. Falls back to requests if unavailable.
try:
    from curl_cffi import requests as creq
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

NSE_BASE = "https://www.nseindia.com"
# NSE is migrating endpoints; try classic first, then v3.
NSE_OC_URLS = [
    NSE_BASE + "/api/option-chain-indices?symbol={symbol}",
    NSE_BASE + "/api/option-chain-v3?type=Indices&symbol={symbol}",
]

BSE_OC_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry_IV/w?"
    "ProductType=IO&scrip_cd=1&ExpiryDate="
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive",
}

CACHE_TTL = 25  # seconds; keep polite to the exchange


class NSEClient:
    def __init__(self):
        self._session = None
        self._session_ts = 0
        self._cache = {}          # symbol -> (timestamp, data)
        self._lock = threading.Lock()

    # ---------------- session handling ----------------
    def _new_session(self):
        if HAS_CURL_CFFI:
            s = creq.Session(impersonate="chrome")
            s.headers.update({"Referer": HEADERS["Referer"],
                              "Accept": HEADERS["Accept"]})
        else:
            s = requests.Session()
            s.headers.update(HEADERS)
        # Hitting the homepage + option-chain page sets the cookies NSE checks
        s.get(NSE_BASE, timeout=15)
        time.sleep(0.5)
        s.get(NSE_BASE + "/option-chain", timeout=15)
        return s

    def _get_session(self, force=False):
        if force or self._session is None or time.time() - self._session_ts > 300:
            self._session = self._new_session()
            self._session_ts = time.time()
        return self._session

    # ---------------- public API ----------------
    def option_chain(self, symbol: str) -> dict:
        """Return normalized option-chain dict for NIFTY/BANKNIFTY/FINNIFTY."""
        symbol = symbol.upper()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and time.time() - cached[0] < CACHE_TTL:
                return cached[1]

        raw = self._fetch_nse(symbol)
        data = self._normalize_nse(raw, symbol)

        with self._lock:
            self._cache[symbol] = (time.time(), data)
        return data

    def _fetch_nse(self, symbol: str) -> dict:
        last = ""
        for attempt in range(3):
            try:
                s = self._get_session(force=attempt > 0)
                for url_tpl in NSE_OC_URLS:
                    url = url_tpl.format(symbol=symbol)
                    try:
                        r = s.get(url, timeout=15)
                    except Exception as e:
                        last = str(e)
                        continue
                    last = f"HTTP {r.status_code} on {url.split('/api/')[1].split('?')[0]}"
                    if r.status_code == 200 and r.text.strip().startswith("{"):
                        j = r.json()
                        if (j.get("records") or {}).get("data"):
                            return j
            except Exception as e:
                last = str(e)
            time.sleep(1.5 * (attempt + 1))
        hint = ("" if HAS_CURL_CFFI else
                " TIP: run `pip install curl_cffi` — NSE's bot protection "
                "blocks plain Python requests.")
        raise RuntimeError(
            f"NSE did not return data for {symbol} (last: {last}). "
            "Note: FINNIFTY/MIDCPNIFTY now trade monthly-only and NSE may "
            "not publish a chain for them on all endpoints. If NIFTY also "
            "fails, wait 2-3 min (throttling) or use a broker feed."
            + hint
        )

    @staticmethod
    def _normalize_nse(raw: dict, symbol: str) -> dict:
        rec = raw.get("records", {})
        filt = raw.get("filtered", {})
        expiry = rec.get("expiryDates", [None])[0]
        spot = rec.get("underlyingValue")

        rows = []
        source = filt.get("data") or rec.get("data") or []
        for item in source:
            if expiry and item.get("expiryDate") != expiry:
                continue
            strike = item.get("strikePrice")
            ce, pe = item.get("CE") or {}, item.get("PE") or {}
            rows.append({
                "strike": strike,
                "ce": _leg(ce),
                "pe": _leg(pe),
            })
        rows.sort(key=lambda r: r["strike"])
        return {
            "symbol": symbol,
            "spot": spot,
            "expiry": expiry,
            "timestamp": rec.get("timestamp"),
            "rows": rows,
            "totals": {
                "ce_oi": filt.get("CE", {}).get("totOI"),
                "pe_oi": filt.get("PE", {}).get("totOI"),
                "ce_vol": filt.get("CE", {}).get("totVol"),
                "pe_vol": filt.get("PE", {}).get("totVol"),
            },
            "source": "NSE (snapshot, ~1-3 min delayed)",
        }


def _leg(d: dict) -> dict:
    return {
        "ltp": d.get("lastPrice", 0) or 0,
        "oi": d.get("openInterest", 0) or 0,
        "oi_chg": d.get("changeinOpenInterest", 0) or 0,
        "volume": d.get("totalTradedVolume", 0) or 0,
        "iv": d.get("impliedVolatility", 0) or 0,
        "chg": d.get("change", 0) or 0,
        "bid": d.get("bidprice", 0) or 0,
        "ask": d.get("askPrice", 0) or 0,
    }


class SensexClient:
    """
    Best-effort SENSEX option chain from BSE's public API. BSE's endpoints are
    less stable than NSE's; if this fails, use the broker adapter instead.
    """

    API = "https://api.bseindia.com/BseIndiaAPI/api/"
    HEADERS = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com",
    }

    def __init__(self):
        self._cache = None
        self._cache_ts = 0
        self._lock = threading.Lock()

    def option_chain(self) -> dict:
        with self._lock:
            if self._cache and time.time() - self._cache_ts < CACHE_TTL:
                return self._cache
        data = self._fetch()
        with self._lock:
            self._cache, self._cache_ts = data, time.time()
        return data

    def _fetch(self) -> dict:
        s = requests.Session()
        s.headers.update(self.HEADERS)
        try:
            # Expiry list for SENSEX index options
            r = s.get(
                self.API + "ddlExpiry_IV/w",
                params={"ProductType": "IO", "scrip_cd": "1"},
                timeout=12,
            )
            expiries = r.json().get("Table", [])
            if not expiries:
                raise RuntimeError("no expiries")
            expiry = expiries[0].get("ExpiryDate")

            r = s.get(
                self.API + "OptionChain_IV/w",
                params={"scrip_cd": "1", "strprice": "0", "expiry": expiry},
                timeout=12,
            )
            raw = r.json().get("Table", [])
        except Exception as e:
            raise RuntimeError(
                "BSE SENSEX chain unavailable via public API "
                f"({e}). Use a broker feed (broker_adapter.py) for SENSEX."
            )

        rows, spot = [], None
        for item in raw:
            strike = item.get("Strike_Price") or item.get("StrikePrice")
            if strike is None:
                continue
            spot = spot or item.get("UlaValue") or item.get("underlying")
            rows.append({
                "strike": float(strike),
                "ce": {
                    "ltp": _f(item, "C_LTP"), "oi": _f(item, "C_OI"),
                    "oi_chg": _f(item, "C_ChgOI"), "volume": _f(item, "C_Volume"),
                    "iv": _f(item, "C_IV"), "chg": _f(item, "C_Chg"),
                    "bid": 0, "ask": 0,
                },
                "pe": {
                    "ltp": _f(item, "P_LTP"), "oi": _f(item, "P_OI"),
                    "oi_chg": _f(item, "P_ChgOI"), "volume": _f(item, "P_Volume"),
                    "iv": _f(item, "P_IV"), "chg": _f(item, "P_Chg"),
                    "bid": 0, "ask": 0,
                },
            })
        rows.sort(key=lambda r: r["strike"])
        return {
            "symbol": "SENSEX",
            "spot": float(spot) if spot else None,
            "expiry": expiry,
            "timestamp": None,
            "rows": rows,
            "totals": {},
            "source": "BSE (best effort)",
        }


def _f(d, k):
    try:
        return float(d.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0
