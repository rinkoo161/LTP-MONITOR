# Build Prompt — NSE Market Intelligence & Equity Advisory Platform ("MarketSense")

> Copy everything below the line into Claude Code (or a new Claude conversation) as your build brief.
> Recommended: run in Claude Code with a fresh repo so it can scaffold, run, and test iteratively.

---

## ROLE

You are a senior quantitative engineer and systems architect building a production-grade, multi-agent market intelligence platform for Indian equities (NSE). I am an experienced algo-trading practitioner with an existing Python stack (Kite Connect / Zerodha, a custom **LTP Monitor** tool for real-time macro and price data, Quantman for backtesting). Build to integrate with that stack, not to replace it.

Work incrementally. Ship a running Phase 1 before writing Phase 2 code. Ask me before making irreversible architectural choices (DB engine, message bus, LLM provider). Do not stub critical logic with `pass` or `TODO` — if something can't be built yet, say so explicitly and propose the fallback.

---

## 1. OBJECTIVE

Build **MarketSense**: an always-on system that

1. **Ingests** every NSE corporate-disclosure RSS feed plus supporting market data, for **all NSE-listed equities** (~2,100 main board + ~600 SME).
2. **Classifies and scores** each disclosure for materiality, sentiment, and expected price impact.
3. **Validates fundamentals** — balance sheet quality, earnings trend, cash flow integrity, accounting red flags, promoter/pledge behaviour.
4. **Evaluates price/volume behaviour** — technical state, relative strength, liquidity, delivery %, F&O positioning.
5. **Fuses** these into a single, explainable conviction score per stock with a **Buy / Accumulate / Hold / Reduce / Exit** stance, target zone, invalidation level, and position-sizing guidance.
6. **Alerts** in real time when a high-materiality event intersects with a favourable or deteriorating technical/fundamental setup.
7. **Integrates bidirectionally with my existing LTP Monitor** so the event layer and the live-price layer inform each other.

This is a **decision-support system**, not an auto-trader. See §10.

---

## 2. DATA SOURCES

### 2.1 NSE RSS feeds (primary event layer — poll these)

Base: `https://nsearchives.nseindia.com/content/RSS/`

| Feed | URL suffix | Priority |
|---|---|---|
| Announcements | `Online_announcements.xml` | **P0** |
| Financial Results | `Financial_Results.xml` | **P0** |
| Board Meetings | `Board_Meetings.xml` | **P0** |
| Corporate Actions | `Corporate_action.xml` | **P0** |
| Insider Trading | `InsiderTrading.xml` | **P0** |
| Regulation 29 (SAST) | `Sast_Regulation29.xml` | P1 |
| Regulation 31 (SAST) | `Sast_Regulation31.xml` | P1 |
| Reason For Encumbrance | `Sast_ReasonForEncumbrance.xml` | **P0** (pledge = red flag) |
| Shareholding Pattern | `Shareholding_Pattern.xml` | P1 |
| Related Party Transactions | `Related_Party_Trans.xml` | **P0** (governance) |
| Daily Buy Back / Redemption | `Daily_Buyback.xml` | P1 |
| Integrated Filing – Financials | `Integrated_Filing_Financials.xml` | P1 |
| Annual Reports | `Annual_Reports.xml` | P2 |
| Corporate Governance | `Corporate_Governance.xml` | P2 |
| Secretarial Compliance | `Secretarial_Compliance.xml` | P2 |
| Statement of Deviation & Variation | `Statement_Of_Deviation.xml` | P1 |
| Investor Complaints | `Investor_Complaints.xml` | P2 |
| Voting Results | `Voting_Results.xml` | P2 |
| Share Transfers | `Share_Transfers.xml` | P2 |
| Offer Documents / ISD | `Offer_Documents.xml` | P2 |
| BRSR | `brsr.xml` | P2 |
| Unitholding Patterns | `Unitholding_Patterns.xml` | P2 |
| NSE Circulars | `Circulars.xml` | P1 |

### 2.2 Supporting NSE endpoints
- Corporate filings JSON APIs behind `nseindia.com/companies-listing/...` (announcements, financial results, shareholding, board meetings) — richer than RSS, use as enrichment.
- Daily bhavcopy + delivery position (`sec_bhavdata_full`), F&O bhavcopy, securities master, price bands, surveillance/ASM/GSM lists, index constituents, 52-week high/low, bulk & block deals, FII/DII activity, India VIX.
- Rumour verification and "Queries raised to listed companies" pages — strong early-warning signals.

### 2.3 Fundamentals
- XBRL financial results from NSE (parse directly — this is the authoritative, free, structured source).
- Annual report PDFs for balance sheet detail, contingent liabilities, auditor qualifications, related-party notes.
- Optional adapters (behind a plugin interface, disabled by default): Screener.in, Tijori, Trendlyne, BSE filings for cross-check.

### 2.4 Live price/volume
- **My LTP Monitor** — treat as an existing service. Define a clean contract (see §6) and adapt to whatever it currently exposes; ask me for its interface before coding against it.
- Kite Connect WebSocket for tick/quote data on the watchlist tier.

### 2.5 Access constraints (critical — get this right first)
NSE blocks naive clients. Build a hardened `NSEClient`:
- Browser-like headers, cookie bootstrap from the homepage, automatic cookie refresh on 401/403.
- Per-host rate limiter + jittered exponential backoff, circuit breaker, request budget per minute.
- Response caching with ETag / Last-Modified honoured on RSS.
- One shared session pool — never let 8 agents hammer NSE independently.
- Full audit log of every request, status, and retry.
- Respect NSE terms of use; this is personal research use, single-user, polite polling.

---

## 3. ARCHITECTURE

**Pattern:** independent agents, loosely coupled over an event bus, each with its own lifecycle, health check, and persistence. No agent calls another directly — they publish and subscribe.

```
                    ┌──────────────────────────────┐
                    │   Event Bus (Redis Streams)  │
                    └──────────────────────────────┘
   publishes ▲              ▲            ▲             ▲
             │              │            │             │
 ┌───────────┴──┐ ┌─────────┴───┐ ┌──────┴─────┐ ┌─────┴──────┐
 │ A1 Ingestion │ │ A3 Fundam.  │ │ A4 Technic.│ │ A5 Flow &  │
 │   (feeds)    │ │  Analyst    │ │  Analyst   │ │ Positioning│
 └──────────────┘ └─────────────┘ └────────────┘ └────────────┘
 ┌──────────────┐ ┌─────────────┐ ┌────────────┐ ┌────────────┐
 │ A2 Document  │ │ A6 Risk &   │ │ A7 Fusion  │ │ A8 Alert & │
 │  Intelligence│ │ Governance  │ │ /Portfolio │ │  Delivery  │
 └──────────────┘ └─────────────┘ └────────────┘ └────────────┘
             │              │            │             │
             ▼              ▼            ▼             ▼
        ┌──────────────────────────────────────────────────┐
        │ TimescaleDB/Postgres + object store for PDFs      │
        └──────────────────────────────────────────────────┘
                              ▲
                              │ contract (§6)
                      ┌───────┴────────┐
                      │  LTP Monitor   │
                      └────────────────┘
```

### Agent specifications

**A1 — Ingestion Agent**
Polls all feeds on per-feed schedules (P0 = 30–60s during market hours, 5 min after; P2 = hourly). Deduplicates by content hash + NSE filing ID. Normalises symbol ↔ ISIN ↔ company name against a securities master (handle renames, mergers, symbol changes, series changes). Downloads attached PDFs to object store. Emits `filing.received`. Must be idempotent and support cold-start backfill from NSE's filing APIs.

**A2 — Document Intelligence Agent**
Consumes `filing.received`. Extracts text from PDFs (pdfplumber → OCR fallback for scans). Classifies each filing into a taxonomy: *order win, capex, capacity expansion, M&A, demerger, fundraise (QIP/rights/preferential/warrants), debt raise, credit rating change, results, guidance, dividend/bonus/split/buyback, management change, auditor resignation, regulatory action, litigation, insider trade, pledge creation/release, plant shutdown, fire/accident, clarification to rumour, other.*
Produces per-filing: **materiality 0–10**, **directional sentiment −1..+1**, **confidence**, extracted numeric entities (order value, capex ₹cr, rating notch, stake %, price of issue), and a ≤40-word plain-English summary. Use an LLM for classification with a strict JSON schema and a deterministic rule layer for high-signal patterns (auditor resignation → materiality ≥9 regardless of model output). Cache by document hash. Emits `filing.classified`.

**A3 — Fundamental Analyst Agent**
Maintains a rolling financial history per company (8+ quarters, 5+ years annual). On each results filing, recomputes:
- Growth: revenue/EBITDA/PAT YoY & QoQ, 3Y CAGR, sequential margin trajectory.
- Quality: ROE, ROCE, operating cash flow / EBITDA (flag <0.6 persistently), CFO vs PAT divergence, working-capital days trend, receivable days spike, inventory build.
- Balance sheet: D/E, net debt / EBITDA, interest coverage, contingent liabilities / net worth, goodwill & intangibles share, promoter pledge %.
- **Red-flag battery** (score each, aggregate into a Forensic Score 0–100): auditor qualification or resignation, frequent auditor change, CFO churn, related-party sales growing faster than revenue, other-income dependency, capitalised interest, receivables > 40% of revenue, tax rate anomalies, Beneish M-score, Altman Z-score, Piotroski F-score.
- Valuation: P/E, EV/EBITDA, P/B, PEG vs own 5Y median and vs sector median; DCF and reverse-DCF (what growth does the current price imply?).
Emits `fundamental.updated` with a **Fundamental Score 0–100**, a Forensic Score, and a fair-value band with explicit assumptions.

**A4 — Technical Analyst Agent**
Daily (and intraday for watchlist) per symbol: trend structure (higher-highs/lows, 20/50/200 DMA stack), RSI/ADX/ATR, volatility regime, distance from 52W high/low, relative strength vs Nifty 500 and vs sector index, volume z-score, delivery-% trend, breakout/breakdown with volume confirmation, support/resistance from swing pivots and volume profile. Emits `technical.updated` with **Technical Score 0–100**, trend label, ATR-based stop level, and key levels.

**A5 — Flow & Positioning Agent**
Bulk/block deals, FII/DII activity, shareholding-pattern deltas (promoter, FII, DII, retail QoQ), insider-trading feed aggregated into net promoter/designated-person buying, F&O OI build-up, futures basis, options skew, PCR, IV rank for F&O names, index inclusion/exclusion, ASM/GSM/surveillance flags. Emits `flow.updated` with **Flow Score 0–100**.

**A6 — Risk & Governance Agent**
Independent veto authority. Maintains hard-block and penalty lists: surveillance/ASM/GSM stage, circuit-band restriction, illiquidity (median 20D turnover below threshold), promoter pledge > 25%, auditor resignation in last 4 quarters, SEBI/regulatory action, going-concern flag, high price-band-hitting frequency, penny-stock/microcap filter, low free float. Also computes portfolio-level exposure caps: per-stock, per-sector, per-factor. Can downgrade or suppress any signal from A7 with a stated reason. Emits `risk.assessed`.

**A7 — Fusion & Portfolio Agent**
The only agent that issues a stance. Combines the four scores plus event overlay into a **Conviction Score** with a configurable, versioned weighting profile (default: Fundamental 30 / Technical 25 / Flow 20 / Event 25; profiles for *value*, *momentum*, *event-driven*, *quality*, *swing*, *positional*). Applies A6's veto layer. Produces per stock:
- Stance, conviction 0–100, time horizon, entry zone, target zone with rationale, invalidation/stop, suggested position size (volatility-adjusted, capped by A6), expected holding period.
- **A written thesis**: 3 bullets *for*, 3 bullets *against*, the single thing that would change the view, and the evidence trail (filing IDs, metric values, dates) for every claim. Every number in the thesis must be traceable to a stored record — no unsourced assertions.
- Change detection: only re-issue when stance or conviction moves beyond a hysteresis band, to avoid alert spam.

**A8 — Alert & Delivery Agent**
Routes by severity to Telegram / email / desktop / webhook. Real-time push for P0 materiality events on watchlist and portfolio holdings. Scheduled digests: pre-open (08:15), mid-day, post-close (16:30), weekly review (Sunday). Rate-limits and batches. Every alert links to the full evidence page.

**Orchestrator**
Supervises agent processes, health checks, restart-on-failure, market-calendar awareness (NSE holidays, muhurat, special sessions), backpressure, and a global kill switch. Agents must survive one another's failure — a dead A3 must not stop A1 or A8.

---

## 4. COVERAGE STRATEGY (important — don't brute-force 2,700 stocks)

Tiered processing:
- **Tier 0 — Portfolio + Watchlist** (my holdings and flagged names): full intraday processing, all agents, real-time alerts.
- **Tier 1 — F&O universe + Nifty 200** (~250): daily full analysis, intraday technicals.
- **Tier 2 — Nifty 500 + liquid midcaps** (~500): daily fundamentals & technicals, event-driven deep dive.
- **Tier 3 — All remaining listed equities**: event-triggered only. RSS ingestion and classification always run for everyone; expensive fundamental/DCF work fires only when a material filing or an unusual price/volume/delivery move occurs.
Tier promotion is automatic on a high-materiality event or an anomaly trigger. Make thresholds configurable.

---

## 5. DATA MODEL (sketch — refine as needed)

`securities`, `filings`, `filing_classifications`, `financials_quarterly`, `financials_annual`, `balance_sheet_metrics`, `forensic_flags`, `shareholding`, `insider_trades`, `pledges`, `corporate_actions`, `price_daily` (hypertable), `price_intraday` (hypertable), `fno_daily`, `scores` (time-series per agent per symbol), `signals`, `signal_evidence`, `alerts`, `portfolio_positions`, `agent_runs`, `audit_log`.

Every score and signal is **append-only and versioned with the scoring-model version** so historical performance can be attributed and back-tested honestly.

---

## 6. LTP MONITOR INTEGRATION

Define a bidirectional contract; implement adapters both ways so neither system needs rewriting.

**Inbound (LTP Monitor → MarketSense):** live LTP, OHLC, volume, OI, VIX, index levels, breadth. Prefer a subscribe API (Redis pub/sub, websocket, or ZeroMQ). Fall back to polling a REST/file interface if that's what exists.

**Outbound (MarketSense → LTP Monitor):**
- `event_flag` — push symbols with a live high-materiality filing so the monitor can highlight them.
- `dynamic_watchlist` — auto-add symbols crossing a conviction threshold.
- `risk_flag` — push ASM/GSM/surveillance/pledge warnings so the monitor can block or warn on entry.
- `level_overlay` — support/resistance/stop levels for display.

Ask me for the LTP Monitor's current interface (module path, data structures, transport) before writing the adapter. Build it as `integrations/ltp_monitor.py` with a protocol class and a mock implementation so MarketSense runs standalone in dev.

---

## 7. INTERFACE

FastAPI backend + React dashboard (Tailwind, Recharts):
1. **Market Pulse** — live filing stream with materiality colour-coding, filterable by type/sector/tier; breadth, VIX, FII/DII. Heat map
2. **Signals** — ranked conviction table, stance, score deltas, filters by horizon and profile.
3. **Stock Deep Dive** — price chart with event markers, all four score histories, financial statement trends, forensic flags, filing archive, full thesis with evidence links.
4. **Screener** — compose queries across every stored metric; save and schedule screens.
5. **Portfolio** — holdings with live P&L (from LTP Monitor), per-position stance, concentration and sector exposure, risk warnings.
6. **Agent Health** — per-agent status, lag, throughput, error rate, NSE request budget consumption.
7. **Signal Performance** — hit rate, average return by stance/horizon/profile, calibration curve. Non-negotiable: I need to know whether this thing actually works.

---

## 8. TECH STACK (proposed — challenge it if you disagree)

Python 3.11+ · FastAPI · Redis Streams · TimescaleDB · APScheduler or Prefect · pandas/numpy/polars · pdfplumber + Tesseract · feedparser + httpx · Claude API for A2/A7 reasoning with strict JSON schemas · pytest · Docker Compose · structlog + Prometheus.

Everything must run on a single VPS or my local machine. No cloud lock-in.

---

## 9. BUILD PHASES

**Phase 1 — Foundation (build and demo before proceeding)**
Hardened NSEClient with cookie/rate-limit handling; securities master with ISIN mapping; A1 ingesting all 23 feeds with dedup and backfill; Postgres schema; CLI to query recent filings by symbol. *Acceptance: 48h continuous run, zero duplicate filings, zero unhandled 403s, full feed coverage verified against the NSE website.*

**Phase 2 — Intelligence**
A2 document classification with taxonomy and materiality; PDF extraction; evaluation set of 100 hand-labelled filings with a measured accuracy report. *Acceptance: ≥85% category accuracy, ≥0.7 correlation on materiality vs my labels.*

**Phase 3 — Analysis**
A3 fundamentals from XBRL, A4 technicals from bhavcopy, A5 flow. Historical backfill of 5 years. *Acceptance: financials for Nifty 500 reconcile against Screener.in within 2% on revenue/PAT/debt.*

**Phase 4 — Fusion**
A6 risk, A7 conviction scoring, thesis generation with evidence trails. Backtest scoring on 3 years of history — walk-forward, point-in-time data only, **no look-ahead** (a filing dated 12 May is only visible from its actual timestamp). *Acceptance: backtest report with hit rate, average return by stance, and comparison against a Nifty 500 buy-and-hold baseline.*

**Phase 5 — Delivery & Integration**
A8 alerts, LTP Monitor adapter, dashboard, performance tracking.

---

## 10. GUARDRAILS

- **Decision support only.** No order placement, no broker write APIs, no auto-execution. Every output is a recommendation I act on manually.
- **Every claim must be traceable.** If A7 says "receivables deteriorating," the underlying values and filing IDs must be retrievable. Zero tolerance for LLM-fabricated numbers — the LLM writes prose around computed values, it never produces the values.
- **Point-in-time integrity.** Any backtest that leaks future data is worse than no backtest. Enforce this structurally in the data access layer, not by convention.
- **Calibrated uncertainty.** Every score carries a confidence and a data-freshness stamp. Stale or missing fundamentals must degrade confidence, not silently default to neutral.
- **Show me when it's wrong.** Signal performance tracking is a Phase 5 requirement, not optional polish.
- Rate-limit politely; single-user personal research use.

---

## 11. FIRST RESPONSE

Do **not** start coding yet. Respond with:
1. Your proposed repo structure and module layout.
2. Any disagreements with the architecture above, with reasoning.
3. The three highest-risk parts of this build and how you'd de-risk them.
4. The specific questions you need answered about my LTP Monitor and existing Kite Connect setup.
5. A concrete Phase 1 task list.

Then wait for my go-ahead.
