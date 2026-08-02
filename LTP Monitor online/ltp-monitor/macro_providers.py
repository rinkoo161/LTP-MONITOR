"""macro_providers.py — one quote, four providers, an explicit order.

2026-08-02. Replaces the hardcoded if/else chain in
`news_macro_agent._fetch_market_data()`. What that chain got wrong:

  - **Alpha Vantage was PRIMARY for FX/commodities.** Its free tier is 25
    requests PER DAY — a hard ceiling, not a spacing problem, so no
    backoff can fix it. Once spent, every FX symbol failed for the rest
    of the day.
  - **Twelve Data's map held only five index tickers**, so every FX and
    metal symbol hit `not in TWELVEDATA_SYMBOLS` and failed before a
    request was made. And TD's free tier does not serve indices at all,
    which is what produced the 404s — no symbol string fixes that.
  - **yfinance was last**, despite being the only provider that needs no
    key, has no daily cap, and covers every symbol.

Order is now: yfinance -> Stooq -> Twelve Data (FX/metals only) ->
Alpha Vantage (behind a persistent daily budget).

BATCHING. yfinance is called ONCE per checkpoint with the full ticker
list, not once per symbol. Per-symbol calls are what get cloud IPs
throttled.

STALENESS IS NOT OPTIONAL. Every quote carries `last_updated` and a
computed `is_stale`. A cash index does not update between 09:15 and
15:30 IST — it returns the previous US close — so presenting it as a
live number is a lie the old code told by omission. `is_stale` is
computed against a per-symbol freshness threshold, and a stale quote is
still SERVED (better than nothing) but never as current.
"""
import csv
import io
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import redaction
import store

ERR_LIMIT = 120          # spec: truncate provider errors before logging


# ------------------------------------------------------------- exceptions
class ProviderError(Exception):
    """Base. Every provider failure is one of these, never a bare None —
    the old code returned None for 'bad symbol', 'rate limited' and
    'network down' alike, which made the logs undiagnosable."""


class NotSupported(ProviderError):
    """This provider cannot serve this symbol. Permanent; skip it."""


class RateLimited(ProviderError):
    """429 or an in-body quota message. Transient within the day."""


class BudgetExhausted(ProviderError):
    """Our own counter refused the call before it was made."""


class TransientError(ProviderError):
    """5xx, timeout, connection reset — worth a retry."""


class BadResponse(ProviderError):
    """200 with unusable content. NOT retried: it will be unusable again."""


# ------------------------------------------------------------------ quote
class Quote:
    __slots__ = ("symbol", "value", "last_updated", "source", "ticker", "note")

    def __init__(self, symbol, value, last_updated=None, source="", ticker="",
                 note=""):
        self.symbol = symbol
        self.value = float(value)
        self.last_updated = float(last_updated or time.time())
        self.source = source
        self.ticker = ticker
        self.note = note

    def is_stale(self, cfg=None, now=None):
        return self.age() > freshness_threshold(self.symbol, cfg)

    def age(self, now=None):
        return max(0.0, (now or time.time()) - self.last_updated)

    def as_dict(self, cfg=None):
        return {"symbol": self.symbol, "value": self.value,
                "last_updated": self.last_updated, "source": self.source,
                "ticker": self.ticker, "note": self.note,
                "age_sec": round(self.age()),
                "is_stale": self.is_stale(cfg)}

    def __repr__(self):
        return f"<Quote {self.symbol} {self.value} via {self.source}>"


def symbol_map(cfg=None):
    cfg = cfg if cfg is not None else config.load()
    return cfg.get("macro_symbols") or config.DEFAULTS.get("macro_symbols") or {}


def freshness_threshold(symbol, cfg=None):
    """Seconds after which a quote for `symbol` counts as stale.

    Per-symbol rather than global: an e-mini future trading around the
    clock is stale after minutes, while a cash index outside its own
    session is EXPECTED to be hours old and flagging it every time would
    train people to ignore the flag.
    """
    m = (symbol_map(cfg).get(symbol) or {})
    try:
        return float(m.get("freshness_sec") or 3600)
    except (TypeError, ValueError):
        return 3600.0


# ------------------------------------------------------------------ budget
class Budget:
    """Persistent per-provider daily request counter.

    In a FILE, not memory: Alpha Vantage's cap is per calendar day and
    this process restarts often. An in-memory counter resets on every
    restart and the cap is blown by lunchtime — which is exactly what
    happened.
    """

    def __init__(self, path=None):
        self.path = path or store.path("provider_budget.json")

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, d):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(d, f)
        except OSError:
            pass          # a budget we cannot persist is handled by spent()

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d", time.localtime())

    def spent(self, provider):
        d = self._load().get(provider) or {}
        return int(d.get("count", 0)) if d.get("day") == self._today() else 0

    def remaining(self, provider, cap):
        return max(0, int(cap) - self.spent(provider))

    def consume(self, provider, cap, n=1):
        """Reserve `n` calls. Raises BudgetExhausted rather than returning
        False, so a caller cannot forget to check."""
        if self.remaining(provider, cap) < n:
            raise BudgetExhausted(
                f"{provider}: daily budget {cap} exhausted "
                f"({self.spent(provider)} used today)")
        d = self._load()
        cur = d.get(provider) or {}
        count = int(cur.get("count", 0)) if cur.get("day") == self._today() else 0
        d[provider] = {"day": self._today(), "count": count + n}
        self._save(d)


# -------------------------------------------------------------------- http
def _http(url, timeout=10, headers=None):
    h = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0 Safari/537.36")}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"HTTP 429 from {urllib.parse.urlsplit(url).netloc}")
        if 500 <= e.code < 600:
            raise TransientError(f"HTTP {e.code}")
        # Any other 4xx is permanent for this request — the spec is
        # explicit that only 429 is retried.
        raise BadResponse(f"HTTP {e.code}")
    except Exception as e:
        raise TransientError(f"{type(e).__name__}: {e}")


def with_backoff(fn, attempts=3, base=0.5, sleep=time.sleep, rand=random.random):
    """Retry ONLY transient failures, with jitter.

    NotSupported/BadResponse/BudgetExhausted are re-raised immediately:
    retrying a 404 or an exhausted quota just burns wall-clock inside a
    checkpoint that other symbols are waiting on.
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except (RateLimited, TransientError) as e:
            last = e
            if i == attempts - 1:
                break
            sleep(base * (2 ** i) * (0.5 + rand()))
    raise last


# --------------------------------------------------------------- providers
class MacroDataProvider:
    name = "base"
    needs_key = None            # config key holding the credential

    def available(self, cfg):
        return not self.needs_key or bool(cfg.get(self.needs_key))

    def supports(self, symbol, cfg=None):
        return bool((symbol_map(cfg).get(symbol) or {}).get(self.name))

    def fetch_many(self, symbols, cfg):
        """{symbol: Quote} for what it could serve. Partial is fine —
        the chain fills the rest. Must not raise for a single bad symbol."""
        raise NotImplementedError


class YFinanceProvider(MacroDataProvider):
    """Primary. One batched call per checkpoint."""
    name = "yf"

    def fetch_many(self, symbols, cfg):
        m = symbol_map(cfg)
        tickers = {}
        for s in symbols:
            t = (m.get(s) or {}).get("yf")
            if t:
                tickers[t] = s
        if not tickers:
            return {}
        try:
            import yfinance as yf
        except ImportError as e:
            raise NotSupported(f"yfinance not installed: {e}")
        out = {}
        # INTERVAL MATTERS FOR STALENESS, not just for price. A daily bar
        # is timestamped at the bar's START, so the freshest possible
        # `last_updated` from interval="1d" is already ~1-2 days old and
        # EVERY quote reads stale — the flag would fire constantly and
        # mean nothing. An hourly bar timestamps close to the last trade,
        # so `is_stale` then reports something real (e.g. genuinely stale
        # over a weekend, fresh mid-session). Still ONE batched call.
        interval = cfg.get("macro_yf_interval", "1h")
        period = cfg.get("macro_yf_period", "5d")
        data = yf.download(list(tickers), period=period, interval=interval,
                           threads=True, group_by="ticker", progress=False,
                           auto_adjust=False)
        for tk, sym in tickers.items():
            try:
                frame = data[tk] if len(tickers) > 1 else data
                closes = frame["Close"].dropna()
                if closes.empty:
                    continue
                ts = closes.index[-1]
                out[sym] = Quote(sym, float(closes.iloc[-1]),
                                 last_updated=ts.timestamp(),
                                 source=self.name, ticker=tk)
            except Exception:
                # One bad ticker must not lose the whole batch — that is
                # the entire point of batching.
                continue
        return out


class StooqProvider(MacroDataProvider):
    """Secondary. Plain CSV, no key, no registration."""
    name = "stooq"
    # 2026-08-02 — the documented quote endpoint `/q/l/` now 404s
    # ("Wybrana lokalizacja nie istnieje") and `/q/d/l/` answers 200 with
    # a JavaScript anti-bot challenge instead of CSV. Stooq therefore does
    # NOT currently satisfy the "plain CSV over HTTP" assumption it was
    # chosen for, at least from this host. The provider is kept — it may
    # serve normally from other IPs/regions, and the chain is designed so
    # a dead provider costs one skipped step — but it detects the
    # challenge and says so rather than failing as an opaque parse error.
    # Defeating the challenge would need a headless browser, which the
    # spec explicitly rules out.
    URL = "https://stooq.com/q/d/l/?s={s}&i=d"

    def fetch_many(self, symbols, cfg):
        m = symbol_map(cfg)
        out = {}
        for s in symbols:
            tk = (m.get(s) or {}).get("stooq")
            if not tk:
                continue
            try:
                body = with_backoff(lambda tk=tk: _http(self.URL.format(s=tk)))
                head = body.lstrip()[:40].lower()
                if head.startswith("(async") or head.startswith("<"):
                    raise BadResponse(
                        "anti-bot challenge instead of CSV — Stooq is not "
                        "serving machine-readable quotes from this host")
                row = next(iter(csv.DictReader(io.StringIO(body))), None)
                if not row:
                    continue
                close = row.get("Close")
                if not close or close in ("N/D", "-"):
                    continue
                out[s] = Quote(s, float(close), source=self.name, ticker=tk)
            except ProviderError:
                continue
            except Exception:
                continue
        return out


class TwelveDataProvider(MacroDataProvider):
    """Tertiary, FX and metals ONLY.

    Its free tier does not serve indices — that is what produced the 404s
    on SPX/DJI/NASDAQ. The map simply has no `td` entry for index
    symbols, so `supports()` is False and the chain skips it rather than
    making a request that cannot succeed.
    """
    name = "td"
    needs_key = "twelve_data_api_key"

    def fetch_many(self, symbols, cfg):
        key = cfg.get(self.needs_key) or ""
        if not key:
            raise NotSupported("no twelve_data_api_key configured")
        m = symbol_map(cfg)
        out = {}
        for s in symbols:
            tk = (m.get(s) or {}).get("td")
            if not tk:
                continue
            try:
                url = ("https://api.twelvedata.com/price?symbol="
                       + urllib.parse.quote(tk) + "&apikey="
                       + urllib.parse.quote(key))
                j = json.loads(with_backoff(lambda u=url: _http(u)))
                if j.get("status") == "error" or j.get("code"):
                    msg = str(j.get("message", j))
                    if "limit" in msg.lower() or j.get("code") == 429:
                        raise RateLimited(msg)
                    continue
                if j.get("price"):
                    out[s] = Quote(s, float(j["price"]), source=self.name,
                                   ticker=tk)
            except ProviderError:
                continue
            except Exception:
                continue
        return out


class AlphaVantageProvider(MacroDataProvider):
    """LAST RESORT, behind a hard persistent counter.

    25 requests per DAY on the free tier. It was previously primary for
    every FX and commodity symbol, which is why the logs are full of
    quota messages.
    """
    name = "av"
    needs_key = "alpha_vantage_api_key"

    def fetch_many(self, symbols, cfg):
        key = cfg.get(self.needs_key) or ""
        if not key:
            raise NotSupported("no alpha_vantage_api_key configured")
        cap = int(cfg.get("alpha_vantage_daily_budget", 20) or 0)
        budget = Budget()
        m = symbol_map(cfg)
        out = {}
        for s in symbols:
            spec = (m.get(s) or {}).get("av")
            if not spec:
                continue
            try:
                budget.consume(self.name, cap)     # raises when spent
            except BudgetExhausted:
                break                              # stop the whole loop
            try:
                if isinstance(spec, (list, tuple)) and len(spec) == 2:
                    url = ("https://www.alphavantage.co/query?"
                           "function=CURRENCY_EXCHANGE_RATE"
                           f"&from_currency={spec[0]}&to_currency={spec[1]}"
                           "&apikey=" + urllib.parse.quote(key))
                    j = json.loads(with_backoff(lambda u=url: _http(u)))
                    rate = (j.get("Realtime Currency Exchange Rate")
                            or {}).get("5. Exchange Rate")
                    val = float(rate) if rate else None
                else:
                    url = (f"https://www.alphavantage.co/query?function={spec}"
                           "&interval=daily&apikey=" + urllib.parse.quote(key))
                    j = json.loads(with_backoff(lambda u=url: _http(u)))
                    rows = j.get("data") or []
                    val = float(rows[0]["value"]) if rows else None
                for k in ("Note", "Information", "Error Message"):
                    if j.get(k):
                        raise RateLimited(str(j[k]))
                if val is not None:
                    out[s] = Quote(s, val, source=self.name, ticker=str(spec))
            except ProviderError:
                continue
            except Exception:
                continue
        return out


class FrankfurterProvider(MacroDataProvider):
    """ECB reference rates. Keyless, uncapped, genuinely independent.

    2026-08-02 — added as the Stooq replacement, with an honest limit:
    it serves FX ONLY. The ECB publishes currency reference rates, not
    index futures, metals or equity indices, so it restores a real
    secondary for USDINR and nothing else.

    That is worth having anyway. USDINR is the single most India-relevant
    number in the map, and it is the one symbol where the chain now has
    true upstream independence rather than two code paths to the same
    Yahoo endpoint.

    ECB publishes ONCE per working day (~16:00 CET). The quote is
    correspondingly coarse — `last_updated` is the publication DATE, so
    it will read stale against USDINR's 15-minute threshold outside the
    publication window. That is accurate, not a defect: this is a
    reference rate, not a live tick.
    """
    name = "ecb"
    URL = "https://api.frankfurter.dev/v1/latest?base={b}&symbols={q}"

    def supports(self, symbol, cfg=None):
        # The base implementation looks for a map entry named after the
        # provider. This one keys off "fx" instead, because the pair is a
        # property of the SYMBOL (USD->INR) rather than of this provider —
        # any FX source can use it. Without this override `supports()`
        # looked for "ecb", found nothing, and the provider was silently
        # never asked: the chain reported USDINR served by Twelve Data
        # while a keyless feed sat unused behind it.
        return len((symbol_map(cfg).get(symbol) or {}).get("fx") or ()) == 2

    def fetch_many(self, symbols, cfg):
        m = symbol_map(cfg)
        out = {}
        for s in symbols:
            pair = (m.get(s) or {}).get("fx")
            if not pair or len(pair) != 2:
                continue
            base, quote = pair
            try:
                body = with_backoff(
                    lambda b=base, q=quote: _http(self.URL.format(b=b, q=q)))
                j = json.loads(body)
                rate = (j.get("rates") or {}).get(quote)
                if rate is None:
                    continue
                ts = None
                if j.get("date"):
                    try:
                        ts = time.mktime(time.strptime(j["date"], "%Y-%m-%d"))
                    except ValueError:
                        ts = None
                out[s] = Quote(s, float(rate), last_updated=ts,
                               source=self.name, ticker=f"{base}/{quote}",
                               note="ECB daily reference rate")
            except ProviderError:
                continue
            except Exception:
                continue
        return out


# ORDER MATTERS. yfinance first (keyless, uncapped, full coverage), then
# the keyless FX-only ECB feed, then the two keyed providers with hard
# daily caps. Stooq is present but DISABLED by default — see
# `macro_providers_enabled` and the StooqProvider docstring: it stopped
# serving CSV and now answers with an anti-bot challenge, so leaving it
# enabled costs a wasted request (and up to 3 retries) per symbol per
# cycle for a provider that cannot succeed.
_ALL = {p.name: p for p in (YFinanceProvider(), FrankfurterProvider(),
                            StooqProvider(), TwelveDataProvider(),
                            AlphaVantageProvider())}
DEFAULT_ORDER = ["yf", "ecb", "td", "av"]


def build_chain(cfg=None):
    cfg = cfg if cfg is not None else config.load()
    order = cfg.get("macro_providers_enabled") or DEFAULT_ORDER
    return [_ALL[n] for n in order if n in _ALL]


CHAIN = build_chain(config.DEFAULTS)


# ------------------------------------------------------------------- cache
class QuoteCache:
    """TTL cache that SERVES ON FAILURE.

    An expired entry is not discarded — when every provider fails, the
    stale value is still the best available answer, and returning nothing
    makes the monitor look broken when it is merely offline. Expiry
    governs whether we re-fetch, not whether we may serve.
    """

    def __init__(self):
        self._d = {}

    def get(self, symbol, ttl):
        q = self._d.get(symbol)
        if q is None:
            return None, False
        return q, (time.time() - q.last_updated) <= ttl

    def put(self, q):
        self._d[q.symbol] = q

    def all(self):
        return dict(self._d)


def ttl_for(cfg=None, market_open=False):
    cfg = cfg if cfg is not None else config.load()
    return int(cfg.get("macro_cache_ttl_open" if market_open
                       else "macro_cache_ttl_closed", 600 if market_open else 3600))


# ------------------------------------------------------------------- chain
def fetch(symbols, cfg=None, cache=None, chain=None, market_open=False,
          log=None):
    """Run the chain. Returns (quotes, summary).

    `log(level, msg)` receives already-redacted, already-truncated text.
    One INFO summary per cycle, per-symbol detail at DEBUG — per spec.
    """
    cfg = cfg if cfg is not None else config.load()
    cache = cache if cache is not None else QuoteCache()
    chain = chain if chain is not None else build_chain(cfg)
    ttl = ttl_for(cfg, market_open)
    log = log or (lambda level, msg: None)

    quotes, served, from_cache = {}, {}, []
    pending = []
    for s in symbols:
        q, fresh = cache.get(s, ttl)
        if q is not None and fresh:
            quotes[s] = q
            from_cache.append(s)
        else:
            pending.append(s)

    for provider in chain:
        if not pending:
            break
        if not provider.available(cfg):
            log("debug", f"{provider.name}: unavailable (no key)")
            continue
        want = [s for s in pending if provider.supports(s, cfg)]
        if not want:
            continue
        try:
            got = provider.fetch_many(want, cfg) or {}
        except ProviderError as e:
            log("debug", redaction.redact(f"{provider.name}: {e}", cfg, ERR_LIMIT))
            continue
        except Exception as e:
            log("debug", redaction.redact(
                f"{provider.name}: {type(e).__name__}: {e}", cfg, ERR_LIMIT))
            continue
        for s, q in got.items():
            quotes[s] = q
            cache.put(q)
            served[s] = provider.name
            log("debug", f"{s}: served by {provider.name} ({q.value})")
        pending = [s for s in pending if s not in got]

    # Everything still missing falls back to a STALE cached value rather
    # than disappearing from the payload entirely.
    stale_served = []
    for s in list(pending):
        q, _ = cache.get(s, ttl)
        if q is not None:
            quotes[s] = q
            stale_served.append(s)
            pending.remove(s)

    # TWO KINDS OF STALE, deliberately not merged (2026-08-02).
    #
    #   from_stale_cache   every provider failed; we served old data
    #                      because it beats returning nothing
    #   stale_on_arrival   the fetch SUCCEEDED, but the quote is older
    #                      than its freshness threshold — normal on a
    #                      weekend or outside a symbol's session
    #
    # "we could not reach anyone" and "the market is shut" are different
    # states and need different responses, so one counter cannot serve
    # both. The summary previously counted only the first, which made it
    # report "0 stale" on a Sunday while all 17 quotes were stale — a log
    # line asserting the opposite of the data underneath it, which is the
    # exact failure mode this module was written to remove.
    stale_on_arrival = sorted(s for s, q in quotes.items()
                              if s not in stale_served and q.is_stale(cfg))
    summary = {"requested": len(symbols), "served": len(served),
               "cached": len(from_cache),
               "from_stale_cache": len(stale_served),
               "from_stale_cache_symbols": sorted(stale_served),
               "stale_on_arrival": len(stale_on_arrival),
               "stale_on_arrival_symbols": stale_on_arrival,
               # Legacy key, unchanged meaning, so any older reader keeps
               # working rather than silently switching definition.
               "stale": len(stale_served),
               "failed": len(pending), "failed_symbols": pending,
               "by_provider": served}
    log("info", f"macro: {len(served)} served, {len(from_cache)} cached, "
                f"{len(pending)} failed; {len(stale_on_arrival)} stale on "
                f"arrival, {len(stale_served)} from stale cache")
    return quotes, summary
