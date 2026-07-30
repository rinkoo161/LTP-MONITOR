# TradingView Webhook Integration — Setup Guide

## Honest status before you start

TradingView does not offer a query API — there is no "send TradingView
a symbol, get back analysis" call. The only real, officially-supported
integration is **alert webhooks**: you write a Pine Script
indicator/strategy on tradingview.com, set an alert on a condition,
and TradingView POSTs a JSON payload to a URL you configure when that
condition fires. Everything below is built around that one real
mechanism — nothing here pretends to be more than that.

Confirmed as of July 2026: webhook alerts require TradingView's
**Essential, Pro, Pro+, or Premium** plan (the free plan has 0
technical alerts). Since you have a paid plan, this is unlocked for
you already.

**The Pine Script below has been written to match Pine Script v5
syntax as documented, but has NOT been run on TradingView from this
environment** (no way to execute Pine Script from here). Paste it into
the Pine Editor and check it compiles cleanly before setting an alert
on it — treat this the same way you'd treat any new code in this
project: verify before trusting live.

## What actually happens end to end

1. You paste the Pine Script strategy below into TradingView's Pine
   Editor, on a chart for NIFTY/BANKNIFTY/FINNIFTY/SENSEX.
2. TradingView's own servers compute MACD/RSI/Stochastic/Bollinger
   Bands on TradingView's own candle data — this is the actual
   "candle analysis" happening on TradingView, not a simulation of it.
3. When the script's confluence condition fires, its `alert()` call
   sends a JSON payload to a URL you configure.
4. ltp-monitor's `/api/tradingview/webhook` endpoint receives that
   payload, validates a shared secret, picks the current ATM strike
   from its own live option chain, computes an ATR-scaled premium stop
   (same approach already used by the MTF Confluence strategy), and
   routes the resulting trade through the EXACT SAME risk pipeline
   every other strategy in this system goes through — no exemption.

## Step 1 — make this app reachable from the internet

TradingView's servers need to reach your webhook URL. If you're
running this app locally (as your logs show — a Mac, local venv),
`localhost` is not reachable from TradingView. You need one of:

- **ngrok** (quickest to test): `ngrok http 8000` (or whatever port
  this app runs on) gives you a temporary public URL like
  `https://abc123.ngrok-free.app` that forwards to your local server.
  Free tier URLs change every restart — fine for testing, not for
  anything you want running unattended for days.
- **Cloudflare Tunnel** (`cloudflared`): free, more stable than ngrok's
  free tier, can give you a persistent subdomain.
- **A real server** with a public IP/domain, if you eventually want
  this running somewhere other than your own machine.

Whichever you pick, your webhook URL will be:
`https://<your-tunnel-or-domain>/api/tradingview/webhook`

## Step 2 — set a webhook secret in ltp-monitor

Settings → Trading → "TradingView Webhook" → paste a random string
into "Webhook secret" (anything long and hard to guess — this is the
only thing stopping someone who finds your URL from placing trades).
You'll put this same string in the Pine Script's alert message below.

## Step 3 — the Pine Script

This implements the WRITTEN rule set from rinkoo.docx's MACD+Stoch
Confluence strategy — daily MACD above zero and rising, weekly MACD
turning up after being down, RSI(14) > 40, Stochastic bullish cross
from oversold, price in upper Bollinger Band (all 5 mandatory for
bullish; bearish is the exact mirror) — matching
`mtf_confluence_strategy.py`'s logic as closely as Pine Script allows.

```pinescript
//@version=5
strategy("MACD+Stoch Confluence (ltp-monitor webhook)", overlay=false)

// ---- inputs ----
webhookSecret = input.string("PASTE_YOUR_SECRET_HERE", "Webhook secret")
minConfidence = input.int(70, "Min confidence to send (informational only — the actual gate is all 5 conditions)")

// ---- daily indicators (this script's own chart timeframe should be Daily) ----
[macdLine, signalLine, histLine] = ta.macd(close, 12, 26, 9)
rsi14 = ta.rsi(close, 14)
// ta.stoch() returns a single float (raw %K) in Pine v5, unlike
// ta.macd()'s tuple return — kSmooth=1 in this system's Python
// defaults means %K here is the raw stochastic with no extra
// smoothing, %D is a 3-period SMA of that.
stochK = ta.stoch(close, high, low, 14)
stochD = ta.sma(stochK, 3)
basis = ta.sma(close, 20)
dev = 2 * ta.stdev(close, 20)
upperBB = basis + dev
lowerBB = basis - dev
percentB = (close - lowerBB) / (upperBB - lowerBB)

// ---- weekly MACD histogram, via request.security ----
histWeekly = request.security(syminfo.tickerid, "W", ta.macd(close, 12, 26, 9)[2])

// ---- weekly "turning up after being down" / "turning down after being up" ----
// Approximates mtf_confluence_strategy.py's _macd_hist_uptick_after_down():
// was in the qualifying zone at some point in a lookback window, AND the
// most recent step moves in the target direction.
wasNegative = false
for i = 1 to 10
    v = histWeekly[i]
    if not na(v) and v < 0
        wasNegative := true
wasPositive = false
for i = 1 to 10
    v = histWeekly[i]
    if not na(v) and v > 0
        wasPositive := true
weeklyTurningUp = wasNegative and histWeekly > histWeekly[1]
weeklyTurningDown = wasPositive and histWeekly < histWeekly[1]

// ---- stochastic bullish/bearish cross from oversold/overbought ----
stochBullCross = ta.crossover(stochK, stochD) and (ta.lowest(stochK, 5) < 30 or ta.lowest(stochD, 5) < 30)
stochBearCross = ta.crossunder(stochK, stochD) and (ta.highest(stochK, 5) > 70 or ta.highest(stochD, 5) > 70)

// ---- the 5 mandatory conditions, each side ----
bullDailyMacd = histLine > 0 and histLine > histLine[1]
bullWeeklyMacd = weeklyTurningUp
bullRsi = rsi14 > 40
bullStoch = stochBullCross
bullBB = percentB > 0.8
bullConfluence = bullDailyMacd and bullWeeklyMacd and bullRsi and bullStoch and bullBB

bearDailyMacd = histLine < 0 and histLine < histLine[1]
bearWeeklyMacd = weeklyTurningDown
bearRsi = rsi14 < 60
bearStoch = stochBearCross
bearBB = percentB < 0.2
bearConfluence = bearDailyMacd and bearWeeklyMacd and bearRsi and bearStoch and bearBB

// ---- ATR for both the stop/target sizing and the webhook payload
// (ltp-monitor scales this into a premium-based stop using the same
// 1.5xATR distance / rr=2.0 target convention used everywhere else in
// that codebase — momentum_buy, PA strategies, MTF Confluence) ----
atr14 = ta.atr(14)
longStop = close - 1.5 * atr14
longTarget = close + 3.0 * atr14     // rr = 2.0, matches ltp-monitor's convention
shortStop = close + 1.5 * atr14
shortTarget = close - 3.0 * atr14

// ---- entries: only when flat, so this can't pile up duplicate
// positions or re-fire the webhook while one is already open — same
// duplicate-prevention idea as webhook_signal()'s own "already have an
// open position" check on the ltp-monitor side ----
if bullConfluence and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    payload = '{"secret":"' + webhookSecret + '","symbol":"' + syminfo.ticker +
              '","direction":"bullish","strategy":"macd_stoch_confluence","atr":' +
              str.tostring(atr14) + ',"confidence":' + str.tostring(minConfidence) + '}'
    alert(payload, alert.freq_once_per_bar_close)

if bearConfluence and strategy.position_size == 0
    strategy.entry("Short", strategy.short)
    payload = '{"secret":"' + webhookSecret + '","symbol":"' + syminfo.ticker +
              '","direction":"bearish","strategy":"macd_stoch_confluence","atr":' +
              str.tostring(atr14) + ',"confidence":' + str.tostring(minConfidence) + '}'
    alert(payload, alert.freq_once_per_bar_close)

// ---- exits: ATR-based stop/target, evaluated every bar a position is
// open (this drives what TradingView's own Strategy Tester reports —
// this is the part that actually gives you real "strategy validation"
// numbers on TradingView, not just alert markers on a chart) ----
if strategy.position_size > 0
    strategy.exit("Long Exit", "Long", stop=longStop, limit=longTarget)
if strategy.position_size < 0
    strategy.exit("Short Exit", "Short", stop=shortStop, limit=shortTarget)

plot(histLine, "Daily MACD Hist", color = histLine >= 0 ? color.green : color.red, style = plot.style_columns)
plotshape(bullConfluence, "Bull confluence", shape.triangleup, location.bottom, color.green, size = size.small)
plotshape(bearConfluence, "Bear confluence", shape.triangledown, location.top, color.red, size = size.small)
```

**Important**: `syminfo.ticker` on TradingView will typically be
something like `NIFTY`, `BANKNIFTY`, etc. depending on which exchange
symbol you charted — check it matches exactly what this app expects
(`NIFTY`, `BANKNIFTY`, `FINNIFTY`, `SENSEX`) before relying on it; if
TradingView's ticker differs, hardcode the correct string in the
payload instead of using `syminfo.ticker`.

## Step 4 — set the alert

1. Add the script to a **Daily** chart for the index you want (the
   strategy's daily/weekly logic assumes this).
2. Click "Alert" (clock icon) → Condition: this strategy → "Any alert()
   function call".
3. Under Notifications, check "Webhook URL" and paste
   `https://<your-tunnel>/api/tradingview/webhook`.
4. Save. TradingView will now POST to your app whenever the script's
   `alert()` fires.

## Step 5 — validate before trusting it live

- Trigger a test manually if possible (TradingView's alert dialog has
  a way to test the webhook delivery), and check ltp-monitor's
  activity log for `TradingView webhook (...) <SYMBOL> APPROVED/REJECTED`.
- Confirm a wrong secret gets rejected (401) and a correct one with
  agents not running gets a clear error, not a silent failure.
- Watch the Trade Quality dashboard's "by setup" breakdown — signals
  from this path are tagged `tradingview_<strategy>` and will show up
  there distinctly, the same way `ema_mtf`/`orb`/etc. do.

## What this does NOT give you

- **No programmatic pull of TradingView's backtest results.** The
  script above DOES give you real Strategy Tester validation — with
  `strategy.entry()`/`strategy.exit()` calls added, TradingView's
  Strategy Tester tab will show genuine trade statistics (win rate,
  profit factor, equity curve) for this confluence logic against
  TradingView's own historical data. That's a real capability. What's
  NOT possible is pulling those numbers into ltp-monitor via API —
  reviewing them stays a manual step on tradingview.com.
- **No live chart embed in this app from this change.** This is the
  alert-signal pathway only; embedding TradingView's actual chart
  widget in the dashboard is a separate, purely visual feature (the
  "Chart.js / Lightweight Charts" roadmap item is closer to that, but
  is not TradingView's own charting library).
