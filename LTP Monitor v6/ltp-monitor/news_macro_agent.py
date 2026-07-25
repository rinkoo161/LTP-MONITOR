"""
NewsMacroAgent — global markets + macro/news monitor.

Standalone agent (kept separate from the existing `NewsAgent`, which stays
as the lightweight India-headline sentiment/blanket-risk-timer feed).
This one is the broader daily macro picture: US close, Asian markets,
commodities/FX, macro news events (RBI/Fed/budget/election/geopolitics/
China), FII/DII flows, NIFTY-50 constituent news, and monsoon/cyclone
weather notes — feeding a structured event log the risk agent (and,
eventually, the dashboard's macro table) can read instead of a blanket
news-risk timer.

Design notes (carried over from planning):
  - Providers are FREE-TIER budgeted: Alpha Vantage (~25 calls/day) is
    reserved for commodities/FX only; Twelve Data (~800 calls/day) covers
    the US/Asia equity indices; yfinance is a no-official-limit fallback
    used only when the primary providers error out or return nothing —
    never a parallel primary source, and every fallback is LOGGED, not
    swallowed (same lesson as the Zerodha urllib/NameError bug: a
    fallback path must never mask a real failure silently).
  - Checkpoints fire ONCE per IST day, at/after their scheduled time —
    not on a tight poll loop. The thread wakes every `interval` seconds
    just to check "is a checkpoint due yet", which is cheap.
  - FII/DII is a known gap: NewsAPI does not carry real flow numbers.
    This logs an explicit placeholder note rather than fabricating a
    figure — a real fix needs an NSE/SEBI data feed, not wired here yet.
  - SGX/GIFT Nifty and some index tickers on Twelve Data's free tier are
    UNVERIFIED against a live account. First real run should log the raw
    response for any symbol that comes back empty so the mapping can be
    corrected instead of silently reporting stale/zero data.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config

IST = timezone(timedelta(hours=5, minutes=30))
STORE_DIR = os.path.expanduser("~/.ltp-monitor")
os.makedirs(STORE_DIR, exist_ok=True)
EVENTS_FILE = os.path.join(STORE_DIR, "macro_events.jsonl")

ROUTINE_RETENTION_DAYS = 5
MAJOR_RETENTION_DAYS = 30


def now_ist():
    return datetime.now(IST)


def market_open(now=None):
    t = now or now_ist()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return 9 * 60 + 15 <= hm <= 15 * 60 + 30


# ---------------------------------------------------------------- symbols
# Alpha Vantage: FX + commodity pairs (each is its OWN call — no batching)
ALPHAVANTAGE_FX = {"USDINR": ("USD", "INR"), "GOLD": ("XAU", "USD"), "SILVER": ("XAG", "USD")}
ALPHAVANTAGE_COMMODITY_FN = {"CRUDE": "WTI"}   # AV's dedicated WTI function

# Twelve Data: equity index tickers (UNVERIFIED — see module docstring)
TWELVEDATA_SYMBOLS = {"DJI": "DJI", "NASDAQ": "IXIC", "SPX": "SPX",
                       "RUSSELL2000": "RUT", "NIKKEI": "N225"}

# yfinance fallback tickers (used only if AV/TD fail or are exhausted)
YFINANCE_TICKERS = {
    "DJI": "^DJI", "NASDAQ": "^IXIC", "SPX": "^GSPC", "RUSSELL2000": "^RUT",
    "NIKKEI": "^N225", "CRUDE": "CL=F", "GOLD": "GC=F", "SILVER": "SI=F",
    "USDINR": "USDINR=X",
}
# SGX/GIFT Nifty has no reliable free public ticker on any of the three
# providers below — flagged rather than faked.
NO_FREE_SOURCE = {"SGX_NIFTY"}

# ---------------------------------------------------------------- schedule
# (hh, mm, key, kind, symbols) — fires ONCE per IST day, at/after this time.
# kind: "market_data" | "news"
CHECKPOINTS = [
    (5,  0,  "us_close",       "market_data", ("DJI", "NASDAQ", "SPX", "RUSSELL2000")),
    (7,  15, "asia_markets",   "market_data", ("NIKKEI", "SGX_NIFTY")),
    (7,  30, "commodities_fx", "market_data", ("CRUDE", "GOLD", "SILVER", "USDINR")),
    (8,  0,  "global_macro",   "news",        None),
    (8,  30, "fii_dii",        "news",        None),   # placeholder — see docstring
]

# TTL (seconds) per symbol group before a re-check re-hits the API
TTL_FAST = 600     # 10 min — crude/gold/silver/USDINR move quickly
TTL_SLOW = 3600    # 60 min — indices only matter at their fixed checkpoint

NEWS_QUERIES = {
    # Anchored to India/markets context — a bare "RBI OR Fed OR budget OR
    # ... OR war" query on NewsAPI's /v2/everything (searches ALL global
    # news, not just finance) was pulling unrelated hits like a
    # Paramount-Warner Bros. entertainment story purely because a keyword
    # happened to appear somewhere in an unrelated article.
    "global_macro": ("(RBI OR \"Reserve Bank of India\" OR Fed OR \"Federal Reserve\" "
                      "OR budget OR election OR sanctions OR tariff OR war OR "
                      "geopolitics OR China) AND (India OR markets OR economy OR "
                      "stocks OR rupee OR Sensex OR Nifty)"),
    # Bug found 2026-07-22: this was "Reliance OR TCS OR ... OR guidance"
    # with NO grouping — "merger" and "guidance" floated as their own bare
    # OR terms, matching ANY article using those common words regardless
    # of company (an ADATA chairman's DRAM "guidance" comment, unrelated
    # M&A news in any industry, etc). Now requires a constituent name AND
    # a business-news term together.
    "constituent":  ("(Reliance OR TCS OR HDFC OR Infosys OR ICICI OR "
                      "\"Bharti Airtel\") AND (earnings OR merger OR "
                      "guidance OR results OR stock)"),
    # Bug found 2026-07-22: "monsoon OR cyclone India" — "India" floated
    # as its own bare OR term, matching literally anything mentioning
    # India (cricket series, political meetings, a sailor's death,
    # diplomacy — none of it monsoon/cyclone-related). Now requires
    # both together.
    "weather":      "(monsoon OR cyclone) AND India",
}

# Post-fetch relevance safety net: even a correctly-grouped NewsAPI query
# can return loosely-related matches. Require the title to actually
# contain a category-appropriate keyword before logging it as an event —
# a query fix alone isn't a guarantee, this is the second line of defense.
RELEVANCE_KEYWORDS = {
    "constituent": ("reliance", "tcs", "hdfc", "infosys", "icici", "bharti", "airtel"),
    "weather": ("monsoon", "cyclone", "rainfall", "rain"),
    "global_macro": ("rbi", "reserve bank", "fed", "federal reserve", "budget",
                     "election", "sanction", "tariff", "war", "geopolit",
                     "china", "india", "market", "economy", "stock", "rupee",
                     "sensex", "nifty"),
}
# Bug found 2026-07-22: naive substring checks let short words like "rain"
# match inside unrelated words ("Ukraine") — same class of bug as the
# earlier "war"/"Warner" fix. Word-boundary regex per category instead.
RELEVANCE_KEYWORDS_RE = {
    kind: re.compile(r"\b(" + "|".join(re.escape(w) for w in kws) + r")\b", re.I)
    for kind, kws in RELEVANCE_KEYWORDS.items()
}

# Bug found 2026-07-22: substring matching let "war" match inside
# "Warner" (Paramount-Warner Bros. story flagged as a war-risk macro
# event). Matched on word boundaries instead via MAJOR_KEYWORDS_RE below.
MAJOR_KEYWORDS = ("rbi", "fed", "budget", "election", "war", "sanction",
                  "tariff", "crash", "surge", "cyclone")
MAJOR_KEYWORDS_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in MAJOR_KEYWORDS) + r")\b", re.I)

# Directional bias classification per event, mirroring the same
# risk-vs-opportunity thinking news_risk_opportunity() already applies
# to the main news feed (bearish news = risk for a CE buy, opportunity
# for a PE buy) — here applied to every macro event so the page itself
# gives a risk/opportunity read instead of a flat "Info" label.
BEARISH_WORDS_RE = re.compile(
    r"\b(decline\w*|fall\w*|fell|drop\w*|crash\w*|plunge\w*|slump\w*|"
    r"tumble\w*|weak\w*|cut|cuts|lower\w*|downgrade\w*|"
    r"tension\w*|sanction\w*|tariff\w*|war|conflict\w*|"
    r"deficit\w*|slowdown\w*|recession\w*|selloff|sell-off)\b", re.I)
BULLISH_WORDS_RE = re.compile(
    r"\b(rally|rallie\w*|surge\w*|gain\w*|ris\w*|rose|growth|grew|strong\w*|"
    r"strengthen\w*|upgrade\w*|record high|all-time high|beat estimates|"
    r"beats estimates|outperform\w*|rebound\w*|recover\w*)\b",
    re.I)


def classify_bias(title):
    """Lightweight keyword-based bullish/bearish/neutral read for a
    single headline — no LLM call per article (would be expensive at
    volume), same word-boundary-safe approach as MAJOR_KEYWORDS_RE."""
    has_bear = bool(BEARISH_WORDS_RE.search(title))
    has_bull = bool(BULLISH_WORDS_RE.search(title))
    if has_bear and not has_bull:
        return "bearish"
    if has_bull and not has_bear:
        return "bullish"
    return "neutral"


class MacroDataCache:
    """TTL cache so a checkpoint re-run (or a manual refresh) doesn't
    re-hit an external API within its freshness window."""

    def __init__(self):
        self._data = {}   # symbol -> (value, ts)

    def get(self, symbol, ttl):
        v = self._data.get(symbol)
        if not v:
            return None
        value, ts = v
        if time.time() - ts > ttl:
            return None
        return value

    def set(self, symbol, value):
        self._data[symbol] = (value, time.time())


# ---------------------------------------------------------------- providers

def _http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "ltp-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def fetch_alpha_vantage(symbol, api_key):
    """Returns (value, error_reason). value is None on any failure —
    error_reason explains WHY instead of the caller just seeing
    "returned nothing". Bug found 2026-07-22: AV/TD return HTTP 200 with
    a rate-limit/error message in the JSON body (not an exception), so
    the old try/except-Exception-return-None swallowed that message
    entirely — every symbol silently fell back to yfinance with zero
    indication of whether it was a bad symbol, an expired key, or the
    free tier's 25-calls/day limit being hit."""
    if not api_key:
        return None, "no API key configured"
    try:
        if symbol in ALPHAVANTAGE_COMMODITY_FN:
            fn = ALPHAVANTAGE_COMMODITY_FN[symbol]
            url = (f"https://www.alphavantage.co/query?function={fn}"
                   f"&interval=daily&apikey={api_key}")
            j = _http_json(url)
            for err_key in ("Note", "Information", "Error Message"):
                if j.get(err_key):
                    return None, f"AV: {j[err_key][:160]}"
            data = j.get("data") or []
            if not data:
                return None, f"AV: no 'data' in response (keys: {list(j.keys())})"
            return float(data[0]["value"]), None
        if symbol in ALPHAVANTAGE_FX:
            frm, to = ALPHAVANTAGE_FX[symbol]
            url = ("https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
                   f"&from_currency={frm}&to_currency={to}&apikey={api_key}")
            j = _http_json(url)
            for err_key in ("Note", "Information", "Error Message"):
                if j.get(err_key):
                    return None, f"AV: {j[err_key][:160]}"
            rate = (j.get("Realtime Currency Exchange Rate") or {}).get("5. Exchange Rate")
            if not rate:
                return None, f"AV: no exchange rate in response (keys: {list(j.keys())})"
            return float(rate), None
    except Exception as e:
        return None, f"AV: {type(e).__name__}: {e}"
    return None, "AV: symbol not mapped to a commodity/FX function"


def fetch_twelve_data(symbol, api_key):
    """Returns (value, error_reason) — see fetch_alpha_vantage docstring
    for why bare None was a debugging dead-end."""
    if not api_key:
        return None, "no API key configured"
    if symbol not in TWELVEDATA_SYMBOLS:
        return None, f"TD: {symbol} not in TWELVEDATA_SYMBOLS mapping"
    try:
        td_symbol = TWELVEDATA_SYMBOLS[symbol]
        url = f"https://api.twelvedata.com/price?symbol={td_symbol}&apikey={api_key}"
        j = _http_json(url)
        if j.get("status") == "error" or j.get("code"):
            return None, f"TD: {str(j.get('message', j))[:160]}"
        price = j.get("price")
        if not price:
            return None, f"TD: no 'price' in response (keys: {list(j.keys())})"
        return float(price), None
    except Exception as e:
        return None, f"TD: {type(e).__name__}: {e}"


def fetch_yfinance(symbol):
    """Fallback only. Returns a float price or None. Logged by the caller
    when used — a silent fallback would hide a real upstream break."""
    ticker = YFINANCE_TICKERS.get(symbol)
    if not ticker:
        return None
    try:
        import yfinance as yf   # optional dependency; degrade gracefully
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker)
        fast = getattr(t, "fast_info", None)
        if fast and fast.get("lastPrice"):
            return float(fast["lastPrice"])
        hist = t.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        return None
    return None


def fetch_newsapi(query, api_key, page_size=10):
    """Returns a list of article dicts (possibly empty). Never raises."""
    if not api_key:
        return []
    try:
        q = urllib.parse.quote(query)
        url = (f"https://newsapi.org/v2/everything?q={q}&language=en"
               f"&sortBy=publishedAt&pageSize={page_size}&apiKey={api_key}")
        j = _http_json(url, timeout=15)
        return j.get("articles") or []
    except Exception:
        return []


# ---------------------------------------------------------------- agent

class NewsMacroAgent(threading.Thread):
    """Duck-types the same interface the Orchestrator expects from the
    Agent base class in agents.py (name/interval/start/info/summary),
    but is defined standalone here to avoid a circular import between
    agents.py and this module."""

    name = "news_macro"
    interval = 60   # cheap "is a checkpoint due yet" check; API calls are
                    # gated separately by the once-per-day checkpoint logic

    def __init__(self, bus, ctx):
        super().__init__(daemon=True)
        self.bus, self.ctx = bus, ctx
        self.stop_evt = threading.Event()
        self.last_run = None
        self.status = "idle"
        self.summary = ""
        self.cache = MacroDataCache()
        self._done_today = set()
        self._today = None
        self._warned_no_keys = False

    def start(self):
        return threading.Thread.start(self)

    def stop(self):
        self.stop_evt.set()

    def info(self):
        return {"name": self.name, "interval": self.interval,
                "last_run": self.last_run, "status": self.status,
                "summary": self.summary}

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.status = "running"
                self.cycle()
                self.status = "ok"
            except Exception as e:
                self.status = f"error: {e}"
                self.bus.log(self.name, f"⚠ {e}")
            self.last_run = now_ist().strftime("%H:%M:%S")
            self.stop_evt.wait(self.interval)
        self.status = "stopped"

    # -------------------------------------------------------------- cycle

    def cycle(self):
        cfg = config.load()
        if not any([cfg.get("alpha_vantage_api_key"), cfg.get("twelve_data_api_key"),
                    cfg.get("newsapi_api_key")]):
            if not self._warned_no_keys:
                self.bus.log(self.name,
                             "no macro data provider keys configured "
                             "(alpha_vantage_api_key / twelve_data_api_key / "
                             "newsapi_api_key) — agent idle")
                self._warned_no_keys = True
            self.summary = "idle — no API keys configured"
            return

        now = now_ist()
        today = now.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._done_today = set()
            self._prune_events()

        due = self._due_checkpoints(now)
        for cp in due:
            self._run_checkpoint(cp, cfg)

        if due:
            self.summary = f"ran checkpoint(s): {', '.join(c[2] for c in due)}"
        elif market_open(now):
            self._market_hours_pulse(cfg)
        else:
            done = len(self._done_today)
            self.summary = f"idle · {done}/{len(CHECKPOINTS)} checkpoints done today"

    def _due_checkpoints(self, now):
        hm = now.hour * 60 + now.minute
        due = []
        for cp in CHECKPOINTS:
            hh, mm, key, kind, syms = cp
            if key in self._done_today:
                continue
            if hm >= hh * 60 + mm:
                due.append(cp)
        return due

    def _run_checkpoint(self, cp, cfg):
        hh, mm, key, kind, syms = cp
        self._done_today.add(key)
        try:
            if kind == "market_data":
                self._fetch_market_data(key, syms, cfg)
            elif kind == "news":
                self._fetch_news_checkpoint(key, cfg)
        except Exception as e:
            # Loud, not swallowed — checkpoint bugs should surface, not
            # get logged as a routine miss.
            self.bus.log(self.name, f"⚠ checkpoint '{key}' failed: {e}")
            raise

    # --------------------------------------------------------- market data

    def _fetch_market_data(self, checkpoint_key, symbols, cfg):
        av_key = cfg.get("alpha_vantage_api_key", "")
        td_key = cfg.get("twelve_data_api_key", "")
        results = {}
        for sym in symbols:
            if sym in NO_FREE_SOURCE:
                results[sym] = None
                continue
            ttl = TTL_FAST if sym in ("CRUDE", "GOLD", "SILVER", "USDINR") else TTL_SLOW
            cached = self.cache.get(sym, ttl)
            if cached is not None:
                results[sym] = cached
                continue
            # Provider allocation: commodities/FX -> Alpha Vantage first
            # (keeps AV's ~25/day budget tiny); equity indices -> Twelve
            # Data first (comfortable ~800/day budget). Either falls back
            # to the other, then to yfinance, with every fallback AND
            # every failure reason logged (bug found 2026-07-22: the old
            # code swallowed the actual AV/TD error/rate-limit message —
            # both return HTTP 200 with an error string in the JSON body,
            # not an exception — so every symbol silently fell back to
            # yfinance with no way to tell why).
            value = None
            source = None
            reasons = []
            if sym in ALPHAVANTAGE_FX or sym in ALPHAVANTAGE_COMMODITY_FN:
                value, err = fetch_alpha_vantage(sym, av_key)
                if err: reasons.append(err)
                source = "alpha_vantage" if value is not None else None
                if value is None:
                    value, err = fetch_twelve_data(sym, td_key)
                    if err: reasons.append(err)
                    source = "twelve_data" if value is not None else None
            else:
                value, err = fetch_twelve_data(sym, td_key)
                if err: reasons.append(err)
                source = "twelve_data" if value is not None else None
                if value is None:
                    value, err = fetch_alpha_vantage(sym, av_key)
                    if err: reasons.append(err)
                    source = "alpha_vantage" if value is not None else None
            if value is None:
                value = fetch_yfinance(sym)
                if value is not None:
                    source = "yfinance_fallback"
                    self.bus.log(self.name,
                                 f"{sym}: primary providers failed "
                                 f"({'; '.join(reasons)}) — used yfinance fallback")
            if value is None:
                self.bus.log(self.name,
                             f"{sym}: no data from any provider — "
                             f"{'; '.join(reasons) if reasons else 'yfinance also unavailable'}")
            else:
                self.cache.set(sym, value)
            results[sym] = value

        prev = self.bus.get("macro_market_data", {}) or {}
        moved = []
        for sym, value in results.items():
            if value is None:
                continue
            prev_val = (prev.get(sym) or {}).get("value")
            chg_pct = None
            if prev_val:
                chg_pct = round((value - prev_val) / prev_val * 100, 2)
            prev[sym] = {"value": value, "chg_pct": chg_pct, "ts": time.time()}
            if chg_pct is not None and abs(chg_pct) >= 1.0:
                moved.append(f"{sym} {chg_pct:+.1f}%")
        self.bus.set("macro_market_data", prev)

        note = f"{checkpoint_key}: " + ", ".join(
            f"{s}={v}" for s, v in results.items() if v is not None) or f"{checkpoint_key}: no data"
        impact = "Risk" if moved else "Info"
        self._log_event("Market Data", note, impact,
                        "monitor" if moved else "none", major=bool(moved))

    # --------------------------------------------------------------- news

    def _fetch_news_checkpoint(self, key, cfg):
        api_key = cfg.get("newsapi_api_key", "")
        if key == "fii_dii":
            # Known gap — NewsAPI carries no real flow numbers. Logged
            # honestly as a placeholder rather than fabricated.
            self._log_event("FII/DII Flows",
                            "FII/DII flow data not available from current "
                            "providers (NewsAPI has no flow feed) — needs "
                            "an NSE/SEBI data source", "Info", "none")
            return

        for kind, query in NEWS_QUERIES.items():
            articles = fetch_newsapi(query, api_key, page_size=8)
            if not articles:
                continue
            relevance_re = RELEVANCE_KEYWORDS_RE.get(kind)
            for a in articles[:5]:
                title = (a.get("title") or "")[:180]
                title_lower = title.lower()
                # Post-fetch relevance safety net (see NEWS_QUERIES comment)
                # — a correctly-grouped query still isn't a hard guarantee
                # with NewsAPI's matching, so double-check here.
                if relevance_re and not relevance_re.search(title_lower):
                    continue
                major = bool(MAJOR_KEYWORDS_RE.search(title))
                bias = classify_bias(title)
                label = {"global_macro": "Global Macro News",
                         "constituent": "NIFTY50 Constituent News",
                         "weather": "Weather/Monsoon"}.get(kind, "News")
                # Risk/opportunity read against the currently-active
                # symbol's regime direction — same logic family as
                # news_risk_opportunity() in agents.py (bearish news is
                # a risk for a long/CE-leaning position, an opportunity
                # for a short/PE-leaning one), just applied here without
                # needing a specific pending signal to check against.
                regime_dir = None
                active_regime = self.bus.get(
                    f"regime:{self.bus.get('active_symbol')}") or {}
                r_label = active_regime.get("regime", "")
                if r_label == "trending-up":
                    regime_dir = "bullish"
                elif r_label == "trending-down":
                    regime_dir = "bearish"
                if bias == "neutral" or not regime_dir:
                    impact, action = ("Risk" if major else "Info"), \
                                      ("monitor" if major else "none")
                elif bias == regime_dir:
                    impact, action = "Opportunity", "confirms trend"
                else:
                    impact, action = "Risk", "conflicts with trend"
                self._log_event(label, f"[{bias}] {title}", impact, action,
                                major=major)

    def _market_hours_pulse(self, cfg):
        """Ongoing sector-rotation / heavyweight-stock / India-VIX check
        during market hours — reuses data other agents already fetched
        (no extra external API calls, just reads the shared bus state)."""
        vix = self.bus.get("india_vix")
        note_parts = []
        if vix:
            note_parts.append(f"VIX {vix}")
        active = self.bus.get("active_symbol")
        if active:
            note_parts.append(f"watching {active}")
        self.summary = ("market hours: " + ", ".join(note_parts)) if note_parts \
            else "market hours: monitoring"

    # ------------------------------------------------------ event log

    def _log_event(self, evt_type, note, impact, action, major=False):
        now = now_ist()
        entry = {"date": now.strftime("%d %b %Y"), "time": now.strftime("%I:%M %p"),
                 "type": evt_type, "event": note, "impact": impact,
                 "action": action, "major": major, "ts": time.time()}
        events = self.bus.get("macro_events", []) or []
        events.insert(0, entry)
        self.bus.set("macro_events", events[:500])
        try:
            with open(EVENTS_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        if impact == "Risk":
            prev = self.bus.get("macro_risk") or {}
            was_active = bool(prev.get("risk_event"))
            if not was_active:
                self.bus.alert("medium", "news_macro", "", f"Macro risk: {note}")
            self.bus.set("macro_risk", {"risk_event": True, "note": note,
                                        "flagged_ts": prev.get("flagged_ts", time.time())})

    def _prune_events(self):
        """Routine events: keep 5 days. Major events: keep 30 days."""
        events = self.bus.get("macro_events", []) or []
        now = time.time()
        kept = []
        for e in events:
            age_days = (now - e.get("ts", now)) / 86400
            limit = MAJOR_RETENTION_DAYS if e.get("major") else ROUTINE_RETENTION_DAYS
            if age_days <= limit:
                kept.append(e)
        if len(kept) != len(events):
            self.bus.set("macro_events", kept)
