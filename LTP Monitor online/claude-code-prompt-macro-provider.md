# Claude Code prompt — macro data provider refactor

> Fill in the three `<< >>` placeholders before pasting. Paste the whole thing as your first message in the repo.

---

## Task

Refactor the macro market-data layer in this project. The current implementation logs errors like the ones below and returns no data for most symbols during Indian market hours.

```
[news_macro] USDINR: no data from any provider — AV: <rate limit message>; TD: USDINR not in TWELVEDATA_SYMBOLS mapping
[news_macro] GOLD: no data from any provider — AV: <daily limit message>; TD: GOLD not in TWELVEDATA_SYMBOLS mapping
[news_macro] SPX: no data from any provider — TD: HTTPError: HTTP Error 404: Not Found; AV: symbol not mapped to a commodity/FX function
[news] feed fetch failed: MINING.COM — HTTP Error 403: Forbidden
```

**Before writing any code**, read the existing implementation and report back:

- `<< /Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor/news_macro.py >>`
- `<< /Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor ##path to the news feed fetcher >>`
- `<< config / .env >>`

Tell me what the current provider abstraction looks like, where `TWELVEDATA_SYMBOLS` is defined, how the module is scheduled/invoked, and how results are consumed downstream. Then propose a plan and wait for my approval before implementing. Do not start editing files in the first turn.

## Root causes to fix

1. **Alpha Vantage free tier is capped at 25 requests/day.** This is a hard daily ceiling, not a spacing problem — retry/backoff cannot fix it. AV must be demoted to a last-resort provider behind a cache, never the primary.
2. **Twelve Data symbol mapping is incomplete** for FX and metals. The correct symbols are `USD/INR`, `XAU/USD`, `XAG/USD`.
3. **Twelve Data index data is gated behind paid tiers**, which is why `SPX`/`DJI`/`NASDAQ`/`NIKKEI`/`RUSSELL2000` return 404. No symbol string fixes this on the free plan — these need a different provider.
4. **Cash indices are stale during Indian market hours.** `^GSPC`, `^DJI`, `^IXIC`, `^RUT` do not update between 09:15 and 15:30 IST; they return the previous US close. A macro monitor for an Indian intraday strategy must use index futures instead.
5. **MINING.COM returns 403** because no browser-like `User-Agent` is sent. Prefer their RSS feed over scraping the HTML page.

## Requirements

### Provider chain

Implement a `MacroDataProvider` interface with a per-symbol fallback chain, tried in this order:

1. **yfinance** — primary. No API key, no daily cap, covers every symbol below. Use `yf.download()` with a batched ticker list, `threads=True`, and `group_by="ticker"` — one call per poll cycle, not one call per symbol.
2. **Stooq** — secondary. Plain CSV over HTTP, no key, no registration.
3. **Twelve Data** — tertiary, only for symbols its free tier actually serves (FX and metals, not indices).
4. **Alpha Vantage** — last resort, behind a hard local request counter that refuses to call once the daily budget is spent.

Each provider returns a normalised quote object or raises a typed exception. The chain moves to the next provider only on failure, and logs which provider served each symbol at DEBUG level.

### Symbol map

Replace the current symbol set. Cash indices are retained only for post-close context; the futures are what the monitor should display during the session.

| Canonical | yfinance | Notes |
|---|---|---|
| SPX_FUT | `ES=F` | S&P e-mini, live during IST session |
| NDX_FUT | `NQ=F` | Nasdaq e-mini |
| DJI_FUT | `YM=F` | Dow e-mini |
| RUT_FUT | `RTY=F` | Russell e-mini |
| NIKKEI | `^N225` | live, Japan session |
| HSI | `^HSI` | live, Hong Kong session |
| GOLD | `GC=F` | COMEX front month |
| SILVER | `SI=F` | |
| CRUDE_WTI | `CL=F` | |
| CRUDE_BRENT | `BZ=F` | |
| USDINR | `USDINR=X` | |
| DXY | `DX-Y.NYB` | dollar index |
| NIFTY | `^NSEI` | |
| BANKNIFTY | `^NSEBANK` | |
| INDIAVIX | `^INDIAVIX` | |
| SPX_CASH | `^GSPC` | stale during IST session — flag it |
| DJI_CASH | `^DJI` | stale during IST session — flag it |

Keep the canonical names as the internal keys so downstream consumers are insulated from provider-specific tickers. Put the map in config, not hardcoded in the fetch logic.

### Staleness flagging

Every quote must carry `last_updated` and a computed `is_stale` boolean. A quote is stale if its timestamp is older than a per-symbol freshness threshold (short for futures and FX, longer for cash indices outside their session hours). The UI/log must show something like `SPX_CASH 6,145.20 (Fri close)` rather than presenting a stale number as live. Never silently serve a stale quote as current.

### Caching and rate limits

- TTL cache keyed on canonical symbol. Default TTL configurable; short during market hours, longer outside.
- Serve from cache on provider failure, marked stale, rather than returning nothing.
- Per-provider request budget tracked in a persistent counter that survives process restarts (a small JSON or SQLite file is fine) so the Alpha Vantage daily cap is respected across runs.
- Exponential backoff with jitter on transient failures (429, 5xx, timeouts). Do not retry on 4xx other than 429.

### Logging hygiene

- **Redact API keys from all log output.** The current code prints raw provider error bodies, which leaked a live key into the logs. Add a redaction filter that masks anything matching known key patterns and any value read from an env var whose name contains `KEY`, `TOKEN`, or `SECRET`.
- Truncate provider error messages to 120 characters before logging.
- One summary line per poll cycle at INFO (`n served, n cached, n failed`), per-symbol detail at DEBUG.

### News feed fix

Set a realistic `User-Agent` on all feed requests. For MINING.COM, use the RSS feed rather than the HTML page. Treat a 403 as a permanent per-source failure for the cycle — do not retry it in a tight loop.

## Non-goals — do not do these

- **Do not scrape tradingeconomics.com.** They have active anti-scraping measures, their market quotes are the paid part of their product, and it would violate their terms. If a TradingEconomics integration is ever wanted, it must go through their official API with a paid key.
- Do not add Selenium, Playwright, or any headless browser dependency.
- Do not add a paid data subscription without asking me first.
- Do not change the downstream consumers' interface — the refactor should be drop-in behind the existing call signature. If that is not possible, tell me why before proceeding.

## Acceptance criteria

- A single poll cycle fetches all symbols in the map with no unhandled exceptions when the network is available.
- With yfinance forcibly disabled (simulate by monkeypatching it to raise), the chain falls through to Stooq and still returns data for the majority of symbols.
- With all providers disabled, the monitor returns cached quotes marked stale, and logs one clear summary line — it does not crash and does not return empty.
- No API key appears anywhere in log output, including in exception tracebacks. Add a test that asserts this.
- Unit tests cover: symbol mapping, the fallback chain order, staleness computation across session boundaries, cache TTL expiry, and the daily-budget counter refusing a call when exhausted.
- Run the existing test suite and confirm nothing regressed.

## Notes

yfinance is unofficial and best-effort — Yahoo changes endpoints without notice, and cloud IPs get throttled harder than residential ones. Structure the code so swapping the primary provider is a config change, not a rewrite. Pin the yfinance version and note in the README that it may need periodic bumping.
