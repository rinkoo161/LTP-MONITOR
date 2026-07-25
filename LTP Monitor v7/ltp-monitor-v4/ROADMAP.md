# LTP Monitor — Roadmap

Living list of pending work. Update this file as items are picked up,
completed, or reprioritized — it's the source of truth across sessions,
not the chat history.

## Institutional-Grade AI Options Trading Dashboard (2026-07-25)

13-section spec to convert LTP Monitor into an institutional-grade
dashboard, extending existing modules only (no rebuild). Explicit rule
from the spec itself: **one feature at a time, stop for review after
each** — followed here. Progress tracked feature-by-feature below,
each section number matches the spec's own numbering.

### Feature #1 — LTP Monitor (Spot vs Futures) — DONE, awaiting review

Extended existing paths only, confirmed by audit before writing
anything:
  - Spot LTP/%/prev_close: ALREADY existed —
    `broker_adapter.py`'s `option_chain()` already computes this from
    Dhan's own `previous_close_price` field (`chain.chg`/`chg_pct`).
    Found `prev_close` itself was computed internally but never
    stored in the response — derived it in the new endpoint instead
    of touching that tested code path.
  - Spot O/H/L/VWAP: newly DERIVED from `spot_hist` (already
    accumulated by `MarketDataAgent` every REST cycle) — no new
    tracking added.
  - Futures LTP/O/H/L/VWAP: extended the EXISTING futures websocket
    tick pipeline (`MarketDataAgent._classify_future_tick`, built for
    OI-buildup classification) with a new `_update_future_ohlc()` —
    same ticks already flowing in, no new subscription.
  - VWAP is a TWAP proxy (mean of LTP across ticks), not a true
    volume-weighted average — same honest tradeoff already established
    elsewhere in this codebase (AnchorPullback's "session anchor") for
    the same reason: no clean per-trade volume delta available from
    the data source.
  - New `/api/ltp-monitor` endpoint + a Spot-vs-Futures panel at the
    top of the main Dashboard, with a simple participation read
    (Confirmed/Weak/Divergent based on whether futures direction and
    magnitude support spot) — deliberately simple; the full weighted
    AI Market Bias engine is Feature #2, not built here.

  **Bug found and fixed before it shipped**: the futures OI-buildup
  classifier returns early on each trading day's very first tick
  (before the OHLC update was originally placed) — would have made
  the recorded session "open" always be the SECOND tick of the day,
  not the true first one. Moved the OHLC update earlier in the
  function; verified with a direct test that "open" now correctly
  captures the true first tick.

  Tested: session-open correctness on the true first tick, high/low/
  close/vwap tracking across multiple ticks, new-day reset applies to
  OHLC same as the existing OI baseline, no regression on the
  existing OI-buildup classification, and the full `/api/ltp-monitor`
  endpoint end-to-end (prev_close derivation, spot OHLC from
  spot_hist, futures OHLC from the extended tracker) through the real
  FastAPI TestClient.

  **Not yet done, explicitly deferred to later features per the
  spec's own structure**: "Live Tick" real-time push to the frontend
  (currently polled every 5s, matching this app's existing polling
  convention — a websocket-push UI update would be a separate,
  smaller follow-up); Market Bias/Strength/Momentum/Institutional-
  Participation/Risk-Level labels beyond the simple participation
  read (that's Feature #2, the AI Market Bias engine).

### Feature #2 — AI Market Bias — DONE, tested

New `market_bias.py`. Reused, not duplicated: MACD/RSI from
`mtf_confluence_strategy.py`; regime/ADX from `RegimeAgent`'s existing
output; OI/PCR from `analyzer.py`; futures trend from
`future_oi_trend:{symbol}` (Feature #1 work); India VIX and global
risk sentiment from `NewsMacroAgent`'s existing bus keys; spot vs
futures % change from the same computation Feature #1's participation
read already used. Genuinely new: Supertrend and Ichimoku Cloud —
neither existed anywhere in this codebase before.

Weighted scoring (10 components, documented as a first-pass heuristic
to tune against real outcomes, same honesty standard as the
impact-window classifier in `news_engine.py`) → Strong Bullish /
Bullish / Neutral / Bearish / Strong Bearish + confidence %, with
missing inputs degrading gracefully (weight redistributed, not
faked) rather than hard-failing.

**Market Breadth explicitly NOT implemented** — no NIFTY50 constituent
advance/decline data source available (same honest-gap pattern as
FII/DII flows elsewhere in this project). Its weight is excluded from
the score entirely; reported in the `unavailable` list rather than
silently defaulted to neutral.

Extends `RegimeAgent` (already runs every 90s with fresh candles)
rather than adding a new agent — `_compute_bias()` reuses the exact
candle fetch already made for regime/ADX (stored to a new
`regime_candles:{symbol}` bus key, kept separate from `regime:{symbol}`
itself so the many existing consumers of that dict don't get a large
candle array attached to every read).

Tested: Supertrend against clean uptrend/downtrend/genuine-reversal
scenarios (with one indexing mistake in my own test caught and
corrected before trusting the result) and insufficient-data
graceful-degradation; Ichimoku against uptrend/downtrend/flat-market/
insufficient-data; the full weighted aggregator against 6 scenarios
including the spec's own worked example (spot +0.82%/future +0.91% →
Strong Bullish, matched exactly), the "spot rises but futures weak"
case correctly producing a weaker read than confirmed agreement, full
graceful degradation on zero inputs, and confirmed Market Breadth is
never scored. One real finding during testing: MACD histogram
converges to ~zero on a perfectly linear synthetic price series (a
genuine mathematical property of MACD, not a bug — verified directly
against the raw MACD/signal line values) — real market data never has
perfectly constant slope, so this is a synthetic-test-data artifact,
not a live-data concern; also a good demonstration of why the
weighted multi-indicator design matters (the other 8 components
correctly outweighed this one artifact in the full scenario test).
Wired end-to-end through the real `RegimeAgent._compute_bias()` method
and the actual `/api/ltp-monitor` endpoint, not just the isolated
module. New bias badge on each LTP Monitor card (confidence % plus a
hover tooltip showing the full component breakdown).



### Feature #3 — Support/Resistance + Entry Criteria — DONE, tested

Retained rather than rebuilt: `analyzer.py`'s `ranked_levels()` already
computed R1-R3/S1-S3 from OI+OI-change+volume, with strength % and
blue/yellow/pink coding, already used live by the spread strategies'
wall detection. For options trading specifically, OI walls are
arguably the most institutionally-relevant level type — kept as the
primary source, not superseded, per the explicit "retain if better"
instruction.

New `support_resistance.py`, genuinely new pieces only: Previous Day
High/Low/Close (didn't exist), and VWAP used as an actual level (VWAP
itself existed since Feature #1, wasn't used as an S/R reference
before). Merges these with the existing OI-wall levels into one
source-tagged, deduplicated (within 0.1%) R1-R3/S1-S3 — sorted by
proximity to spot, matching the spec's own framing of R1 as the
*first* (nearest) level, not necessarily the strongest. Also
implements the spec's own entry-criteria framework directly: bullish
requires spot above S1, S2 is the stop-loss zone, R1/R2/R3 is the
target ladder — bearish is the exact mirror.

**Honest gap, stated directly in the module docstring**: Volume
Profile and Price Acceptance from the spec are NOT implemented — both
need tick-level volume-at-price distribution over the session, which
this system doesn't retain (candle data is OHLC only). Same pattern as
Market Breadth in Feature #2 — reported as `unavailable`, not faked.

Extends `RegimeAgent` again (now computing regime + bias + levels in
one 90s cycle, reusing the same candle fetch throughout — zero extra
API calls added across all three).

Tested: previous-day extraction correctly picks yesterday specifically
(not today, not two days back) from a multi-day series; level merging
correctly ranks by proximity and tags each level's source; near-
duplicate levels (OI wall landing within 0.1% of a prev-day high)
correctly collapse to one entry instead of showing twice; entry
criteria tested across 6 scenarios — valid/invalid bullish, valid/
invalid bearish, the early-session no-levels-yet case, and an
unrecognized direction string. Wired end-to-end through the real
`RegimeAgent._compute_levels()` method, not just the isolated module.
New R/S display added to each LTP Monitor card.

### Database persistence — DONE, tested (extends Feature #3, applies going forward)

Per explicit request: Previous Day levels now persisted rather than
re-derived from a live candle re-fetch every cycle, and the storage
foundation for Volume Profile is in place. Extends `history.py`'s
existing SQLite DB (already used for backtesting/chain archival) —
two new tables, no parallel database:
  - `daily_ohlc(symbol, date, open, high, low, close, volume)` — one
    row per symbol per day, idempotently upserted every ~90s by
    `RegimeAgent._persist_daily_ohlc()` as the session progresses, so
    today automatically becomes tomorrow's persisted "previous day"
    with no separate end-of-day job needed.
  - `volume_profile(symbol, date, price_bucket, volume)` — price-
    bucketed volume accumulation. Storage only for now; the actual
    Volume Profile ANALYSIS (identifying high-volume nodes as S/R,
    the gap flagged in Feature #3) is not yet wired into
    `support_resistance.py` — but the data is now being captured
    rather than discarded, so that feature won't need a data-
    collection lead time later.

`support_resistance.previous_day_levels()` is now DB-first with
graceful fallback to the original live-candle derivation (handles
both "no data persisted yet" and "DB write failed" without losing
Previous Day levels entirely).

Tested: upsert/retrieve/idempotent-update/date-filtering for daily
OHLC (5 scenarios), volume-at-price bucketing and descending-volume
sort, the DB-first-with-fallback path in `support_resistance.py` (3
scenarios: DB has data / DB empty for this symbol / no symbol arg at
all for backward compatibility), and the full running-OHLC tracker
through the actual `RegimeAgent` method across multiple calls plus a
day-boundary reset.

**Principle to carry forward, per explicit instruction**: future
features needing time-series or distributional data (not just current-
state bus values) should persist to this same DB rather than holding
everything in memory only — noted here as a standing convention, not
a one-off for this feature.

### TradingView Lightweight Charts — DONE, tested (Feature #12, reframed from webhooks)

Built exactly the requested architecture: DhanHQ WebSocket (existing
hybrid feed) → Market Data Service (existing `MarketDataAgent`) →
Candle Builder (new) → FastAPI WebSocket Server (new) → TradingView
Lightweight Charts (new, client-side).

- **Candle Builder** (`MarketDataAgent._build_candle`): hooks into the
  EXISTING index-tick handler (`_on_ws_tick`) — no new subscription.
  Aggregates ticks into 1-minute candles in memory, publishes the
  currently-forming candle to `live_candle:{symbol}` on every tick,
  and persists each COMPLETED minute to `history.py`'s existing
  `candles` table (security_id convention `"{symbol}_SPOT_1m"` —
  reusing the schema built for option-leg candles, not a parallel
  table).
- **FastAPI WebSocket Server** (`/ws/candles/{symbol}`): sends
  today's historical 1m candles from the DB on connect (so the chart
  isn't empty on load), then streams the live-forming candle every
  ~1s. One message shape handles both "still the current bar" and "a
  new bar started" — Lightweight Charts' own `update()` call
  distinguishes them by timestamp, no separate event type needed on
  the wire.
- **Frontend**: TradingView Lightweight Charts loaded from CDN
  (`unpkg.com/lightweight-charts`, following the same CDN convention
  already used for Chart.js), a new panel at the top of the dashboard
  with a symbol selector, auto-reconnect on disconnect (3s backoff).

Retained, not replaced: the existing custom canvas-based Price Chart
panel stays as-is — this is a new, additional panel, not a rip-and-
replace, since the existing one may still serve other purposes and
this needed its own validation first.

Tested: tick aggregation within a single minute (open/high/low/close
correctly track across 3 ticks), minute-rollover persistence (the
completed candle correctly lands in the DB with the right OHLC and
timestamp, a fresh candle starts immediately), and the full WebSocket
endpoint end-to-end through FastAPI's real websocket test client —
confirmed it sends historical candles first, then the live-forming
candle, with real data seeded into the actual DB and bus.

**Honest limitation**: the CDN script load has not been verified from
a live browser (this sandbox can't run one) — if `unpkg.com` is
blocked or the library version pinned here ever changes its API
shape, the chart will show "Lightweight Charts failed to load" (a
handled, visible failure state, not a silent blank panel) rather than
crash the page. Worth a quick visual check on your end before relying
on it.

**Bug found live 2026-07-25, fixed**: chart rendered (cursor/crosshair
working, confirming the library and WebSocket connection itself were
fine) but no candles showed. Root cause: the candle builder is brand
new, so its own DB accumulation (`candles` table, `{symbol}_SPOT_1m`)
was genuinely empty — reported at 21:39 IST, outside market hours, so
no new live ticks were arriving either. Fixed with a fallback: when
the DB has nothing for today, the WebSocket endpoint now seeds history
from `regime_candles:{symbol}` — data Feature #2/#3 already fetch
every 90s during market hours, no new API call. These are 5-minute
bars, not 1-minute, so the chart shows coarser candles until the
builder's own 1m data accumulates and takes over — a real, disclosed
tradeoff, not hidden. The status line now also shows which source is
active, and a genuinely-no-data case (e.g. a symbol regime hasn't run
yet, or a weekend) shows a clear message instead of a silently empty
chart. Tested both the fallback-triggers case and the truly-empty
case through the real WebSocket endpoint.

**Second round of live feedback, both fixed**:

1. Candles still not showing even with the `regime_candles` fallback
   — turned out the existing Price Chart panel reliably has data
   because `/api/candles/{symbol}` makes a LIVE REST call
   (`d.intraday()`) independent of any bus/DB state, while the
   WebSocket's first two fallback tiers both depend on background
   agents having run recently. Added that same live REST call as a
   third tier — used only when both the candle-builder DB and
   `regime_candles` come back empty. Tested directly: mocked the Dhan
   client, confirmed the REST tier correctly kicks in and is
   correctly labeled `rest_live_fallback` in the response.
2. **Timezone bug**: the chart displayed 21:48 when the actual time
   was 03:21 AM IST — 21:48 UTC the previous day, confirming Lightweight
   Charts was rendering raw UTC with no IST conversion. This dashboard
   is IST-based throughout; the library has no built-in fixed-timezone
   option in this version. Fixed with the standard workaround: shift
   every timestamp by +19800s (5:30) before handing it to the chart, so
   its UTC-display shows the correct IST digits. Verified the exact
   math directly (a live epoch shifted by 19800s landed on 03:23,
   matching the reported 03:21 AM almost exactly).

**Third round of live feedback**: still no candles, and only NIFTY's
chart updates at all, not the other 3 symbols. Investigated the
websocket index subscription (`add_index_instrument` correctly maps
each symbol's own security_id in a dict, no collision across symbols)
and found a genuine bug in my OWN diagnostic code instead: the REST
fallback's exception was being silently swallowed (`except Exception:
pass`) — directly violating this project's own "fail loud, not
silent" principle. Fixed: every tier's outcome (DB count, regime_
candles count, REST error or stale-data detail) is now captured and
sent back in the WebSocket message's `diagnostics` field and logged to
the activity log, with the 3 failure modes (hard REST error, REST
returned data but none from today, no Dhan client at all) each
distinguishable. Also added a 30s no-live-tick watchdog per symbol —
if `live_candle:{symbol}` is never set after connecting, that's now
visible instead of the panel just sitting silently blank forever.
Tested all 3 new diagnostic paths directly.

**Honest status**: this doesn't yet explain WHY only NIFTY works — I
could not conclusively determine that from code inspection alone
(several plausible causes: REST failing specifically for the other 3
symbols, RegimeAgent not having cycled for them yet, or something
else). What this DOES do is turn the next test attempt into actionable
data instead of another guess — the diagnostics field will show
exactly which tier is failing and why, per symbol, the next time this
is tried.

**Root cause found from the diagnostic data itself** (this is exactly
why the diagnostics were built first): every symbol's log showed "350
candles returned but none are from today". At 3:35 AM with markets
closed, REST correctly returns the last trading session's real
candles, but the fallback filter strictly required calendar-date
"today" — which can never match outside market hours, so 350 genuine
candles were being thrown away every time. Not a per-symbol issue at
all (the "only NIFTY" theory from the previous round doesn't hold up
against this data — all 4 symbols show the identical pattern here).

Fixed: added `most_recent_session()` — prefers genuine today's candles
when the market IS open (preserving the correct live-session behavior
during trading hours), and falls back to the most recent PRIOR
trading day's full candle set when nothing from today exists yet, the
same way a real chart is expected to behave outside market hours
rather than sitting blank. Also caught and fixed a second bug while
implementing this: a bare `datetime.fromtimestamp()` call with no
import in scope (this file only imports `datetime` locally inside a
different function) — would have been a live `NameError` the moment
this code path actually ran, caught before shipping by running the
tests, not after.

Tested: the exact reported scenario reproduced directly (350 candles,
all from ~20h ago, none from calendar-today) now correctly returns all
350; confirmed no regression when genuine today's candles ARE present
(still correctly prioritized); confirmed candles spanning multiple
prior days correctly resolve to the MOST RECENT day specifically, not
an arbitrary older one.

Also clarified from this same log: the "no live tick received" warnings
appeared identically for NIFTY, BANKNIFTY, FINNIFTY, and SENSEX — fully
expected with markets closed (no symbol receives live ticks with
nothing trading), not evidence of a NIFTY-specific subscription
problem as first suspected.


### Log review round 2 (2026-07-25, continued) — TLS environment issue found, coverage-check diagnostic bug fixed, 3-month futures added

- [!] **Root cause of "still no candles" and ">3s refresh" — NOT a code
  bug, an environment issue on the user's own machine.** The new
  activity.log shows, for ALL FOUR symbols simultaneously: `Could not
  find a suitable TLS CA certificate bundle, invalid path:
  /Users/user/Documents/Stock Tools/LTP Monitor v7/ltp-monitor-v2/
  venv/lib/python3.14/site-packages/certifi/cacert.pem`. This means
  every REST call to Dhan (option chain AND candle fetches, which both
  go through the same requests/certifi HTTPS layer) was failing
  wholesale — which fully explains both symptoms: no candles (Regime-
  Agent's REST candle fetch, the thing that persists to the DB, never
  succeeds) and slow refresh (MarketDataAgent's ~3s cycle spends its
  time retrying failed HTTPS connections instead of completing).
  The path itself is the smoking gun: it points at an OLD version
  folder ("LTP Monitor v7/ltp-monitor-v2/venv") — almost certainly a
  stale `SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` environment variable
  left over from a previous venv that's since been deleted or moved
  when this version was set up. Not something I can fix from here (no
  access to the user's machine/shell) — flagged directly rather than
  chasing it as a phantom code bug. Practical fix on their end: check
  for and clear/update that env var, or reinstall certifi in the
  CURRENT venv (`pip install --upgrade --force-reinstall certifi`).
- [x] **My own coverage-check diagnostic had a real bug, found from
  its own output.** The instrumentation added last round reported
  "NEVER received for SENSEX" — but the reported security_id (1144507)
  was SENSEX's FUTURE, not its index, and that future had been
  subscribed only 3 seconds before the 30s check fired (futures
  subscribe asynchronously, after the index-only initial connection).
  It never had time to receive a tick — not a fair test. Worse, the
  SAME log data shows all 4 INDEX security_ids (13/25/27/51) DID each
  receive a tick within the first fraction of a second after connect —
  meaning the original "only NIFTY gets index ticks" theory from the
  previous round is not actually confirmed by this data. Fixed: the
  coverage check now snapshots exactly which security_ids were part of
  the INITIAL connection (before any subscribe_more() calls can add
  futures/options) and checks coverage against that fixed snapshot
  only, not the live-mutating dict. Verified directly: reproduced the
  exact failure (index ticks all received, then a future subscribed
  and ticked seconds before a 30s mark) and confirmed the fixed logic
  no longer flags it as missing.
- [x] **Futures now track 3 months, not just the front one — per
  explicit request ("there are 2 more months - capture those as
  well").** `dhan_scrip_master.get_current_futures_detailed(symbol,
  n=3)` — same row-scanning logic as the single-contract version,
  returns up to 3 nearest-unexpired contracts sorted by expiry.
  `get_current_future_detailed()` (the original, singular function) is
  now a thin wrapper calling this with n=1 — confirmed byte-for-byte
  behavior-identical for existing callers via the full regression
  suite. `MarketDataAgent._ensure_futures_subscribed()` now subscribes
  all 3; the FRONT month keeps its EXACT existing role (still the only
  one driving `future_oi_trend:{sym}`/`future_ohlc:{sym}` — the live
  OI-buildup strategy signal and LTP Monitor panel are unaffected).
  The 2nd/3rd months are additive: a new lightweight `_future_month_
  tick()` tracks their LTP/OI (no buildup classification — that's
  specified against the front month only) into a new
  `future_months:{sym}` bus key, keyed by role (`month2`/`month3`).
  **Honest scope**: this captures the data (same "capture first,
  analyze later" pattern as the candle-DB work) — an actual multi-
  month OI/volume-wall UI or analysis on top of it is not built here,
  since that's squarely Option Chain Engine territory (Feature #4),
  and building it blind without your spec risks going a different
  direction than what you actually want there.
  Tested against your real SENSEX/NIFTY sample rows extended to 3
  expiries each: correctly returns all 3 sorted nearest-first, with
  exact security_ids/expiries/lot_size/tick_size; confirmed the
  backward-compat single-contract path still returns exactly the
  front-month contract via the full existing test suite (all pass).

### Live log review (2026-07-25, continued) — 3 real fixes, 1 instrumented-not-guessed

Confirmed from real screenshots + a real activity.log the person shared
(not guessed): the SENSEX-futures and DB-persistence fixes above ARE
working live — the log shows `SENSEX future subscribed: BSXFUT
(security_id=1144507, expiry=2026-07-30)` (exact match to the fix) and
the chart's own diagnostics showed `db_candles_found: 54,
source: "candle_builder_1m"` for SENSEX — real historical data loading
from the DB now, not empty.

- [x] **Top index tabs and the Lightweight Chart panel were two
  independent "current symbol" selections.** Root cause of "websocket
  change with index selection... should work for all parallelly":
  `switchSym()` (the top NIFTY/BANKNIFTY/FINNIFTY/SENSEX tabs) never
  touched the chart, which only moved via its own separate dropdown —
  clicking a top tab left the chart showing whatever it was already
  on. Fixed both directions: `switchSym()` now also updates the
  chart's dropdown and reconnects it; the chart's own dropdown
  (`switchLwChart()`) now also updates `current` and refreshes every
  other panel. Both start on NIFTY by default so they're in sync from
  load.
- [x] **The "no live ticks" watchdog message was stomping a good
  status.** Live report: SENSEX's chart successfully showed real
  history (`db_candles_found: 54`), but 30s later a diagnostic message
  overwrote that reassuring status with "no live ticks received... may
  be closed, or feed isn't connected" — confusing given data was
  already on screen. Fixed: the watchdog note only replaces the status
  line if no real history has loaded yet; otherwise it goes into the
  tooltip only. Also made the message itself context-aware
  (`agents.market_open()`) instead of a vague "may be closed, or..."
  hedge — it now says plainly whether the market is open (symbol-
  specific subscription gap) or closed (expected, showing last
  session).
- [x] **Instrumented (not guessed) the actual "only NIFTY gets live
  ticks" question.** Log evidence: `⚠ chart websocket: no live tick
  ever received for {BANKNIFTY,SENSEX,FINNIFTY}` fired repeatedly but
  NEVER for NIFTY, despite `ws: connected — 4 instrument(s)
  subscribed` — a real, symbol-specific gap, not explained by "market
  closed" alone (some of these fired during live trading hours).
  Downloaded and read the REAL installed `dhanhq==2.2.0` source
  directly (`validate_and_process_tuples`/`subscribe_instruments` in
  `marketfeed.py`) rather than guessing: its batching correctly keeps
  distinct security_ids as separate subscription entries (no
  accidental dedup across different index IDs), so a client-side
  batching bug looks unlikely from the library code alone — but that
  can't be confirmed without seeing which security_ids actually arrive
  over the wire. Rather than guess a third time, added real
  instrumentation to `dhan_ws.py`: the exact resolved subscription
  list is now logged at connect time; every tick's security_id is
  tracked in a `_seen_sec_ids` set; an unmapped/unexpected security_id
  arriving is logged once (would indicate a client-side routing bug —
  tick arrives but doesn't match our expected key); and a one-shot
  30s-after-connect coverage check logs exactly which symbols got at
  least one tick and which never did. This will give a conclusive
  answer next live session: Dhan's server never sending data for those
  IDs (server/account-side) vs. the client receiving-but-misrouting
  them (would show as unmapped-security_id log lines instead).
  Unit-tested the tracking mechanism in isolation (mocked dhanhq,
  simulated ticks for only one of four subscribed symbols plus one
  unmapped id) — confirms `_seen_sec_ids`/missing-symbol reporting and
  the unmapped-id log line both work correctly. NOT yet confirmed
  against a real live capture of the coverage-check output itself —
  that needs the next live session.

### SENSEX futures fixed (switched to detailed scrip master CSV) + chart flat-line bug fixed (2026-07-25, continued)

Live report: candles still not showing (should show last market day's
data, never a blank chart with only a live flat line), and SENSEX
futures still not loading. Two separate real fixes:

- [x] **SENSEX futures — root cause was the wrong CSV file entirely.**
  `dhan_scrip_master.py` was pointed at the COMPACT scrip master
  (`api-scrip-master.csv`), which has no `UNDERLYING_SYMBOL` column at
  all and an unconfirmed BSE exchange code — SENSEX futures could
  never reliably resolve from it. Switched to the DETAILED file
  (`api-scrip-master-detailed.csv`) per explicit request, with the
  real column schema and sample rows provided directly (EXCH_ID,
  SEGMENT, SECURITY_ID, INSTRUMENT, UNDERLYING_SECURITY_ID,
  UNDERLYING_SYMBOL, SYMBOL_NAME, DISPLAY_NAME, INSTRUMENT_TYPE,
  LOT_SIZE, SM_EXPIRY_DATE ["DD/MM/YY", no time component — different
  from the compact file's "DD/MM/YY HH:MM"], STRIKE_PRICE,
  OPTION_TYPE, TICK_SIZE). Confirmed directly from the real sample:
  SENSEX FUTIDX rows use EXCH_ID="BSE" plainly (no BSE_FNO/BFO
  guessing needed), and UNDERLYING_SYMBOL is populated cleanly for
  every symbol (no more trading-symbol-prefix-parsing fallback
  needed as the primary path). `tick_size` now also returned
  alongside `lot_size` — both needed for margin calculations per
  explicit request. COLUMN_CANDIDATES keeps the old SEM_* names as
  fallbacks for backward compatibility. Cache file renamed
  (`scrip_master_detailed.csv`) so a stale compact-schema cache can't
  silently linger.
  Tested (`test_dhan_scrip_master.py`, rewritten for the new schema):
  nearest-unexpired-contract selection, OPTIDX-row exclusion,
  **SENSEX specifically** — resolves at all (previously always
  None), picks the nearest of 3 real BSE expiries, exchange reads as
  plain "BSE", lot_size/tick_size both correct — legacy compact-CSV
  schema still parses via the fallback columns (regression check).
  All pass against real confirmed sample rows. Live-network tier
  still blocked in this sandbox (`images.dhan.co` returns 403) — not
  yet confirmed against the real 25MB+ file end-to-end.

- [x] **Chart flat-line bug — root cause was live ticks rendering
  before any real history loaded.** The reported symptom (empty-
  looking chart with just a thin flat line near the current price,
  weird auto-generated axis labels) matches exactly what happens when
  `lwSeries.update()` is called repeatedly for live-only single-tick
  "candles" while `setData()` was never given real historical data —
  Lightweight Charts auto-fits its axis to whatever sparse data exists,
  producing that degenerate look. Fixed in `dashboard.html`: a new
  `lwHistoryLoaded` flag is set only when the `history` message
  actually contains candles; `live` messages are now silently ignored
  until it's true. This can't fully fix a genuine no-data-anywhere
  case, but it guarantees the chart either shows a real candle history
  (today's, or the DB-backed "most recent session" persisted by
  RegimeAgent as of the fix above) or a clear "no candles found —
  <diagnostic>" message — never a misleading flat sliver. The
  `db_1m_most_recent_session`/`db_5m_most_recent_session` tiers added
  above are exactly what should now make "last market day" actually
  available immediately rather than only accumulating from the moment
  this code first runs.
  **Not independently confirmed against the exact reported screenshot**
  — this sandbox has no live Dhan connection or market-hours access,
  so this is a logical fix for the mechanism that best explains the
  reported symptom, not a live-reproduced-then-fixed bug. Worth a
  direct re-check with the chart's status-line tooltip (now shows the
  full `diagnostics` JSON) if it recurs — that will show exactly which
  of the 5 tiers is failing and why.

### Candle DB persistence extended to all 4 symbols, all timeframes (2026-07-25)

Per explicit request ("store the candles in local db for further use
and analysis, now onwards") + a live report that chart candles were
missing for all indexes. Root cause of both, same gap: the DB-backed
1m candle table (`{symbol}_SPOT_1m`) was ONLY ever populated by the
live websocket tick builder (`MarketDataAgent._build_candle`), which
requires `market_data_feed: "websocket"` (default is `"rest"`) — so
under the default config, that table stayed empty regardless of
symbol, and the chart fell all the way back to a live REST call every
time (slow, network-dependent, and only ever showed whichever symbol
you'd just selected — nothing persisted for the other three).

RegimeAgent already fetches 1m/5m/15m candles for ALL FOUR symbols
every ~90s via REST (needed for regime/bias/levels) — reused that
exact fetch, no new API calls:
  - `history.upsert_index_candles(symbol, candles, timeframe)` — new,
    writes into the SAME `candles` table/security_id convention the
    websocket builder already uses (`{symbol}_SPOT_{tf}m`), so both
    mechanisms coexist safely (idempotent REPLACE on security_id+ts).
  - `RegimeAgent._persist_candles()` calls this for c1/c5/c15 right
    after they're fetched, before the warmup-length early-return, so
    even a symbol still warming up gets captured.
  - `history.most_recent_session_candles(security_id)` — new, returns
    the most recent IST calendar day that has ANY persisted data
    (today's if today has data, else the last trading day's) —
    network-independent, DB-only.
  - `/ws/candles/{symbol}` (app.py) gained two new tiers ahead of the
    existing bus/REST-live ones: DB 1m most-recent-session, then DB 5m
    most-recent-session — tried before falling back to in-memory bus
    state or a live REST call. Frontend status line updated with
    labels for both new sources.

Tested directly (synthetic data, isolated DB): insert + retrieve
round-trip for `upsert_index_candles`/`most_recent_session_candles`,
correct grouping to the most recent date with data, empty-security-id
case returns `[]` not an error, and idempotent re-upsert doesn't
duplicate rows. NOT tested against a live Dhan feed or market hours —
today (2026-07-25) is a Saturday, market closed, and this sandbox has
no network path to api.dhan.co — RegimeAgent itself won't run
(`market_open()` gates its whole cycle) until the next live session,
which is also when this persistence will actually start accumulating
real data. Flagging directly: this should close the "candles missing
for all indexes" report, but hasn't been confirmed against a real
live/market-hours run yet — worth a check once markets are open.

### Features #4–11, #13 — not started

#1 (LTP Monitor), #2 (AI Market Bias), #3 (Support/Resistance + Entry
Criteria) are done and tested above. Remaining: Option Chain Engine
enhancements (#4), Institutional Activity detection (#5), Strike
Ranking (#6 — note: substantially overlaps with #3's OI-wall work
already retained from analyzer.py; likely a lighter lift than
originally scoped), Technical Engine (#7), AI Signal Engine (#8), Risk
Engine (#9), Options Strategy Engine (#10), AI Narrative (#11).
TradingView overlays (#12) is DONE above (Lightweight Charts). #13
(implementation rules — modularity, async, docs, tests) is an ongoing
discipline applied throughout, not a discrete deliverable. Each
remaining feature is independently substantial — several (Signal
Engine, Risk Engine, Options Strategy Engine) are comparable in scope
to the MTF Confluence strategy build earlier this project, which took
a full session on its own with the same testing rigor. Working through
these with the same testing discipline used for #1/#2/#3/#12, not
batched untested.



## rinkoo.docx (2026-07-23) — status of every deliverable requested

Reference docs saved to `docs/strategy-reference/` (rinkoo.docx,
future_and_options.pdf, triple_screen_setup.pdf,
check_list_for_3rd_wave_setup_.pdf) for this and future sessions.

### Done and tested

- [x] **Scrip master schema fix** — real confirmed compact-CSV schema
  wired into `dhan_scrip_master.py` (`SEM_SMST_SECURITY_ID` etc.,
  `DD/MM/YY HH:MM` expiry format, `SM_SYMBOL_NAME` as the underlying-
  symbol filter since this file has no dedicated `UNDERLYING_SYMBOL`
  column). Tested against the user's exact FINNIFTY row (security_id
  61091). Still not run against the real 25MB live file — this
  sandbox's egress allowlist blocks images.dhan.co directly (confirmed
  403) — `test_dhan_scrip_master.py` is ready for that run.
- [x] **ATR-based position sizing** (`sizing.size_by_atr_risk`) —
  matches the docx's worked example exactly: capital ₹10,00,000, ATR
  228.47 → SL buffer 342.7pts → risk/lot ₹8,567.50; 1% risk → 1 lot
  (0.86% actual), 2% risk → 2 lots (1.71% actual). Includes the
  options-delta scaling note (ATM option premium moves ~half the
  index-point distance).
- [x] **"MACD+Stoch Confluence" strategy — the first new strategy
  requested, built and wired to execute.** `mtf_confluence_strategy.py`
  implements the WRITTEN rule set from the docx (daily MACD above-zero
  uptick + weekly MACD turning up after being down + RSI(14)>40 +
  Stochastic bullish cross from oversold + price in upper Bollinger
  Band — all 5 mandatory; bearish is the exact mirror; futures OI
  buildup is supportive-only, boosts confidence but never blocks).
  `MTFConfluenceAgent` runs it every 15 min during market hours and
  fires real BUY_CE/BUY_PE signals into the standard risk pipeline —
  same capital gates, position caps, daily loss limit as every other
  strategy, no special exemption. Sized via `size_by_atr_risk` with
  delta=0.5 (options, not futures). Visible on the Strategies page
  (new MTF Confluence panel) and configurable in Settings (enable
  toggle, min confidence, max trades/day).
  Indicator math (MACD/RSI/Stochastic/Bollinger %B/ATR/weekly
  resampling) independently verified against known reference behavior
  — monotonic series, crossover detection, %B at band extremes — not
  just trusted from the file's own comments. Found and fixed a real
  bug during this verification: an unclamped ATR-scaled stop distance
  could exceed the entire option premium when ATR is large relative to
  that day's IV (produced a ₹0.05 stoploss on a ₹100 option in
  testing) — added a 10-60%-of-entry sanity clamp and re-verified
  against the exact failing case plus two edge cases (realistic ATR,
  zero ATR).
  **Scope note**: this implements the WRITTEN rule set from rinkoo.docx,
  not the more elaborate second Pine Script in that doc (pivot-based
  MACD/RSI/Stochastic divergence detection, Fibonacci-extension
  targets, a full weekly/daily/1H confluence table) — that stays a
  distinct, larger follow-up, listed below, not silently folded in.
  Regression: all touched files pass syntax + import checks, JS
  validates, settings round-trip via the actual API, all agents
  (14 total incl. this one) instantiate and cycle cleanly with no
  live broker connection (graceful degradation confirmed).

### Explicitly deferred (not built, tracked here rather than dropped)

- [ ] **Full "1H MTF Reversal Strategy" port** — the second Pine
  Script in rinkoo.docx: pivot-high/low based divergence detection
  across MACD/RSI/Stochastic, EMA5/13/26 1H cross with a weekly+daily
  context gate, Fibonacci-extension or nearest-support/resistance
  targets, and a live on-chart confluence table. Substantially larger
  than the first strategy — recommend as its own dedicated session
  once the first strategy has a day or two of live results to compare
  against.
- [x] **Futures OI as a live supportive signal — DONE (2026-07-24).**
  `MarketDataAgent` now subscribes the current-month future per symbol
  (via `dhan_scrip_master.get_current_future_detailed()` + the
  existing `DhanWebsocketClient.subscribe_more()`, re-checked once per
  trading day — this is what makes monthly rollover automatic: a new
  day's lookup naturally resolves to whichever contract is now
  nearest-unexpired, and if that's a different security_id than
  yesterday's, the new one gets subscribed with no manual update).
  Futures ticks are classified into long/short buildup by comparing
  LTP+OI against a same-day baseline (captured at the first tick of
  each trading day — buildup is a session-level read, not tick-to-tick
  noise) and written to `future_oi_trend:{symbol}` as exactly `"long"`
  / `"short"` / `None` — matching `mtf_confluence_strategy.evaluate()`'s
  `future_buildup` parameter exactly (verified by reading its literal
  string comparisons before writing this, not assumed — an earlier
  draft would have written `"long_buildup"`/`"short_buildup"` here,
  which would have silently never matched). Only the strict textbook
  buildup quadrants (price+OI both up = long, price down + OI up =
  short) feed the strategy signal; the other two quadrants (short
  covering, long unwinding) are real signals but a weaker/different
  read than what rinkoo.docx specifically asked for — reported on a
  separate `future_oi_quadrant:{symbol}` diagnostic key instead, so
  the strategy only ever sees the exact signal it was specified
  against.
  Tested: all 4 OI/price quadrants classify correctly, first-tick and
  new-day baseline reset, daily dedup (doesn't re-subscribe same day),
  monthly rollover (different resolved security_id on a later day
  triggers a fresh subscribe), connection-not-ready retries next cycle
  rather than giving up for the day, scrip-master lookup failure
  logged and handled without crashing, futures ticks confirmed routed
  away from the option-chain merge (a future's data doesn't belong in
  `chain:{symbol}`'s strike/ce/pe rows), and a full end-to-end test
  confirming the live bus value actually reaches
  `mtf_confluence_strategy.evaluate()`'s confidence calculation.
  Scope respected: no changes to ExecutionAgent's place/exit/
  exit_spread, RiskAgent.evaluate, or sizing.py — confirmed by
  inspecting MarketDataAgent's method list after the change (only new
  methods: `_ensure_futures_subscribed`, `_classify_future_tick`) and
  confirming `sizing.size_by_atr_risk` is unchanged.
  Still requires `market_data_feed: "websocket"` to be enabled
  (defaults to `"rest"`) and Dhan as the active broker — same
  precondition as the rest of the hybrid feed.
- [ ] **News as an active entry/exit input** — currently news only
  gates (blocks a conflicting-direction signal, or scores as a
  same-direction opportunity via `news_risk_opportunity()`). The docx
  asks for something more attentive: news severity actively feeding
  entry/exit decisions, not just a block/pass gate. Needs a concrete
  design — e.g., a severity-weighted score merged into a strategy's
  confidence the same way futures-OI buildup already is for MTF
  Confluence — before implementation, since "more attentive" is a
  design decision (how much should a severe news event move a
  decision?) as much as a coding task.
- [x] **DJI/Nasdaq/Crude/Gold/Silver/macro-event summary — DONE
  (2026-07-24).** `/api/macro/digest` endpoint + a "Digest" panel at
  the top of the Macro/News page, above the existing raw event log —
  exactly as scoped: no new data plumbing, reads the same
  `macro_market_data`/`macro_events` bus keys `/api/macro` already
  exposed, compressed into indices + commodities/FX values, major
  events from the last 24h, and event counts by category. Tested with
  populated bus data through the actual API layer (not just empty
  state).

## Newly added (2026-07-24, continued) — end-of-day review: 6 real fixes from one log set

- [x] **Open Position section missing spreads** — `/api/trades` only ever
  read single-leg positions; spreads (most of the day's actual exposure,
  given spread-driven auto_strategies) were never included. Added
  `open_spreads` to the response and a new table section on the P&L
  page. Tested end-to-end through the actual API with real spread data.
- [x] **Digest "no summary output" — case-mismatch bug.**
  `macro_market_data`'s real keys are uppercase ("DJI", "NASDAQ", etc.,
  confirmed from `news_macro_agent.py`'s actual fetch code) but the
  digest endpoint checked lowercase — the membership check silently
  failed every time regardless of how much real data existed. Log
  confirmed yfinance fallback was succeeding for every index/commodity
  that session; the data was never the problem. Fixed and verified
  against the exact real data shape.
- [x] **News tracker retention was never actually enforced.**
  `prune_tracker_file()` existed but was never called anywhere —
  confirmed live (1000+ accumulated entries). Wired into `NewsAgent`
  (hourly throttle), default changed 120h→48h (2 market days, per
  request).
- [x] **News tracker sort order made explicit + creation time now
  shown.** Was relying on file-append order + reversal, fragile now
  that two agent threads (NewsAgent, NewsMacroAgent) write to the same
  file. Now explicitly sorted by `fetched_ts` descending. Added a Time
  column to the table — previously not shown at all, so "is this
  actually sorted by recency" wasn't even visible.
- [x] **Table height/scroll constraints** — Order History (20 rows),
  Day-wise P&L (5 rows), News Tracker (15 rows), all per the request,
  with sticky headers so the column labels stay visible while scrolling.
- [x] **`MTFConfluenceAgent` was silently giving zero log visibility**
  when it never fired — meaning a full day of no signals looked
  identical whether it was legitimately finding no qualifying setup
  (plausible — 5 strict conditions by design) or failing silently for
  a data/config reason. Same "why is X silent" gap already fixed for
  PA strategies and spread auto-deploy; this agent had been missed.
  Added a periodic breadcrumb, plus a specific alert for the "no Dhan
  client" case (the one genuinely surprising failure given
  broker=dhan and the strategy enabled in their actual config).
- [x] **Data-driven fix: spreads picking a risk:reward worse than the
  premium justified.** User's complaint ("picking spreads where loss
  projection is higher compared to profits") checked directly against
  7 real spreads opened live that day: most clustered at 15-22% credit-
  to-width fraction (right at the old `credit_min_frac` floor),
  producing 4-5.6:1 risk:reward AGAINST the trader (e.g. NIFTY
  bear_call: ₹15.2 credit risking ₹84.8). The two spreads with a
  naturally higher credit fraction (35-44%) had a far more reasonable
  1.2-1.8:1 ratio. Raised `credit_min_frac`'s bounds floor 0.08→0.25
  and default 0.15→0.28 — same tradeoff pattern as the wall_gap_frac
  fix (fewer eligible setups, meaningfully better ones). Verified the
  new floor correctly rejects the two worst real spreads from that day
  while still allowing the two reasonable ones through. The
  `get_params()` bounds-clamp built earlier the same day already
  retroactively catches any stale persisted `credit_min_frac` too —
  confirmed directly, no additional migration code needed.
- Investigated, confirmed NOT a code bug: **SENSEX "0 chain days"**.
  The user's own log shows the actual causes directly — a local TLS
  certificate path error, a 401 "Dhan access token expired", and
  repeated DNS resolution failures for api.dhan.co — all consistent
  with the nightly archive sync running while the machine is asleep/
  disconnected or after that day's token expired. Confirmed SENSEX-
  specific by comparing spread trade counts across all 4 symbols in
  the uploaded backtests.json (NIFTY/BANKNIFTY/FINNIFTY all have real
  spread trades; SENSEX has zero for both). Practical fix is
  operational (keep the machine awake/connected around 15:45 IST),
  not a code change.

### Explicitly deferred from this same review — need their own scoping, not rushed

- [ ] **Futures-derivatives-focused strategy** — user wants strategies
  that actively trade/analyze futures, not just the existing OI-
  buildup supportive signal into MTF Confluence. This is a genuinely
  new strategy family (position type, P&L accounting, entry/exit
  rules distinct from options) — needs its own scoping conversation,
  not a same-session addition on top of everything else here.
- [x] **TradingView webhook integration — DONE (2026-07-24), scoped
  honestly.** Confirmed via live search (July 2026) before building
  anything: TradingView has NO query API — the only real, officially-
  supported integration is alert webhooks (write a Pine Script on
  tradingview.com, set an alert, TradingView POSTs JSON to a URL when
  it fires). Webhooks require Essential/Pro/Pro+/Premium — the paid
  plan the user has unlocks this.
  Built the receiving half: `Orchestrator.webhook_signal()` translates
  a TradingView alert's index-level direction into an actual option
  trade — picks the current ATM strike from the live chain, computes
  an ATR-scaled premium stop (same clamped approach already used by
  MTFConfluenceAgent, including its sanity-clamp fix), then routes
  through the IDENTICAL risk pipeline every other strategy uses. New
  `/api/tradingview/webhook` endpoint with mandatory shared-secret
  validation (503 if unset, 401 if wrong — this endpoint could place
  real trades if compromised, so it's closed by default) and flexible
  field-name acceptance (symbol/ticker, direction/action — different
  Pine templates use different names).
  Tested: 8 scenarios on `webhook_signal()` directly (not-running,
  bad direction, duplicate position, missing chain data, risk-agent
  rejection, bearish→BUY_PE, all direction synonyms, large-ATR clamp
  reuse) plus 5 on the actual HTTP endpoint (no secret configured,
  wrong secret, correct secret, missing fields, alias field names) —
  all through the real FastAPI TestClient, not mocked in isolation.
  Also confirmed the webhook secret never leaks in plaintext via the
  settings API.
  Wrote `docs/tradingview-webhook-setup.md` with a full Pine Script
  v5 template implementing the MACD+Stoch Confluence rule set
  (matching `mtf_confluence_strategy.py`'s logic), INCLUDING real
  `strategy.entry()`/`strategy.exit()` calls (ATR-based stop/target,
  same rr=2.0 convention used throughout this codebase) so TradingView's
  own Strategy Tester gives genuine backtest validation — the user
  specifically asked for "strategy validation", which needs real
  entries/exits, not just alert markers. Caught and fixed two real
  bugs in my own Pine draft before finalizing it: `ta.stoch()` returns
  a single float in Pine v5, not a tuple like `ta.macd()` (my first
  draft used invalid tuple-destructuring syntax on it), and a dead
  unused variable left over from an earlier draft.
  **Honest limitations, stated directly in the doc**: no programmatic
  pull of TradingView's Strategy Tester results into ltp-monitor (that
  stays a manual on-TradingView step); no live chart embed (a separate,
  purely visual feature); the app must be reachable from the internet
  for TradingView to deliver webhooks (ngrok/Cloudflare Tunnel/a real
  host — not just localhost) — a real operational requirement, not
  something code can solve; the Pine Script itself has been written to
  match documented v5 syntax but has NOT been run on TradingView from
  this environment (no way to execute Pine Script here) — flagged
  explicitly for the user to verify in the Pine Editor before trusting
  it live, same discipline as every other "can't test this myself"
  item this project.

- [x] **Global Markets Snapshot "just a number, it can be used" — DONE
  (2026-07-24).** Added `_update_global_sentiment()` to
  `NewsMacroAgent`: averages chg_pct across DJI/NASDAQ/SPX/
  RUSSELL2000, classifies "risk_on" (avg >= +0.75%) / "risk_off"
  (avg <= -0.75%) / "neutral", stored on `global_risk_sentiment`.
  Wired into `mtf_confluence_strategy.evaluate()` as a second
  supportive-only input alongside futures-OI buildup — smaller
  adjustment (+/-5 vs futures' +/-10, since it's a broader macro
  factor, not symbol-specific), never blocks, stacks correctly with
  futures buildup when both are present. Also surfaced visibly in the
  Digest panel (risk-on/risk-off badge with the averaging detail), not
  just used internally. Tested: 5 sentiment-classification scenarios
  (broad selloff/rally/mixed/no-data/all-None), 6 confluence-scoring
  scenarios confirming no regression on the existing futures-buildup
  logic plus correct stacking of both supportive signals, and one
  end-to-end test confirming the value actually flows from
  NewsMacroAgent through the real MTFConfluenceAgent code path.
- [ ] **>500 tracked news items — relevance filtering.** Flagged
  previously as a known gap (`valid` currently just checks for a
  non-empty title). The retention fix (48h) will shrink the accumulated
  count going forward, but doesn't address whether all ~9 configured
  feeds are actually producing execution-relevant content — a genuine
  review of feed suitability, not done in this pass.

## Newly added (2026-07-24, continued) — regression root cause found: bounds fixes don't retroactively apply to persisted versions

User reported the system went from profitable to losing after updating.
Given real trade data (07-16 through 07-24): 07-16/17/20/21 all
negative (pre-fix era), 07-22/23 positive after early fixes, 07-24
negative again (-₹2,183) despite having MORE wins than losses (20 vs
8) — the signature of a few large losses outweighing many small wins.
Traced the 3 largest losses (-3983/-1200/-748, combined -5,931) to
"short strike breached" — the EXACT failure mode the wall_gap_frac fix
(0.4→1.5 bounds floor, 2026-07-23) was supposed to have already fixed.

- [x] **Root cause: `backtester.get_params()` returns persisted
  strategy-version parameters completely unvalidated against current
  bounds.** Raising a bounds floor in code (as the wall_gap_frac fix
  did) only changes what FUTURE auto-tuning steps can move a value
  TOWARD — `tune()` only checks bounds during an incremental
  relax/tighten step. A version already tuned and persisted to an
  out-of-bounds value BEFORE the fix landed keeps being returned
  verbatim, forever, since nothing re-validates it on read. This is
  the same "stale persisted state survives a code fix" pattern hit
  repeatedly with config.json defaults this project — except this
  time in `strategy_versions.json`, a different persistence layer,
  which the earlier fixes didn't touch.
  **Fixed**: `get_params()` now clamps every parameter against its
  current bounds (`strategies.SPREAD_BOUNDS` / `pa_strategies.
  PA_BOUNDS`) on every single read — the one function every consumer
  (auto-deploy, PriceActionAgent, backtests) goes through — so a stale
  persisted value can never again silently outlive a bounds fix.
  Tested: reproduced the exact regression scenario (a persisted
  wall_gap_frac=0.8 from before the fix) and confirmed it's now
  clamped to 1.5 on read; confirmed values already in-bounds are left
  untouched; confirmed values above the ceiling also get clamped down;
  confirmed the no-persisted-version fallback path still works;
  confirmed PA strategy bounds (mtf_confirm) clamp the same way;
  verified end-to-end through the actual `backtester` module the app
  imports, not just in isolation.
- Noted, not a bug: `spread_profit_target_pct` in the live config was
  30% (vs. the ~10% that was actually producing profitable captures in
  an earlier session's analysis), but observed captures during the day
  ranged from ~10% (early trades) to ~37% (a couple of late-morning
  trades) — consistent with `profit_target` being computed ONCE at
  spread creation from whatever the config value was AT THAT MOMENT,
  never retroactively updated for already-open positions. This
  strongly suggests the config value was raised mid-session (e.g.
  during testing) rather than being a code-side bug — flagging for the
  user's awareness, not something the code should "fix," since config
  changes correctly apply going forward only.
- Other live config values worth the user's own review, not touched
  since they read as intentional manual settings rather than stale
  defaults: `daily_loss_limit: 2000` (notably tighter than the 5000
  default), `stop_after_consecutive_losses: 9` (notably more tolerant
  than earlier defaults), `cooldown_after_loss_min: 1` (very short).
  None of these were changed — flagged for the user's own judgment.

## Newly added (2026-07-24, continued) — ema_mtf never fired: root cause found

Second live-log review, same session. User asked why ORB/vwap_pullback/
ema_mtf showed no signals — turned out ORB and vwap_pullback WERE
firing (2 ORB signals became real approved+taken trades; vwap_pullback
fired but hit risk-gate rejections on regime/confluence, not silent).
`ema_mtf` was the genuinely, permanently silent one — not a market-
conditions issue, a real structural bug.

- [x] **Bug: `ema_mtf` could never fire with its own default settings.**
  Root cause, found in two parts:
  1. `c15_today` (today's 15-minute candles) was computed in
     `MarketDataAgent`'s candle-fetch method but never included in the
     dict stored to `pa_candles:{symbol}` — only `c1`/`c5` were stored.
  2. `PriceActionAgent.cycle()` then called `pa.evaluate(name,
     pack["c1"], pack["c5"], None, ...)` — the 15-min candles argument
     was hardcoded to `None`, not read from `pack`.
  `ema_mtf`'s `mtf_confirm` (the DEFAULT parameter value) requires BOTH
  `c5` and `c15` present — with `c15` always `None`, it bailed at that
  check on every single call, permanently, completely independent of
  whether a genuine 5/13 EMA cross was happening on the 1-minute
  candles. Fixed both points.
  Tested rigorously: constructed synthetic candles producing a genuine,
  precisely-timed fresh EMA cross confirmed by trending 5m/15m data —
  confirmed it fires correctly with real `c15` data, then re-ran the
  IDENTICAL setup with only `c15=None` to directly reproduce the exact
  original bug (same cross, same confirmation data, only the missing
  argument differs) — confirming this precise mechanism was the root
  cause, not a coincidence. Also verified end-to-end through the real
  `PriceActionAgent.cycle()` wiring (not just the isolated strategy
  function), and confirmed with `backtester.is_live_enabled` mocked
  that this fires and publishes an actual signal.
- [x] **New: per-strategy `no_setup` diagnostic breakdown.** The
  existing skip-reason counter aggregated all three PA strategies
  (orb/vwap_pullback/ema_mtf) into one `no_setup` number — meaning it
  was structurally impossible to tell from the log which strategy was
  actually silent, which is exactly the question that got asked. Added
  a per-strategy breakdown alongside the existing aggregate (kept for
  backward compatibility), surfaced in both the live summary and the
  10-minute diagnostic breadcrumb. Tested: confirms each strategy's
  no-setup count is correctly attributed individually.

## Newly added (2026-07-24, continued) — live production log review

First real activity-log review from a live paper-trading session
(2026-07-24 market hours). Two real bugs found and fixed; two genuine
live-data gaps diagnosed with better tooling (not blindly guess-fixed,
since this sandbox can't reach the live scrip master to debug
directly); one dead feed flagged.

- [x] **Bug: "daily profit target reached" message nonsensical on a
  PASSING check.** `check()` logs its label on every call regardless
  of pass/fail (prefixed ✓/✗) — the message was hardcoded to always
  read as the failure case ("reached... locking in"), so a normal
  passing check (day P&L still under target) printed "✓ daily profit
  target reached (₹0 ≥ ₹50000)" — literally false arithmetic shown as
  if true. Fixed: message now correctly describes whichever state
  actually holds. Tested both states directly.
- [x] **Finding, not a bug: live spread profit-target is ~10%, not the
  18% default.** Reverse-engineered from 5 real closed-spread captures
  in the log — 4 of 5 landed within a few paise of a 10%-of-credit
  threshold (BANKNIFTY: captured 3.8 vs a 10% threshold of 3.785,
  essentially exact), not the 18% default set earlier this week. This
  means the user's saved config.json has an old/custom
  `spread_profit_target_pct` value that the code default can't
  override — the same "stale config survives a code-default change"
  pattern hit repeatedly this project (daily_loss_limit, news cooldown,
  etc.), now confirmed happening again for this specific key.
  **Deliberately not changed** — today's actual results at ~10% were
  all net-positive (5/5 closed spreads profitable, +₹2,882 combined),
  so overriding a live user setting that's currently working well
  based on a theoretical 18% recommendation would be presumptuous.
  Flagged to the user directly; their Settings page shows the true
  current value if they want to check/adjust it themselves.
- [x] **Futures OI lookup failing live for SENSEX and FINNIFTY** —
  exactly the risk flagged when this was built (SENSEX's "BSE"
  exchange code was explicitly noted as unconfirmed; FINNIFTY's
  monthly-only 2026 listing change was a known open question).
  Since this sandbox can't reach the live scrip master to debug
  directly, added STAGED diagnostics to
  `get_current_future_detailed()` instead of guessing a fix: it now
  reports which filter stage (exchange code / instrument tag /
  underlying-symbol name / all-expired) eliminated every candidate,
  and for a symbol-name mismatch specifically, lists the actual
  underlying_symbol values seen in the file. Tested against three
  constructed failure scenarios (wrong exchange code, symbol name
  variant, all-expired) — each correctly diagnosed with a distinct,
  actionable `likely_cause`. No `agents.py` change needed — the richer
  diagnostic dict flows through the existing log line automatically.
  **Root cause confirmed same day from the next live run**: the
  diagnostics worked exactly as designed — `symbol_matches: 0` with
  `sample underlying_symbol values seen: ['']` for NIFTY, BANKNIFTY,
  AND FINNIFTY (133,656 rows matched the exchange, 15 matched
  FUTIDX, 0 matched a symbol name) — `SM_SYMBOL_NAME` is empty for
  every live FUTIDX row, not just unreliable for FINNIFTY specifically
  as first suspected. Fixed: `_derive_underlying_symbol()` now falls
  back to parsing `SEM_TRADING_SYMBOL`'s prefix before the first
  hyphen (confirmed reliably populated — "FINNIFTY-Jul2026-FUT" in the
  user's own original sample row) whenever `SM_SYMBOL_NAME` is empty.
  Tested against the exact live failure (empty SM_SYMBOL_NAME,
  populated SEM_TRADING_SYMBOL) for FINNIFTY and NIFTY, confirmed no
  regression when SM_SYMBOL_NAME IS populated, and confirmed graceful
  degradation (empty string, never a crash) for a malformed trading
  symbol with no hyphen. Added as a permanent regression test in
  `test_dhan_scrip_master.py` (not just an ad-hoc check) so this exact
  failure mode is covered going forward. SENSEX's exchange-code
  question remains open — no SENSEX failure appeared in this
  particular log excerpt to confirm either way.
- [ ] **Financial Express RSS feed confirmed dead** (`HTTP Error 410:
  Gone`) — this was the user's own provided URL, not one added
  speculatively. Error handling worked correctly (logged clearly,
  didn't crash, didn't block the other 3 Indian feeds or any global
  feed). Not silently swapped or removed — flagged for the user to
  either find an updated URL or remove it via the feed-management UI.

## Newly added (2026-07-24) — News agent merge + RSS engine

At the user's request: two previously-separate news pipelines
(NewsAgent's single Google-News RSS query for risk-gating, and
NewsMacroAgent's NewsAPI-based global_macro/constituent/weather
categories) were independently fetching and classifying overlapping
stories — the user's exact complaint was "picking similar information
again and again."

- [x] **`news_engine.py`** — new shared module, used by BOTH agents:
  - RSS 2.0 + Atom feed parsing (stdlib `xml.etree`, no new dependency)
  - Category classification into geopolitical / market / tech /
    business / economics / energy / mergers / banking / auto / weather
    / global-macro / other — tested against 10 sample headlines
    spanning every category, all correct
  - Bias classification (bullish/bearish/neutral) — re-verified against
    the two previously-found substring bugs ("war" inside "Warner",
    "rain" inside "Ukraine") to confirm neither recurs in the shared
    module
  - **Impact-window heuristic** (1m/5m/15m) — a documented,
    category+severity-keyword-based estimate of how long a headline is
    likely to matter, explicitly NOT a validated/backtested prediction
    model; a starting point meant to be refined against real outcomes,
    same discipline as the spread profit-target and ATR-stop-clamp
    tuning earlier in this project
  - **Cross-source, cross-agent deduplication** — the actual fix for
    the user's complaint. `is_duplicate()`/`log_tracked_event()` share
    module-level state across both agents (they're threads in the same
    process). Tested: the same story from two different sources is
    deduped; a reworded/different-punctuation version of the same
    story is still caught (normalized signature); a genuinely different
    story is never falsely deduped.
  - Feed source CRUD (add/delete/persist to `~/.ltp-monitor/
    news_feeds.json`) — tested including duplicate-id and delete-
    nonexistent error cases
  - `test_feed()` — validates a URL and returns an actionable
    pass/fail rather than a stack trace, tested against HTTP errors,
    network errors, and empty-feed cases
  - Default feeds: the user's 4 confirmed-working Indian sources
    (Moneycontrol, Economic Times, Financial Express, Business Line)
    plus 5 global feeds (CNBC World/Economy/Finance/Energy, Yahoo
    Finance). **HONEST STATUS**: this sandbox's egress allowlist
    blocks every news domain tested (same restriction hit with
    images.dhan.co earlier) — none of these URLs have been fetched
    live from this environment. The Indian feeds are the user's own
    confirmed sources; the global ones are widely-documented,
    long-standing RSS endpoints, not personally verified. Use
    `/api/news/feeds/test` (or the Test button on the Macro/News page)
    to validate each source from a machine that can actually reach
    these domains before relying on any of them.
- [x] **NewsAgent rewired** to pull from the shared RSS engine instead
  of one hardcoded query. Its exact bus contract (`sentiment`/
  `risk_event`/`flagged_ts`, read by `news_risk_opportunity()` in the
  live risk-gating pipeline) was verified byte-for-byte unchanged after
  the swap — that logic is close to live trading and was deliberately
  left untouched beyond the input source. The existing stale-headline
  dedup (a previously-fixed bug) was re-tested and confirmed still
  works with the new fetch source.
- [x] **NewsMacroAgent rewired** to check the shared dedup before
  logging a NewsAPI-sourced event. Tested end-to-end: fed it a story
  already logged by NewsAgent plus a genuinely new one, confirmed it
  correctly logged only the new story.
- [x] **New "News Tracker" table** on the Macro/News page — every
  item shown with source, description, category, market-impact
  indicator, impact window, action, and a valid/invalid flag, with
  category/region/valid-only filters. Backed by `/api/news/tracker`.
- [x] **RSS feed management UI**, same page — add/delete/test feed
  sources (name, URL, category, region, id), backed by
  `/api/news/feeds` (GET/POST/DELETE) and `/api/news/feeds/test`.
- Regression: found and fixed a real bug during the verification pass
  — an earlier edit had accidentally dropped the `async function
  loadQuality(){` declaration line when inserting the new news
  functions before it, which would have broken the entire Trade
  Quality page (JS syntax error). Caught by running `node -c` on the
  extracted script block before packaging, not after.
- [ ] **Not yet done**: a real relevance/spam filter for RSS-sourced
  items (currently `valid` just checks for a non-empty title — a
  genuine filter, e.g. requiring a category match or a minimum keyword
  density, is a natural next refinement); the impact-window heuristic
  has not been validated against real candle outcomes yet (by design —
  it's flagged as a first-pass model to tune later, not a finished one).

- [ ] **TradingView integration** — the user has explicitly noted this
  needs to support ANALYSIS, not just visualization, and that a paid
  license may be required (which they will arrange). Not started —
  correctly gated on that licensing decision rather than built as a
  /substitute (e.g. a native inline-SVG chart standing in
  for "TradingView integration" would misrepresent what was actually
  delivered). Once a license path is confirmed, this needs its own
  scoping pass: which TradingView product (Charting Library / Advanced
  Charts / Lightweight Charts) fits the "for analysis" requirement,
  and how it connects to this system's live signals.
- [ ] **Avadhut Sathe Triple Screen / 3rd Wave Setup checklists** —
  saved to `docs/strategy-reference/` as requested ("keep at project
  level and learning"). These are dense, chart-pattern-recognition-
  heavy systems (Elliott Wave 3rd-wave counting, Tide/Wave/Ripple
  multi-timeframe structure, candlestick pattern recognition, Fibonacci
  retracement/extension) — not scoped as strategies to build yet; kept
  here as reference material the user may want built out later.

## Newly added (2026-07-22)

- [x] **Persist open positions/spreads to disk, survive restarts/updates.**
  ~~Currently `positions` and `spreads` live only in the in-memory `Bus.state`~~
  Done: `open_state.json` snapshot (atomic write) saved on every
  positions/spreads mutation via a `Bus.set()` hook — catches every
  existing call site automatically, no per-call-site changes needed.
  Restored on `Orchestrator.__init__`, same pattern as closed-trade
  history. Missing/corrupt snapshot loads cleanly as empty rather than
  crashing startup. Tested: round-trip save/restore, position removal
  correctly drops from the snapshot on close, missing/corrupt file
  handling, full Orchestrator startup restore.

- [x] **Capital/margin used per trade + top-of-dashboard capital summary.**
  Done: `positions`/`spreads` now carry `capital_used`/`margin_used` at
  open time, shown as a column in all three positions/spreads tables
  (Dashboard, P&L/Orders, Strategy Library). New capital strip below the
  header shows **Total Capital**, **Capital Used**, **Capital
  Remaining**, and **Day P&L (incl. spreads)** — the P&L figure combines
  today's realized (closed trades) with live unrealized P&L from every
  currently-open position and spread. Backed by
  `sizing.deployed_capital()` (already existed) plus a new `capital`
  block in `Orchestrator.status()`. Tested end-to-end through the actual
  `/api/autopilot/status` response.

## Newly added (2026-07-22, continued)

- [x] **Macro/News relevance filtering + risk/opportunity scoring.**
  Fixed unparenthesized OR queries (bare "guidance"/"merger"/"India"
  matching anything) plus a post-fetch word-boundary relevance check.
  Each macro event now gets a bullish/bearish/neutral read, labeled
  Opportunity/Risk against the active symbol's regime direction instead
  of a flat Info label.
- [x] **ATR-based stoploss + trailing stop.** Alternative to fixed-%
  mode, using the regime engine's atr_pct. Preserves the exact rr=2.0
  ratio for PA strategies regardless of ATR reading so it can't get
  silently rejected by the risk-reward gate.
- [x] **Risk Management panel (partial).** Daily profit target (locks
  in gains, halts new positions for the day), transaction-level
  absolute-rupee SL/target, rupee-based step-ratchet trailing for
  single-leg positions (mirrors the spread ratchet). All exposed in a
  restructured Settings page.
- [x] **Settings page restructured** — the "Trading" card was a 30+
  field wall; split into 6 grouped subcards (Basics, Position Sizing,
  Stop-Loss & Trailing, Risk Management, Spread Management, News Gates)
  in a 2-column inner grid.
- [ ] **"Overall Strategy Level" SL/Target** from the reference image —
  not implemented (shown disabled in the reference too, lower priority).

## Newly added (2026-07-23) — Dhan Live Market Feed websocket

- [x] **Client built** — `dhan_ws.py`, wrapping the official `dhanhq`
  PyPI package's `MarketFeed` class (not hand-rolled binary parsing —
  Dhan's feed is well-documented and the library is first-party, so
  this carries none of the protocol-decoding risk the Kotak websocket
  work had). Built by installing the actual package and reading its
  `marketfeed.py` source directly, not guessing from docs — the
  response dict field names (LTP, OI, security_id, depth, etc.) are
  copied straight from `process_full()`/`process_quote()`.
- [x] **Chain-merge logic** — `merge_tick_into_chain()` updates a
  REST-fetched `chain:{symbol}` dict in place from a live tick, WITHOUT
  wiping REST-only fields the feed doesn't carry (iv, delta/theta/
  gamma/vega) — those stay as last known from the REST snapshot.
- [x] **Dependencies confirmed**: `dhanhq>=2.2.0` added to
  requirements.txt — pulls in `pandas`, `pyOpenSSL`, `websockets`
  (asyncio; distinct from `websocket-client` used by the Kotak client,
  no conflict). This is a real footprint increase (pandas especially)
  worth being aware of.
- [x] **Config toggle** — `market_data_feed`: `"rest"` (default) or
  `"websocket"`. Nothing switches automatically.
- [x] **Tested**: all internal logic that doesn't require a live Dhan
  connection — segment mapping (NIFTY/BANKNIFTY/FINNIFTY→NSE_FNO,
  SENSEX→BSE_FNO), tick parsing, packet-type filtering, unregistered-
  security_id handling, chain-merge correctness (incl. REST-only field
  preservation) — all verified with mocks.
- [x] **Tested against a real Dhan account (2026-07-23) — PASSED, both
  the option leg AND the index, after one round of fixing.** First run:
  NIFTY option leg (23850 CE) streamed correct real-time LTP (147.5),
  OI (3,479,775), and bid/ask (146.55/147.2) within seconds — this is
  the data the whole system actually needs, confirmed working end to
  end. The index (`add_index_instrument`) initially produced zero
  ticks — root cause: it was subscribing in Full mode, whose payload
  includes market depth + OI, neither of which exist for an index (no
  order book, no open interest, since an index isn't directly traded).
  Switched to Ticker mode (LTP+LTT only). Re-tested live: **both the
  index (LTP 23869.6) and the option leg now stream correctly.**
- [x] **Index security_ids double-checked against user-provided values**
  (NIFTY=13, BANKNIFTY=25, FINNIFTY=27, SENSEX=51) — already matched
  exactly in both `dhan_ws.py` and `broker_adapter.py`, no changes
  needed.
- [x] **Design review of Dhan's remaining data-API surface (2026-07-23)**
  — full writeup in `dhan_ws.py`'s module docstring. Summary:
  - **Option Chain / Historical Data**: REST-only by Dhan's own design
    (no websocket variant exists for either) — already correctly used
    via `broker_adapter.py`, unaffected by this work. The right
    architecture is exactly what's built: REST for the slower-changing
    shape (strikes, IV, greeks), this websocket overlaying fast fields
    (LTP/OI/depth) on top.
  - **Full Market Depth (20/200-level, a separate websocket)** —
    deliberately NOT built. Dhan's docs state only NSE Equity/
    Derivatives are enabled, excluding BSE — meaning SENSEX would have
    no depth coverage while the other three symbols would, breaking
    the same-logic-across-all-four-symbols design this app is built
    on. Also, current strategies price off best bid/ask (already in
    the 5-level depth this Live Feed already provides) and don't
    consume deep-book data. Worth revisiting only if a future strategy
    specifically needs it.
  - **Futures** — this system trades options only; no futures
    position/P&L/strategy exists anywhere in the codebase, and Dhan's
    option-chain endpoint doesn't return futures contracts (would need
    a separate scrip-master CSV lookup by `UNDERLYING_SECURITY_ID` +
    `INSTRUMENT=FUTIDX`). This is a strategy-level decision to make
    explicitly, not a data-plumbing gap — flagged rather than built
    speculatively.
- [x] **Wired into the live agent loop as a true hybrid (2026-07-24).**
  `MarketDataAgent` now has `_ensure_ws_client()`/`_sync_ws_feed()`/
  `_on_ws_tick()`. REST stays the ONLY source of chain shape/greeks —
  nothing about the existing REST cycle changed. When
  `market_data_feed` is `"websocket"` (Settings → Broker; default
  remains `"rest"`, zero behavior change unless explicitly opted in):
  a `DhanWebsocketClient` is created once, subscribes all 4 indices in
  Ticker mode immediately, and as each REST poll discovers option-leg
  `security_id`s, they're incrementally added via `subscribe_more()` —
  so the websocket subscription set grows to match whatever REST has
  already confirmed exists, never ahead of it. Ticks merge onto the
  same `chain:{symbol}` dict via `merge_tick_into_chain()` (option
  legs) or a direct spot/ticker update (index).
  Found and fixed a real race condition while wiring this: `subscribe_
  more()` can be called before the async websocket connection is fully
  up, and `MarketFeed.subscribe_symbols()` silently drops (not queues)
  a request made too early — with no error. Added `is_connected()`
  (tracked via on_connect/on_close) and made `subscribe_more()` return
  True/False; `MarketDataAgent` only marks a leg "subscribed" in its
  own bookkeeping if the send actually happened, so a too-early attempt
  correctly retries on the next ~3s cycle instead of being silently
  lost forever.
  Tested end-to-end with mocks: hybrid mode off → zero websocket
  activity (confirmed no client ever created); hybrid mode on → client
  created on first cycle, option legs correctly NOT marked subscribed
  before connection confirmed, correctly subscribed once connected,
  index tick correctly updates chain spot + ticker in place, option
  tick correctly merges LTP/OI/bid/ask. NOT yet run live in this
  wired-in form — the underlying pieces (`dhan_ws.py` itself) are
  live-validated, but this specific integration point needs a live
  market-hours run to confirm before relying on it for real trading.
- [x] **Futures data-plumbing added (2026-07-24)**, at your request with
  real scrip-master example data (BANKNIFTY-Sep2026-FUT, security_id
  68390) — but scoped as DATA ACCESS ONLY, not a new trading strategy.
  This system still trades options exclusively; nothing here opens a
  futures position or references futures P&L anywhere. What it does
  add: `dhan_scrip_master.py` downloads (daily-cached) Dhan's detailed
  scrip master CSV and resolves the CURRENT-MONTH futures contract for
  a symbol by picking whichever unexpired row has the nearest expiry —
  computed dynamically from expiry dates every time, so monthly
  rollover (a new security_id every month, no formula for it) is
  handled automatically with no manual maintenance. `dhan_ws.py` gained
  `add_future_instrument()` (Full mode — futures have real OI/depth,
  unlike the index) that either takes an explicit security_id or calls
  the scrip-master lookup itself.
  Tested: your exact example row (68390) resolves correctly; "nearest
  unexpired, not just listed last" logic verified with dates relative
  to today (not hardcoded, so this stays correct on any day it's run);
  an expired contract and a same-underlying OPTION row mixed into the
  sample are both correctly excluded; SENSEX correctly resolves to
  BSE_FNO (not NSE_FNO, unlike your example list which only covered
  NSE names); unknown symbols and scrip-master schema mismatches both
  fail with an actionable message rather than a crash.
  HONEST STATUS: `dhan_scrip_master.py`'s CSV parsing is validated
  against a constructed sample matching the documented schema — this
  sandbox's egress allowlist blocks images.dhan.co directly (confirmed:
  a direct `curl -I` returns 403), so it has NOT been run against the
  real live file. `test_dhan_scrip_master.py` is written and ready to
  validate that — if it reports a "CSV schema mismatch," the printed
  fieldnames list is what's needed to fix `COLUMN_CANDIDATES`.
  NOT wired into `MarketDataAgent`'s hybrid loop — that loop currently
  only handles index + option instruments. Wiring futures in is a
  reasonable next step once (a) the live CSV test passes and (b) there's
  an actual reason to want futures data flowing (e.g. as a spot proxy,
  or if a futures strategy gets added later) — flagging this as a
  decision point rather than building it speculatively.

## Pending roadmap items (not yet started) — published 2026-07-23

| # | Item | Status |
|---|------|--------|
| 6 | Chart.js visual pass / TradingView Lightweight Charts | Not started |
| 7 | ML scoring (shadow journal is accumulating volume now) | Not started |
| 9 | Trade quality dashboard — expectancy, win rate by hour/setup, exit efficiency | **Done** — `/api/quality` endpoint + Trade Quality nav page |
| 11 | Liquidity-sweep / FVG confluence layered onto OI-wall logic | Not started |
| 12 | ML probability scoring (same track as #7, larger scope) | Not started |
| 13 (partial) | Full visual redesign | Token/color/icon/Settings-layout/journal-filter pass done; charts, Dashboard/P&L/Strategies/Backtest/Agents panel emoji cleanup, and deeper Supabase layout patterns (breadcrumbs, page sections elsewhere) still open |

Detail on each:

- [ ] **#6 — Chart.js visual pass / TradingView Lightweight Charts.** Not
  started. Note: this is distinct from the TradingView-for-analysis
  requirement raised in rinkoo.docx (needs a licensing decision) — this
  item is purely visual/charting for existing dashboard panels.
- [ ] **#7 / #12 — ML probability scoring.** Shadow journal is
  accumulating real volume now; getting closer to viable but not
  started.
- [x] **#9 — Trade quality dashboard.** Done — `/api/quality` endpoint
  + "Trade Quality" nav page: expectancy per trade, profit factor, win
  rate, avg win/loss, exit efficiency (% of each trade's peak MFE
  actually captured). Breakdowns by setup, by symbol, by entry
  hour-of-day (inline net-P&L bar chart), plus MFE/MAE scatter charts
  (vs P&L and volume). Date + symbol filterable.
- [ ] **#11 — Liquidity-sweep / FVG confluence** layered onto the
  existing OI-wall logic. Not started.
- [ ] **#13 (partial) — Full visual redesign, Supabase-docs style.**
  Done so far: color tokens, flat icons, panel headers, metric-card
  style, Settings page grouped/2-column layout, journal date
  filtering. Still open: deeper Supabase layout patterns beyond
  Settings (breadcrumbs, page sections elsewhere), and an emoji
  cleanup pass across Dashboard/P&L/Strategies/Backtest/Agents panel
  headers (only the panels directly touched so far have been cleaned
  up).

## Recently completed (for context, not action items)

- Margin-aware position sizing, spread defense rules, MAE/MFE tracking,
  target/trail-stop retuning (items 1–4 of the original roadmap).
- News/Macro Agent (global markets + macro/news checkpoints, structured
  event log, provider error surfacing for Alpha Vantage/Twelve Data).
- News agent risk/opportunity scoring (roadmap #8) — replaced the old
  blanket block window with directional, decaying scoring.
- Bug fixes: spread sizing hard-zeroing instead of falling back to
  minimum lot; backtest replaying today's in-progress day as if closed;
  repeating/stale news alerts; substring keyword false-positives (e.g.
  "war" matching inside "Warner"); multi-timeframe confluence reading
  stale prior-day candles early in the session; cross-symbol signal
  staleness in the manual "Confirm & place order" flow.
