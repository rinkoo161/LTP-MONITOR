# LTP Option Chain Monitor

A local dashboard for NIFTY, BANKNIFTY, FINNIFTY and SENSEX option chains, inspired by volume/OI based "LTP calculator" style analysis: buyer/seller dominance per strike, OI walls (support/resistance), max pain, PCR, a risk meter, seller risk zones (green → red), a rule-engine that proposes trades, and an optional Claude-powered deep-analysis mode.

## Quick start (Windows / Mac / Linux)

```bash
cd ltp-monitor
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8000 — pick an index tab; data refreshes every 30 seconds.

## Macro data providers

The macro monitor (Macro/News page) fetches global markets through a
fallback chain defined in `macro_providers.py`:

1. **yfinance** — primary. No key, no daily cap, covers every symbol. One
   batched call per checkpoint.
2. **Stooq** — secondary. Currently answers with an anti-bot challenge
   rather than CSV from some hosts; kept because it costs one skipped
   step when unavailable.
3. **Twelve Data** — FX and metals only. Its free tier does not serve
   indices.
4. **Alpha Vantage** — last resort, behind a persistent daily counter
   (`alpha_vantage_daily_budget`, default 20 of its 25/day free cap).

The canonical-symbol → provider-ticker map lives in
`config["macro_symbols"]`, so swapping a provider is a settings edit.
Downstream consumers only ever see the canonical key.

**yfinance is pinned and will need periodic bumping.** It is unofficial —
Yahoo changes endpoints without notice, and cloud IPs are throttled
harder than residential ones. If the macro panel goes quiet, check the
pin in `requirements.txt` first. The chain is ordered so demoting it to
secondary is a one-line change.

Every quote carries `last_updated` and `is_stale`. Cash indices
(`SPX_CASH`, `DJI_CASH`) do not update during the IST session — they show
the previous US close and are flagged, never presented as live. The
index **futures** (`SPX_FUT` etc.) are what the monitor reads during the
session, and they are what drives `global_risk_sentiment`.

## AI deep-analysis mode (optional)

Set your Anthropic API key before starting, then click "Run deep analysis":

```bash
# Windows (PowerShell)
$env:ANTHROPIC_API_KEY="sk-ant-..."
# Mac/Linux
export ANTHROPIC_API_KEY="sk-ant-..."
python app.py
```

Without a key, the built-in rule engine still produces bias, market state, risk zones and trade ideas.






## v5: Lightweight local AI (Mac-safe defaults)

**IMPORTANT: pick the right model for your Mac's RAM before running.**

| Model | Size | Min RAM | Notes |
|-------|------|---------|-------|
| `qwen2.5:1.5b` | ~1 GB | 8 GB | Safe on any Mac, ultra-light |
| `qwen2.5:3b` | ~2 GB | 8 GB | **Recommended default** |
| `llama3.2:3b` | ~2 GB | 8 GB | Meta alternative |
| `phi3:mini` | ~2 GB | 8 GB | Microsoft, fast |
| `qwen2.5:7b` | ~4.7 GB | 16 GB | Better reasoning, needs headroom |
| ~~llama3.1 (8B)~~ | ~4.7 GB | 16 GB | **AVOID on <16 GB Macs — will freeze the system** |

Setup:
```bash
brew install ollama
ollama serve &
ollama pull qwen2.5:3b        # or whichever model you chose above
```

Then in Settings pick the same model. The app **caps Ollama at 4 CPU cores** and unloads it after 2 min idle by default — configurable in Settings if you have more headroom.

## v4: Local AI via Ollama (no token cost)

The system now runs its AI **locally** by default using Ollama — zero API tokens, nothing leaves your machine.

One-time setup:
```bash
# 1. install Ollama
brew install ollama          # macOS  (or download from https://ollama.com)
# 2. start it (runs in background on http://localhost:11434)
ollama serve &
# 3. pull a model (~5GB)
ollama pull llama3.1         # or: qwen2.5  (good for finance/JSON), mistral
```
Then in the dashboard: Settings -> AI Engine -> **local**, model **llama3.1**. The header shows `engine: local (Ollama up)` in green when connected.

Engine options:
- **local** — Ollama only (free, recommended)
- **online** — Anthropic API (costs tokens; needs key)
- **auto** — local first, online fallback
- **off** — rule engine only, no LLM at all

If Ollama is down, the app automatically falls back to the built-in rule engine — it never breaks.

## P&L / Orders tab

A dedicated tab tracks every paper/live order: signal, qty, entry, exit, SL, T1/T2, P&L, target-vs-achieved %, exit reason and mode — plus running totals (realized, unrealized, win rate). This is your learning log.

## v3: Multi-agent architecture

Nine agents on independent cadences, communicating through a shared message bus (blackboard state + pub/sub topics). The Agent System panel on the dashboard shows each agent's live status.

| Agent | Cadence | Role |
|---|---|---|
| market_data | 3s | Dhan chain snapshots (3s is Dhan's hard API limit; WebSocket feed is the tick-level upgrade path) |
| technical | 60s | OI walls, PCR, risk zones, bias |
| news | 10 min | Google News RSS -> Claude sentiment + risk-event flag |
| social | 10 min | Reddit trading forums -> retail mood |
| fundamental | daily ~08:45 | pre-market macro brief |
| strategy | event-driven | builds signal from chain + news + social + macro context |
| risk | on every signal | pre-order gate: market hours, confidence, trade cap, daily loss limit (default ₹5,000), data freshness, news risk events |
| execution | on approval | places/monitors/exits orders (paper or live) |
| learning | EOD 15:35 | day review + AI critique -> journal.json |

Message flow: strategy -> risk -> execution -> learning. No order can reach the execution agent without passing risk — including manual clicks in the dashboard.

## Running continuously / on a separate machine

Keep it running after closing the terminal (Mac/Linux):
```bash
nohup ./start.sh > monitor.log 2>&1 &
```
Stop it with: `pkill -f "python app.py"`

Serve on your LAN from a dedicated machine:
```bash
HOST=0.0.0.0 PORT=8000 python app.py
```
then open http://<machine-ip>:8000 from any device. ⚠ Anyone who can reach that port can place trades with your keys — bind it only on a trusted network / firewall the port. There is no login screen.

Auto-start on boot (Linux server): create /etc/systemd/system/ltp-monitor.service with ExecStart pointing at venv/bin/python app.py, then `systemctl enable --now ltp-monitor`. On a Mac, use a LaunchAgent or simply `nohup`.

Note: the Dhan access token still expires (~24h) — paste a fresh one in Settings each morning; every agent picks it up live without a restart.

## v2: Trading terminal features

**Settings in the portal** — click the gear icon. Enter your Dhan Client ID, Access Token (paste a fresh one when it expires ~daily) and Anthropic key once; they're saved to a local `config.json` so you never touch the terminal again. Start the app with just `./start.sh` or `python app.py`.

**Themes** — the ◐ button toggles dark/light; the choice is remembered.

**AI Trade Signal** — "Get signal" returns a structured decision card: BUY CE / BUY PE / WAIT with exact entry premium, stoploss, two targets, spot-invalidation level, confidence and reasons. Uses Claude when your Anthropic key is set, otherwise the local rule engine.

**Order execution** — "Confirm & place order" trades the signal through Dhan (market order, INTRADAY). "Exit position" squares off. The open position bar shows live LTP and P&L, with automatic SL/target monitoring and a breakeven trail after target-1.

**Autopilot** — analyses every 45s, generates signals, executes when confidence clears your threshold, monitors, and exits on SL/T2/invalidation. Guard rails:
- **Paper mode ON by default** — every order is simulated until you switch it off in Settings.
- **Auto-execute OFF by default** — autopilot only queues signals for your confirmation unless you explicitly enable it.
- Max trades/day cap, one position at a time, full activity log, stop button.

**Strong advice**: run at least 1-2 weeks in paper mode. Track whether the signals actually make money before risking a rupee. Options selling/buying intraday is a negative-sum game after costs for most retail participants.

`config.json` holds your API keys in plain text on your machine — keep the folder private and never share or upload it.

## Live data via Dhan (recommended)

1. Log in at web.dhan.co -> My Profile -> **DhanHQ Trading APIs** -> generate an **Access Token**. Make sure the **Data APIs pack** (~Rs 499/month) is active on your account — the option-chain endpoint needs it.
2. Before starting the app, set (Mac/Linux):

```bash
export DHAN_CLIENT_ID="your-client-id"
export DHAN_ACCESS_TOKEN="your-token"
python app.py
```

The app auto-detects the credentials and switches every symbol — NIFTY, BANKNIFTY, FINNIFTY and SENSEX — to Dhan's live feed (fresh every ~12s, within Dhan's 1-request-per-3s limit). Tokens expire (24h on the basic plan), so regenerate and re-export if you see a 401 message.

## Important facts about the data

1. NIFTY / BANKNIFTY / FINNIFTY come from NSE's public option-chain endpoint. It is a snapshot the exchange refreshes roughly every 1–3 minutes — near real-time, not tick-by-tick. The app polls every 30s and reuses browser-style cookies to stay compatible.
2. NSE blocks cloud/datacenter IPs and aggressive polling. Run this from a normal home/office internet connection. If you see a fetch error, wait a minute or restart — the client automatically rebuilds its session.
3. SENSEX trades on BSE, not NSE. The app pulls it from BSE's public API on a best-effort basis; BSE's endpoints change more often than NSE's. If SENSEX fails, use a broker feed (below).
4. For true tick-level real time (what paid LTP-calculator tools use), plug in a broker market-data API — see `broker_adapter.py`. DhanHQ's feed is free with an account; Zerodha Kite Connect is paid. The adapter file documents the exact dict shape to return; then swap one line in `app.py`.

## What the dashboard shows

- Risk meter (0–100): how hostile conditions are for fresh positions (distance from max pain, one-sided PCR, elevated IV).
- Bias & market state: bullish/bearish/rangebound from PCR, max pain, and writer behaviour (short build-up in PEs = bullish support, in CEs = bearish cap).
- OI walls chart: biggest CE OI = resistance, biggest PE OI = support.
- Chain table with risk zones: each cell is tinted green (safe for writers) → amber → red (danger zone); ⚡ marks heavy intraday churn (volume ≫ ΔOI).
- Trade ideas: rule-engine suggestions with entry hint, stoploss and target zones, plus rationale.

## Disclaimer

This is educational decision-support software, not investment advice. Index options are high-risk instruments; always confirm prices on your broker's live terminal and size positions responsibly.
