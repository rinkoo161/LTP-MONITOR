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
import store
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import news_engine as ne

IST = timezone(timedelta(hours=5, minutes=30))
STORE_DIR = store.home()
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
# 2026-08-02 — symbols are now the canonical keys from
# config["macro_symbols"]. US CASH indices are replaced by the e-mini
# FUTURES as the primary read: ^GSPC/^DJI do not update between 09:15 and
# 15:30 IST, so a macro monitor for an Indian intraday strategy was
# reading the previous US close and presenting it as current. Cash is
# retained alongside for post-close context and flagged stale.
CHECKPOINTS = [
    (5,  0,  "us_close",       "market_data", ("SPX_FUT", "NDX_FUT", "DJI_FUT",
                                               "RUT_FUT", "SPX_CASH", "DJI_CASH")),
    (7,  15, "asia_markets",   "market_data", ("NIKKEI", "HSI", "NIFTY",
                                               "BANKNIFTY", "INDIAVIX")),
    (7,  30, "commodities_fx", "market_data", ("CRUDE_WTI", "CRUDE_BRENT",
                                               "GOLD", "SILVER", "USDINR", "DXY")),
    (8,  0,  "global_macro",   "news",        None),
    (8,  30, "fii_dii",        "news",        None),   # placeholder — see docstring
]

# ---------------------------------------------------- intra-session refresh
# 2026-08-02. The daily CHECKPOINTS all fire pre-market (05:00-08:30 IST),
# which was fine when the monitor read CASH indices — they do not move
# during the IST session anyway. It is NOT fine now that the primary read
# is the e-mini FUTURES, which were chosen precisely because they trade
# THROUGH 09:15-15:30.
#
# It was also an outright regression: `_update_global_sentiment` now
# refuses stale quotes, and the futures freshness threshold is 15 minutes.
# A quote taken at 05:00 is 4h15m old at the opening bell, so the sentiment
# input MTFConfluenceAgent reads would have been None for the entire
# session — safer than reporting yesterday's move as today's, but silent.
#
# A single extra checkpoint would not fix it: the quote would go stale 15
# minutes later. This is a REPEATING refresh, futures only, and only while
# the market is open. The daily checkpoints are untouched.
INTRASESSION_SYMBOLS = ("SPX_FUT", "NDX_FUT", "DJI_FUT", "RUT_FUT")

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
#
# 2026-07-27 — real gap found: this module maintained its OWN
# byte-identical COPY of BEARISH_WORDS_RE/BULLISH_WORDS_RE, duplicating
# news_engine.py's definitions exactly — a genuine "two copies that
# will silently drift" risk (editing one without the other would leave
# this module and the main news feed disagreeing about what counts as
# bearish/bullish, with no test or check that would catch it). Now
# references the SAME regex objects directly rather than a parallel
# copy — one source of truth for both consumers.
BEARISH_WORDS_RE = ne.BEARISH_WORDS_RE
BULLISH_WORDS_RE = ne.BULLISH_WORDS_RE


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
        self.cache = MacroDataCache()      # legacy; kept for the news path
        self._quote_cache = None           # macro_providers.QuoteCache
        self._last_quotes = {}             # symbol -> Quote, for staleness
        self._done_today = set()
        self._today = None
        self._warned_no_keys = False
        self._last_intrasession = 0.0

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
        # 2026-08-02 — this used to idle the WHOLE agent when no provider
        # key was configured. That predates the provider refactor: market
        # data now comes from yfinance first, which needs no key and
        # covers every symbol, so idling here threw away the only
        # provider that actually works. Only the NEWS half needs a key
        # now, and it says so instead of stopping everything.
        if not cfg.get("newsapi_api_key"):
            if not self._warned_no_keys:
                self.bus.log(self.name,
                             "no newsapi_api_key — macro NEWS checkpoints "
                             "will be skipped. Market data is unaffected "
                             "(yfinance needs no key).")
                self._warned_no_keys = True

        now = now_ist()
        today = now.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._done_today = set()
            self._prune_events()

        due = self._due_checkpoints(now)
        for cp in due:
            self._run_checkpoint(cp, cfg)

        # Repeating futures refresh while the session is open. Cadence is
        # deliberately shorter than the futures freshness threshold (15
        # min) so the sentiment input stays FRESH rather than flickering
        # between a value and None as each quote ages out.
        try:
            every = max(60, int(cfg.get("macro_intrasession_refresh_sec", 300)))
        except (TypeError, ValueError):
            every = 300
        if (cfg.get("macro_intrasession_enabled", True) and market_open(now)
                and time.time() - self._last_intrasession >= every):
            self._last_intrasession = time.time()
            try:
                # quiet=True: this runs ~75x a session. Writing a macro
                # EVENT every time would bury the daily checkpoints it is
                # meant to complement — only a material move is logged.
                self._fetch_market_data("intrasession", INTRASESSION_SYMBOLS,
                                        cfg, quiet=True)
            except Exception as e:
                self.bus.log(self.name, f"⚠ intra-session refresh failed: {e}")

        if due:
            self.summary = f"ran checkpoint(s): {', '.join(c[2] for c in due)}"
        elif market_open(now):
            self._market_hours_pulse(cfg)
        else:
            done = len(self._done_today)
            # 2026-07-30 -- "idle · 0/5 checkpoints done today" at 02:50
            # reads like a fault. It is not: the first checkpoint is not
            # scheduled until the US close (~05:00-07:00 IST). Naming the
            # NEXT one turns an alarming line into an informative one,
            # and the Digest panel's "no data yet" then has an obvious
            # cause rather than looking broken.
            nxt = None
            for cp in CHECKPOINTS:
                hh, mm = cp[0], cp[1]
                if (hh, mm) > (now.hour, now.minute) and cp[2] not in self._done_today:
                    nxt = f"{hh:02d}:{mm:02d} {cp[2]}"
                    break
            if nxt is None and CHECKPOINTS:
                c0 = CHECKPOINTS[0]
                nxt = f"{c0[0]:02d}:{c0[1]:02d} {c0[2]} (tomorrow)"
            self.summary = (f"idle · {done}/{len(CHECKPOINTS)} checkpoints done "
                            f"today · next: {nxt}" if nxt else
                            f"idle · {done}/{len(CHECKPOINTS)} checkpoints done today")

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

    def _fetch_market_data(self, checkpoint_key, symbols, cfg, quiet=False):
        """Fetch via the macro_providers chain. Checkpoints are UNCHANGED —
        this still fires at fixed IST times, it just batches within the
        checkpoint instead of making one keyed request per symbol.

        2026-08-02 refactor. What this replaces: an if/else that put Alpha
        Vantage FIRST for FX/commodities (25 requests per DAY, so it was
        exhausted by mid-morning), consulted Twelve Data for indices its
        free tier does not serve (the 404s), and reached yfinance — the
        only uncapped, keyless provider that covers everything — last.
        """
        import macro_providers as mprov
        import redaction as _red
        if self._quote_cache is None:
            self._quote_cache = mprov.QuoteCache()
        wanted = [s for s in symbols if s not in NO_FREE_SOURCE]

        def _log(level, msg):
            # Provider text is redacted and truncated by the chain before
            # it reaches here; INFO is the one-line cycle summary, DEBUG
            # is per-symbol and only surfaces when explicitly enabled.
            if level == "info" or cfg.get("macro_debug_logging", False):
                self.bus.log(self.name, _red.redact(msg, cfg, mprov.ERR_LIMIT))

        quotes, summary = mprov.fetch(wanted, cfg=cfg, cache=self._quote_cache,
                                      market_open=market_open(), log=_log)
        results = {s: (quotes[s].value if s in quotes else None)
                   for s in symbols}
        self._last_quotes = quotes
        if summary["failed_symbols"]:
            self.bus.log(self.name, "macro: no data for "
                         + ", ".join(summary["failed_symbols"]))

        prev = self.bus.get("macro_market_data", {}) or {}
        moved = []
        for sym, value in results.items():
            if value is None:
                continue
            prev_val = (prev.get(sym) or {}).get("value")
            chg_pct = None
            if prev_val:
                chg_pct = round((value - prev_val) / prev_val * 100, 2)
            # `value`/`chg_pct`/`ts` keep their exact meaning so existing
            # consumers are untouched; the staleness fields are ADDITIVE.
            # Never present a stale quote as current — a cash index during
            # the IST session is the previous US close, not a live number.
            q = (self._last_quotes or {}).get(sym)
            prev[sym] = {"value": value, "chg_pct": chg_pct, "ts": time.time()}
            if q is not None:
                prev[sym].update({
                    "last_updated": q.last_updated,
                    "is_stale": q.is_stale(cfg),
                    "age_sec": round(q.age()),
                    "source": q.source, "ticker": q.ticker,
                })
            if chg_pct is not None and abs(chg_pct) >= 1.0:
                moved.append(f"{sym} {chg_pct:+.1f}%")
        self.bus.set("macro_market_data", prev)
        self._update_global_sentiment(prev)

        note = f"{checkpoint_key}: " + ", ".join(
            f"{s}={v}" for s, v in results.items() if v is not None) or f"{checkpoint_key}: no data"
        impact = "Risk" if moved else "Info"
        # `quiet` suppresses the routine event; a MATERIAL move still logs,
        # because that is the thing worth interrupting someone for.
        if not quiet or moved:
            self._log_event("Market Data", note, impact,
                            "monitor" if moved else "none", major=bool(moved))

    def _update_global_sentiment(self, market_data):
        """Turns the raw DJI/NASDAQ/SPX/RUSSELL2000 numbers into an
        actual decision input — "risk_on" / "risk_off" / "neutral" —
        stored on the bus for MTFConfluenceAgent (and any future
        consumer) to read. Added 2026-07-24 per direct feedback that
        these numbers were displayed but never used for anything.

        Heuristic, not a validated model (same honesty standard as the
        impact-window classification in news_engine.py): averages
        chg_pct across whichever US indices have a fresh reading, and
        classifies risk_off if the average move is <= -0.75%, risk_on
        if >= +0.75%, else neutral. A meaningful but not extreme
        single-session move — deliberately not requiring a huge swing,
        since this is a supportive-only signal (a few points of
        confidence, never a block), not a standalone trading trigger.
        """
        # 2026-08-02 — FUTURES FIRST, cash only as fallback. The cash
        # indices this used to read are frozen at the previous US close
        # during the entire IST session, so a "global risk" input feeding
        # MTFConfluenceAgent was re-reporting yesterday's move as though
        # it were today's. The e-minis trade through the IST session and
        # are the honest read; cash is kept as a fallback so a futures
        # outage degrades rather than silently zeroing the signal.
        US_FUT = ("SPX_FUT", "NDX_FUT", "DJI_FUT", "RUT_FUT")
        US_CASH = ("SPX_CASH", "DJI_CASH", "DJI", "NASDAQ", "SPX", "RUSSELL2000")

        def _fresh_chgs(keys):
            out = []
            for s in keys:
                d = market_data.get(s) or {}
                if d.get("chg_pct") is None:
                    continue
                # A stale quote must not drive a decision input.
                if d.get("is_stale"):
                    continue
                out.append(d["chg_pct"])
            return out

        chgs = _fresh_chgs(US_FUT)
        basis = "futures"
        if not chgs:
            chgs = _fresh_chgs(US_CASH)
            basis = "cash (futures unavailable)"
        if not chgs:
            self.bus.set("global_risk_sentiment", None)
            return
        avg_chg = sum(chgs) / len(chgs)
        if avg_chg <= -0.75:
            sentiment = "risk_off"
        elif avg_chg >= 0.75:
            sentiment = "risk_on"
        else:
            sentiment = "neutral"
        self.bus.set("global_risk_sentiment", sentiment)
        self.bus.set("global_risk_sentiment_detail",
                     {"avg_chg_pct": round(avg_chg, 2), "n_indices": len(chgs),
                      "sentiment": sentiment, "basis": basis})

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

        # 2026-07-24: shared dedup added here — before this, NewsAgent
        # (its own RSS-based fetch) and this NewsAPI-based checkpoint
        # frequently logged the SAME underlying story twice (e.g.
        # "Sensex falls 700 points" appearing both as a NewsAgent risk
        # event and, independently, as a "Global Macro News" row here),
        # exactly the "picking similar information again and again"
        # duplication. news_engine.is_duplicate() is a shared,
        # process-wide check (both agents are threads in this same
        # process) — if either agent already logged this exact story
        # (normalized) within the last 6h, skip logging it again here.
        import news_engine as ne
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
                if ne.is_duplicate(title):
                    continue   # already surfaced by NewsAgent's RSS feed
                              # or an earlier NEWS_QUERIES category this
                              # same cycle — don't log it twice
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
                # Also feed the new unified, richer tracker (category +
                # impact-window classification) the Macro/News page's
                # per-item table reads — this checkpoint's items are
                # NewsAPI-sourced, "india" region by convention (matches
                # the existing macro-event log's own assumption).
                category = ne.classify_category(title)
                windows = ne.classify_impact_window(title, category)
                ne.log_tracked_event({
                    "source": "NewsAPI", "description": title, "link": "",
                    "category": category, "market_impact": bias,
                    "impact_windows": windows, "region": "india",
                    "valid": True, "action": action, "fetched_ts": time.time(),
                })

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
