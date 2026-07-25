# LTP Monitor — Roadmap

Living list of pending work. Update this file as items are picked up,
completed, or reprioritized — it's the source of truth across sessions,
not the chat history.

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

## Still pending — roadmap items 6, 7, 9, 11, 12

- [ ] #6 — Chart.js visual pass / TradingView Lightweight Charts
- [ ] #7 / #12 — ML probability scoring
- [x] #9 — **Trade quality dashboard** — DONE. New `/api/quality`
  endpoint + "Trade Quality" nav page: expectancy per trade, profit
  factor, win rate, avg win/loss, and exit efficiency (% of each
  trade's peak MFE actually captured — directly measures the
  "giving back gains" pattern). Breakdowns by setup (orb/vwap/ema_mtf/
  spread strategies), by symbol, and by entry hour-of-day (with an
  inline net-P&L bar chart). Date + symbol filterable.
- [ ] #6 — Chart.js visual pass / TradingView Lightweight Charts
- [ ] #7 / #12 — ML probability scoring
- [ ] #11 — Liquidity-sweep / FVG confluence on the OI-wall logic



- [ ] **#6 — Chart.js visual pass / TradingView Lightweight Charts.** Not started.
- [ ] **#7 / #12 — ML probability scoring.** Shadow journal is accumulating
  real volume now; getting closer to viable but not started.
- [ ] **#9 — Trade quality dashboard** — expectancy, win rate by hour/setup,
  exit efficiency. Depends on MAE/MFE tracking (done) as a base.
- [ ] **#11 — Liquidity-sweep / FVG confluence** layered onto the existing
  OI-wall logic. Not started.
- [ ] **#13 (partial) — Full visual redesign, Supabase-docs style.**
  Done so far: color tokens, flat icons, panel headers, metric-card style,
  Settings page grouped/2-column layout, journal date filtering. Still
  open: deeper Supabase layout patterns beyond Settings, and an emoji
  cleanup pass across Dashboard/P&L/Strategies/Backtest/Agents panel
  headers (only the panels directly touched so far have been cleaned up).

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
