"""
Multi-agent trading system.

Agents (each runs on its own thread & cadence, communicating through a
shared Bus with a blackboard state store and pub/sub topics):

  market_data    every 3s      chain snapshots (Dhan rate limit = 1 req/3s;
                               upgrade path: Dhan WebSocket for true ticks)
  technical      every 60s     analyzer (OI walls, PCR, risk zones, bias)
  news           every 10min   RSS headlines -> Claude sentiment/risk flags
  social         every 10min   public feeds (Reddit RSS) -> retail mood
  fundamental    daily 8:45    macro/context brief for the day
  strategy       on analysis   builds trade signal (AI + full context)
  risk           on signal     pre-order gate: approves or rejects
  execution      on approval   places orders (paper/live), monitors, exits
  learning       EOD 15:35     reviews the day's trades, writes journal

Message flow:
  strategy -> topic 'signal'   -> risk
  risk     -> topic 'approved' -> execution
  execution-> topic 'closed'   -> learning (journal)

All timings are IST-aware; strategy/risk/execution stand down outside
market hours (09:15-15:30 Mon-Fri).
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta, timezone

import config
from analyzer import analyze, ai_signal, ai_budget_status as _ai_budget

IST = timezone(timedelta(hours=5, minutes=30))
BASE = os.path.dirname(os.path.abspath(__file__))
# Persist trade history + logs in the user's home dir so a code-folder
# update / re-zip never wipes them.
STORE_DIR = os.path.expanduser("~/.ltp-monitor")
os.makedirs(STORE_DIR, exist_ok=True)
JOURNAL = os.path.join(STORE_DIR, "journal.json")
TRADES_FILE = os.path.join(STORE_DIR, "trades.jsonl")   # append-only, one JSON per line
LOG_FILE = os.path.join(STORE_DIR, "activity.log")


def _append_trade(trade: dict):
    """Append a closed trade to the persistent log. Never overwrites."""
    try:
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        print(f"[persist] failed to write trade: {e}")


def load_persisted_trades():
    """Load all historical closed trades from disk on startup."""
    if not os.path.exists(TRADES_FILE):
        return []
    out = []
    try:
        with open(TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"[persist] failed to load trades: {e}")
    return out


def _append_activity(line: str):
    """Append a single activity-log line to disk (best-effort)."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def now_ist():
    return datetime.now(IST)


def market_open():
    t = now_ist()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return 9 * 60 + 15 <= hm <= 15 * 60 + 30


class ClaudeAuthError(Exception):
    """LLM auth error (online key invalid) — distinct from transient errors."""


def claude(prompt, api_key, max_tokens=500):
    """Now routes through the unified local/online LLM layer (Ollama by
    default). api_key kept for signature compatibility."""
    import llm
    text, engine, err = llm.generate_json(prompt, max_tokens)
    if err:
        if "invalid" in err or "401" in err:
            raise ClaudeAuthError(err)
        raise RuntimeError(err)
    return text


# ================================================================== bus

class Bus:
    """Blackboard + pub/sub. Agents write state and publish events."""

    def __init__(self):
        self.state = {}                      # shared blackboard
        self._subs = {}                      # topic -> [callback]
        self._lock = threading.Lock()
        self.feed = deque(maxlen=400)        # global activity feed
        self.alerts = deque(maxlen=100)      # high-priority alert stream

    def set(self, key, value):
        with self._lock:
            self.state[key] = value

    def get(self, key, default=None):
        with self._lock:
            return self.state.get(key, default)

    def subscribe(self, topic, cb):
        self._subs.setdefault(topic, []).append(cb)

    def publish(self, topic, msg):
        for cb in self._subs.get(topic, []):
            try:
                cb(msg)
            except Exception as e:
                self.log("bus", f"⚠ handler error on {topic}: {e}")

    def log(self, agent, msg):
        line = f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S')}] [{agent}] {msg}"
        self.feed.append(line)
        _append_activity(line)
        print("  " + line)

    def alert(self, severity, category, symbol, message):
        """severity: 'high' | 'medium' | 'low'. Pushed to the alert stream
        (dashboard banner/bell) and the activity log."""
        a = {"id": f"{time.time():.6f}", "ts": now_ist().strftime("%H:%M:%S"),
             "severity": severity, "category": category, "symbol": symbol,
             "message": message}
        self.alerts.append(a)
        self.log(category, f"🔔[{severity.upper()}] {symbol}: {message}")
        return a


# ================================================================== base

class Agent(threading.Thread):
    name = "agent"
    interval = 60

    def __init__(self, bus: Bus, ctx: dict):
        super().__init__(daemon=True)
        self.bus, self.ctx = bus, ctx
        self.stop_evt = threading.Event()
        self.last_run = None
        self.status = "idle"
        self.summary = ""

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

    def cycle(self):
        raise NotImplementedError

    def info(self):
        return {"name": self.name, "interval": self.interval,
                "last_run": self.last_run, "status": self.status,
                "summary": self.summary}


# ================================================================== agents

class MarketDataAgent(Agent):
    name = "market_data"
    interval = 3           # Dhan option-chain hard limit: 1 request / 3 s

    def cycle(self):
        syms = self.bus.get("symbols", ["NIFTY"])
        active = self.bus.get("active_symbol") or syms[0]
        i = self.bus.get("_md_idx", 0)
        self.bus.set("_md_idx", i + 1)
        # weighted rotation: every other slot goes to the active symbol
        if i % 2 == 0 and active in syms:
            sym = active
        else:
            others = [s for s in syms if s != active] or syms
            sym = others[(i // 2) % len(others)]
        chain = self.ctx["get_chain"](sym)
        self.bus.set(f"chain:{sym}", chain)
        self.bus.set(f"chain_ts:{sym}", time.time())
        self.bus.set("chain_ts", time.time())
        # spot history for intraday momentum (no extra API calls)
        hist = self.bus.get(f"spot_hist:{sym}", [])
        if chain.get("spot"):
            hist.append((time.time(), chain["spot"]))
            self.bus.set(f"spot_hist:{sym}", hist[-800:])
        # live ticker entry (prev_close filled by app-side cache)
        tick = self.bus.get("ticker", {})
        tick[sym] = {"spot": chain.get("spot"),
                     "ts": now_ist().strftime("%H:%M:%S")}
        self.bus.set("ticker", tick)
        self.summary = f"{sym} {chain.get('spot')} · cycling {len(syms)} indices"


class TechnicalAgent(Agent):
    name = "technical"
    interval = 60

    def cycle(self):
        syms = self.bus.get("symbols", ["NIFTY"])
        done = []
        for sym in syms:
            chain = self.bus.get(f"chain:{sym}")
            if not chain:
                continue
            momentum = compute_momentum(self.bus.get(f"spot_hist:{sym}", []))
            indicators = self._indicators(sym)
            analysis = analyze(chain, momentum=momentum,
                               indicators=indicators)
            if analysis.get("error"):
                continue
            self.bus.set(f"analysis:{sym}", analysis)
            self._check_iv_spike(sym, analysis)
            done.append(f"{sym[:4]}:{analysis['bias'].split()[0][:4]}"
                        + (f"({momentum['trend'][:2]})" if momentum else ""))
            self.bus.publish("analysis", {"symbol": sym})
        self.summary = " · ".join(done) if done else "waiting for market data"

    def _check_iv_spike(self, sym, analysis):
        """Volatility alert: a fast rise in average IV often precedes/
        follows a macro surprise or big event — flag it with a price
        change so the person can react before the move finishes."""
        iv = analysis.get("avg_iv") or 0
        hist = self.bus.get(f"iv_hist:{sym}", [])
        hist.append((time.time(), iv, analysis.get("spot")))
        hist = hist[-400:]
        self.bus.set(f"iv_hist:{sym}", hist)
        if len(hist) < 6:
            return
        target = hist[-1][0] - 900       # ~15 min ago
        past = min(hist, key=lambda x: abs(x[0] - target))
        if abs(past[0] - target) > 600 or not past[1]:
            return
        iv_chg_pct = (iv - past[1]) / past[1] * 100
        last_alert = self.bus.get(f"iv_alert_ts:{sym}", 0)
        if iv_chg_pct >= 20 and time.time() - last_alert > 900:
            spot_chg = (analysis["spot"] - past[2]) if past[2] else 0
            self.bus.alert("high", "volatility", sym,
                           f"IV jumped {iv_chg_pct:.0f}% in ~15m (now "
                           f"{iv:.1f}%) alongside a spot move of "
                           f"{spot_chg:+.1f} — possible news/event-driven "
                           "volatility spike. Widen stops / reduce size.")
            self.bus.set(f"iv_alert_ts:{sym}", time.time())

    def _indicators(self, sym):
        """MACD + Stochastic from 5-minute candles (cached by the client)."""
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            return None
        try:
            candles = d.intraday(sym, "5")["candles"]
        except Exception:
            return None
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        if len(closes) < 35:
            return None

        def ema(vals, n):
            k = 2 / (n + 1)
            e = vals[0]
            out = []
            for v in vals:
                e = v * k + e * (1 - k)
                out.append(e)
            return out

        macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
        signal = ema(macd_line, 9)
        hist_v = macd_line[-1] - signal[-1]
        hh, ll = max(highs[-14:]), min(lows[-14:])
        k_val = (closes[-1] - ll) / (hh - ll) * 100 if hh > ll else 50

        def k_at(i):
            h, l = max(highs[i-13:i+1]), min(lows[i-13:i+1])
            return (closes[i] - l) / (h - l) * 100 if h > l else 50
        d_val = sum(k_at(len(closes)-1-j) for j in range(3)) / 3
        return {"macd_hist": round(hist_v, 2),
                "macd_positive": hist_v > 0,
                "stoch_k": round(k_val, 1), "stoch_d": round(d_val, 1),
                "stoch_zone": ("oversold" if k_val < 20 else
                               "overbought" if k_val > 80 else "neutral")}


def compute_momentum(hist):
    """Intraday momentum from the 3-second spot history."""
    if len(hist) < 5:
        return None
    now = hist[-1]
    spot = now[1]

    def pct_ago(seconds):
        target = now[0] - seconds
        past = min(hist, key=lambda x: abs(x[0] - target))
        if abs(past[0] - target) > seconds * 0.6:
            return None
        return round((spot - past[1]) / past[1] * 100, 3)

    p5, p15 = pct_ago(300), pct_ago(900)
    ref = p15 if p15 is not None else p5
    if ref is None:
        return None
    if ref > 0.12:
        trend = "rising"
    elif ref < -0.12:
        trend = "falling"
    else:
        trend = "flat"
    return {"pct_5m": p5, "pct_15m": p15, "trend": trend, "spot": spot}


class NewsAgent(Agent):
    name = "news"
    interval = 900

    FEEDS = [
        "https://news.google.com/rss/search?q=nifty+OR+sensex+OR+%22indian+stock+market%22&hl=en-IN&gl=IN&ceid=IN:en",
    ]

    def cycle(self):
        heads = []
        for url in self.FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                xml = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
                heads += re.findall(r"<title>(.*?)</title>", xml)[1:16]
            except Exception:
                continue
        heads = [re.sub(r"&\w+;", " ", h)[:140] for h in heads][:15]
        if not heads:
            self.summary = "no headlines fetched"
            return
        if config.load().get("ai_engine", "local") != "off":
            try:
                out = claude(
                    "Market news headlines for Indian indices below. Reply "
                    "ONLY JSON: {\"sentiment\":\"bullish|bearish|neutral\","
                    "\"risk_event\":true|false,\"note\":\"<one line>\"}\n\n"
                    + "\n".join(heads), None, 200)
                j = json.loads(out.replace("```json", "").replace("```", "").strip())
            except ClaudeAuthError as e:
                j = {"sentiment": "neutral", "risk_event": False, "note": str(e)}
                self.bus.log(self.name, f"⚠ {e}")
            except Exception:
                j = {"sentiment": "neutral", "risk_event": False,
                     "note": "AI sentiment unavailable"}
        else:
            j = {"sentiment": "neutral", "risk_event": False,
                 "note": "headlines collected (AI off)"}
        j["headlines"] = heads[:8]
        # State-transition alerting: only alert when we cross from
        # "no risk" -> "risk" (an edge), not every cycle while it lingers.
        # The AI slightly re-wording the same event was defeating hash-dedup.
        prev = self.bus.get("news") or {}
        was_active = bool(prev.get("risk_event"))
        now_active = bool(j.get("risk_event"))
        if now_active and was_active:
            # keep the original timestamp so risk agent's expiry window is
            # measured from when the event was first detected
            j["flagged_ts"] = prev.get("flagged_ts", time.time())
        elif now_active and not was_active:
            j["flagged_ts"] = time.time()
            self.bus.alert("high", "news", "",
                           f"News risk event: {j.get('note','')}")
        self.bus.set("news", j)
        self.summary = f"{j['sentiment']} · risk_event={j['risk_event']}"


class SocialAgent(Agent):
    name = "social"
    interval = 900

    FEEDS = ["https://www.reddit.com/r/IndianStreetBets/.rss",
             "https://www.reddit.com/r/IndianStockMarket/.rss"]

    def cycle(self):
        titles = []
        for url in self.FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ltp-monitor/1.0"})
                xml = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
                titles += re.findall(r"<title>(.*?)</title>", xml)[1:9]
            except Exception:
                continue
        titles = [t[:120] for t in titles][:12]
        if not titles:
            self.summary = "no social data (feeds unavailable)"
            self.bus.set("social", {"mood": "unknown", "posts": []})
            return
        engine_on = config.load().get("ai_engine","local") != "off"
        mood = "unknown"
        if engine_on:
            try:
                out = claude("Retail trader forum post titles (India). One "
                             "word only - overall mood: euphoric, bullish, "
                             "neutral, bearish, or fearful.\n\n" + "\n".join(titles),
                             None, 20)
                mood = out.strip().lower().split()[0]
            except Exception:
                pass
        self.bus.set("social", {"mood": mood, "posts": titles[:6]})
        self.summary = f"retail mood: {mood}"


class FundamentalAgent(Agent):
    name = "fundamental"
    interval = 3600        # checks hourly, produces once per day at ~08:45

    def cycle(self):
        today = now_ist().strftime("%Y-%m-%d")
        cur = self.bus.get("macro") or {}
        if cur.get("date") == today:
            self.summary = f"today's brief ready ({cur.get('stance','')})"
            return
        if now_ist().hour < 8:
            self.summary = "waiting for 08:45 IST"
            return
        news = self.bus.get("news") or {}
        engine_on = config.load().get("ai_engine","local") != "off"
        brief = {"date": today, "stance": "neutral",
                 "note": "no AI key — neutral macro assumption"}
        if engine_on:
            try:
                out = claude(
                    "Write a 3-line pre-market macro brief for Indian index "
                    "option traders for today. End with STANCE: bullish/"
                    "bearish/neutral. Recent headlines:\n"
                    + "\n".join(news.get("headlines", ["none"])), None, 300)
                stance = "neutral"
                m = re.search(r"STANCE:\s*(\w+)", out, re.I)
                if m:
                    stance = m.group(1).lower()
                brief = {"date": today, "stance": stance, "note": out.strip()}
            except Exception as e:
                brief["note"] = f"AI unavailable: {e}"
        self.bus.set("macro", brief)
        self.summary = f"daily brief: {brief['stance']}"


class StrategyAgent(Agent):
    name = "strategy"
    interval = 5           # event-driven; the loop only drains a queue

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._pending = deque()
        bus.subscribe("analysis", self._pending.append)
        self._last_signal_ts = 0
        self._recent_signals = deque(maxlen=6)   # (sym, signal, strike, ts)
        self._backoff_until = 0

    def cycle(self):
        if not self._pending:
            return
        jobs = {}
        while self._pending:                 # dedupe: latest per symbol
            m = self._pending.popleft()
            jobs[m["symbol"]] = m
        if not market_open():
            self.summary = "market closed — standing down"
            return
        if self.bus.get("position"):
            self.summary = "position open — no new signals"
            return
        if time.time() < self._backoff_until:
            self.summary = f"backing off after repeated rejects ({int(self._backoff_until-time.time())}s)"
            return
        if time.time() - self._last_signal_ts < 120:
            return                       # cooldown between signals
        # Check if risk-rejected signals repeat — if so, halt for a while.
        # This avoids the "same BUY_CE 24100 every 2 min for 30 min" spam.
        last_verdict = self.bus.get("last_risk_check") or {}
        if last_verdict.get("verdict") == "REJECTED":
            failed = [c for c in last_verdict.get("checks", []) if c.startswith("✗")]
            hard_reasons = ("daily loss limit", "halted", "cooldown",
                            "trades ", "market open")
            if any(any(hr in f for hr in hard_reasons) for f in failed):
                # 15-min backoff — the reason isn't going to change quickly
                self._backoff_until = time.time() + 900
                self.summary = "hard-reject reason present; 15-min backoff"
                self.bus.log(self.name, self.summary)
                return
        context = {
            "news": self.bus.get("news"),
            "social_mood": (self.bus.get("social") or {}).get("mood"),
            "macro": (self.bus.get("macro") or {}).get("stance"),
        }
        cfg = config.load()
        # Cost control: by default only run the (expensive) signal on the
        # symbol the user is actively watching. The AI gate additionally
        # caches and rate-limits, so this stays cheap.
        if cfg.get("ai_active_only", True):
            active = self.bus.get("active_symbol")
            jobs = {active: jobs[active]} if active in jobs else {}
        best = None
        for sym in jobs:
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis or analysis.get("error"):
                continue
            sig = ai_signal(analysis, context=context)
            self.bus.set(f"signal:{sym}", sig)
            if sig["signal"] != "WAIT" and \
               (best is None or sig["confidence"] > best[1]["confidence"]):
                best = (sym, sig, analysis)
        if best:
            sym, sig, analysis = best
            self.bus.set("last_signal", sig)
            self._last_signal_ts = time.time()
            self.summary = f"{sym}: {sig['signal']} {sig.get('strike','')} conf {sig['confidence']}%"
            self.bus.log(self.name, self.summary)
            self.bus.alert("medium", "strategy", sym,
                           f"{sig['signal'].replace('_',' ')} {sig.get('strike','')} "
                           f"signal generated (confidence {sig['confidence']}%)")
            self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                        "analysis": analysis})
        else:
            self.summary = f"scanned {len(jobs)} indices — WAIT"


class RiskAgent(Agent):
    name = "risk"
    interval = 5

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._queue = deque()
        bus.subscribe("signal", self._queue.append)
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_ts = 0
        self.halted = False
        bus.subscribe("closed", self._on_closed)

    def _on_closed(self, msg):
        pnl = msg.get("pnl", 0)
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_ts = time.time()
            cfg = config.load()
            stop_n = cfg.get("stop_after_consecutive_losses", 2)
            if stop_n and self.consecutive_losses >= stop_n:
                self.halted = True
                self.bus.alert("high", "risk", msg.get("symbol", ""),
                               f"AUTOPILOT HALTED — {self.consecutive_losses} "
                               f"losses in a row. Change to manual or reset in Settings.")
        else:
            self.consecutive_losses = 0

    def evaluate(self, job):
        """Run all pre-order checks. Returns (ok, checks)."""
        sig, cfg = job["signal"], config.load()
        checks = []
        ok = True

        def check(cond, label):
            nonlocal ok
            checks.append(("✓" if cond else "✗") + " " + label)
            ok = ok and cond

        trades = self.bus.get("trades_today", 0)
        check(not self.halted, "autopilot not halted (consecutive losses)")
        cooldown_min = cfg.get("cooldown_after_loss_min", 15)
        if cooldown_min > 0 and self.last_loss_ts:
            since_loss = (time.time() - self.last_loss_ts) / 60
            check(since_loss >= cooldown_min,
                  f"cooldown ({since_loss:.0f}m/{cooldown_min}m since last loss)")
        check(market_open(), "market open")
        check(self.bus.get("position") is None, "no open position")
        check(trades < cfg["max_trades_per_day"],
              f"trades {trades}/{cfg['max_trades_per_day']}")
        check(sig["confidence"] >= cfg["min_confidence"],
              f"confidence {sig['confidence']}≥{cfg['min_confidence']}")
        check(sig.get("entry", 0) > 0 and sig.get("stoploss", 0) > 0,
              "valid price points")
        entry, sl, t1 = sig.get("entry", 0), sig.get("stoploss", 0), sig.get("target1", 0)
        rr = (t1 - entry) / (entry - sl) if entry > sl else 0
        check(rr >= 1.95, f"risk-reward {rr:.1f} ≥ 1:2")
        atm = job["analysis"].get("atm")
        strike = sig.get("strike")
        if sig.get("signal") == "BUY_CE":
            check(strike is not None and atm and strike <= atm,
                  f"strike {strike} not OTM (ATM {atm})")
        elif sig.get("signal") == "BUY_PE":
            check(strike is not None and atm and strike >= atm,
                  f"strike {strike} not OTM (ATM {atm})")
        # News risk is a *soft, time-limited* gate — a single morning
        # headline must not block trades all day. It only blocks for a short
        # window after detection, and only against the risk direction.
        news = self.bus.get("news") or {}
        news_block = False
        if news.get("risk_event"):
            flagged_ts = news.get("flagged_ts", 0)
            within_window = time.time() - flagged_ts < cfg.get("news_block_minutes", 20) * 60
            # only block trades that align WITH the adverse direction, and only
            # briefly right after the event
            if within_window:
                news_block = True
        check(not news_block,
              "no fresh news-risk block" if not news_block
              else f"news risk active (<{cfg.get('news_block_minutes',20)}m old)")
        max_loss = (sig.get("entry", 0) - sig.get("stoploss", 0)) \
            * cfg["lot_sizes"].get(job["symbol"], 75) * cfg["lots_per_trade"]
        check(self.daily_pnl - max_loss > -abs(cfg.get("daily_loss_limit", 5000)),
              f"daily loss limit (risking ₹{max_loss:.0f}, day P&L ₹{self.daily_pnl:.0f})")
        data_age = time.time() - (self.bus.get(f"chain_ts:{job['symbol']}")
                                  or self.bus.get("chain_ts") or 0)
        check(data_age < 30, f"fresh {job['symbol']} data ({data_age:.0f}s old)")
        return ok, checks

    def cycle(self):
        if not self._queue:
            return
        job = self._queue.popleft()
        ok, checks = self.evaluate(job)
        sig = job["signal"]
        verdict = "APPROVED" if ok else "REJECTED"
        self.summary = f"{verdict}: {sig['signal']} {sig.get('strike','')}"
        self.bus.log(self.name, f"{verdict} — " + " · ".join(checks))
        self.bus.set("last_risk_check", {"verdict": verdict, "checks": checks})
        if ok:
            self.bus.alert("high", "risk", job["symbol"],
                           f"Order APPROVED: {sig['signal'].replace('_',' ')} "
                           f"{sig.get('strike','')} — awaiting execution")
            self.bus.publish("approved", job)
        elif sig.get("confidence", 0) >= 60:
            failed = [c for c in checks if c.startswith("✗")]
            self.bus.alert("low", "risk", job["symbol"],
                           f"Signal REJECTED ({sig['confidence']}% conf): "
                           + "; ".join(failed))


class ExecutionAgent(Agent):
    name = "execution"
    interval = 2           # fast: enter approved orders + monitor open pos

    def __init__(self, bus, ctx):
        super().__init__(bus, ctx)
        self._queue = deque()
        bus.subscribe("approved", self._queue.append)

    def cycle(self):
        if self._queue:
            self._enter(self._queue.popleft())
        self._monitor()

    def _enter(self, job):
        cfg = config.load()
        if not cfg["auto_execute"]:
            self.bus.set("pending_confirmation", job)
            self.summary = "signal approved — awaiting your confirmation"
            self.bus.log(self.name, "auto-execute OFF: order waits for "
                                    "manual confirm in dashboard")
            return
        self.place(job)

    def place(self, job, manual=False):
        cfg = config.load()
        sig, analysis, sym = job["signal"], job["analysis"], job["symbol"]
        lot = cfg["lot_sizes"].get(sym, 75)
        qty = lot * cfg["lots_per_trade"]
        leg = "CE" if sig["signal"] == "BUY_CE" else "PE"
        label = f"{sym} {sig['strike']} {leg}"
        fill = sig.get("option_ltp") or sig["entry"]
        if cfg["paper_mode"]:
            order_id = f"PAPER-{int(time.time())}"
            self.bus.log(self.name, f"📄 PAPER BUY {qty} x {label} @ ₹{fill}")
        else:
            orders = self.ctx["orders_factory"]()
            if orders is None or not sig.get("security_id"):
                self.bus.log(self.name, "⚠ cannot place live order "
                                        "(no broker / security_id)")
                return
            resp = orders.place(sym, sig["security_id"], "BUY", qty, "MARKET")
            order_id = resp.get("orderId", "?")
            self.bus.log(self.name, f"🔴 LIVE BUY {qty} x {label} — "
                                    f"order {order_id}")
        self.bus.set("position", {
            "symbol": sym, "strike": sig["strike"], "leg": leg, "qty": qty,
            "entry": fill, "stoploss": sig["stoploss"],
            "target1": sig["target1"], "target2": sig["target2"],
            "spot_invalidation": sig.get("spot_invalidation"),
            "security_id": sig.get("security_id"), "order_id": order_id,
            "opened": now_ist().strftime("%H:%M:%S"), "ltp": fill,
            "pnl": 0.0, "t1_hit": False, "paper": cfg["paper_mode"],
            "manual": manual,
        })
        self.bus.set("trades_today", self.bus.get("trades_today", 0) + 1)
        self.bus.set("pending_confirmation", None)
        self.bus.alert("high", "execution", sym,
                       f"{'PAPER' if cfg['paper_mode'] else 'LIVE'} entry: "
                       f"BUY {qty} x {label} @ ₹{fill}")

    def _monitor(self):
        p = self.bus.get("position")
        if not p:
            self.summary = self.summary or "idle"
            return
        sym = p["symbol"]
        chain = self.bus.get(f"chain:{sym}")
        analysis = self.bus.get(f"analysis:{sym}")
        if not chain:
            return
        row = next((r for r in chain["rows"] if r["strike"] == p["strike"]), None)
        if not row:
            return
        ltp = row[p["leg"].lower()]["ltp"]
        p["ltp"] = ltp
        p["pnl"] = round((ltp - p["entry"]) * p["qty"], 0)
        spot = chain["spot"]
        self.summary = f"{p['strike']} {p['leg']} LTP ₹{ltp} P&L ₹{p['pnl']:.0f}"

        reason = None
        if ltp <= p["stoploss"]:
            reason = f"stoploss (₹{ltp} ≤ ₹{p['stoploss']})"
        elif ltp >= p["target2"]:
            reason = f"target-2 (₹{ltp})"
        elif p["t1_hit"] and ltp <= p["entry"]:
            reason = "gave back gains after T1"
        elif p.get("spot_invalidation") and spot:
            inv = p["spot_invalidation"]
            if (p["leg"] == "CE" and spot < inv) or (p["leg"] == "PE" and spot > inv):
                reason = f"spot invalidation ({spot:.0f} vs {inv})"
        elif not market_open():
            reason = "market closing — squaring off intraday position"

        if not p["t1_hit"] and ltp >= p["target1"]:
            p["t1_hit"] = True
            p["stoploss"] = max(p["stoploss"], p["entry"])
            self.bus.log(self.name, f"✅ T1 hit ₹{ltp} — SL trailed to "
                                    f"breakeven ₹{p['stoploss']}")
        self.bus.set("position", p)
        if reason:
            self.exit(reason)

    def exit(self, reason="manual exit"):
        p = self.bus.get("position")
        if not p:
            return {"error": "no open position"}
        if p["paper"]:
            self.bus.log(self.name, f"📄 PAPER SELL {p['qty']} x {p['symbol']} "
                         f"{p['strike']} {p['leg']} @ ₹{p['ltp']} — {reason} "
                         f"· P&L ₹{p['pnl']:.0f}")
        else:
            orders = self.ctx["orders_factory"]()
            if orders and p.get("security_id"):
                try:
                    resp = orders.place(p["symbol"], p["security_id"], "SELL",
                                        p["qty"], "MARKET")
                    self.bus.log(self.name, f"🔴 LIVE SELL — order "
                                 f"{resp.get('orderId','?')} — {reason} "
                                 f"· est P&L ₹{p['pnl']:.0f}")
                except Exception as e:
                    self.bus.log(self.name, f"⚠ LIVE EXIT FAILED: {e} — "
                                            "close manually on Dhan NOW")
                    return {"error": str(e)}
            else:
                self.bus.log(self.name, "⚠ no broker for live exit — close "
                                        "manually on Dhan")
        now = now_ist()
        closed = dict(p,
                      closed=now.strftime("%H:%M:%S"),
                      closed_date=now.strftime("%Y-%m-%d"),
                      closed_at=now.isoformat(),
                      reason=reason)
        self.bus.set("position", None)
        trades = self.bus.get("closed_trades", [])
        trades.append(closed)
        self.bus.set("closed_trades", trades)
        _append_trade(closed)          # persist to disk immediately
        self.bus.alert("high", "execution", p["symbol"],
                       f"Exited {p['strike']} {p['leg']} — {reason} — "
                       f"P&L ₹{p['pnl']:.0f}")
        self.bus.publish("closed", closed)
        return {"closed": closed}


class LearningAgent(Agent):
    name = "learning"
    interval = 300

    def cycle(self):
        t = now_ist()
        today = t.strftime("%Y-%m-%d")
        done = self.bus.get("journal_done")
        trades = self.bus.get("closed_trades", [])
        if done == today:
            self.summary = "today's journal written"
            return
        if t.hour * 60 + t.minute < 15 * 60 + 35:
            self.summary = f"tracking {len(trades)} closed trades; journal at 15:35"
            return
        pnl = sum(x["pnl"] for x in trades)
        wins = sum(1 for x in trades if x["pnl"] > 0)
        stats = {"date": today, "trades": len(trades), "wins": wins,
                 "pnl": pnl}
        engine_on = config.load().get("ai_engine","local") != "off"
        critique = ""
        if engine_on and trades:
            try:
                critique = claude(
                    "You are a trading coach. Review today's option trades "
                    "(entry/exit/reason/P&L below). In 5 bullet lines: what "
                    "worked, what didn't, and one concrete adjustment for "
                    "tomorrow.\n\n" + json.dumps(trades, default=str), None, 400)
            except Exception as e:
                critique = f"(AI review unavailable: {e})"
        entry = {**stats, "critique": critique, "trades_detail": trades}
        journal = []
        if os.path.exists(JOURNAL):
            try:
                journal = json.load(open(JOURNAL))
            except Exception:
                pass
        journal.append(entry)
        json.dump(journal, open(JOURNAL, "w"), indent=2, default=str)
        self.bus.set("journal_done", today)
        self.bus.set("journal_latest", entry)
        self.summary = f"journal: {len(trades)} trades, P&L ₹{pnl:.0f}"
        self.bus.log(self.name, self.summary)


# ================================================================== orchestrator

AGENT_CLASSES = [MarketDataAgent, TechnicalAgent, NewsAgent, SocialAgent,
                 FundamentalAgent, StrategyAgent, RiskAgent, ExecutionAgent,
                 LearningAgent]


class Orchestrator:
    def __init__(self, get_chain, orders_factory):
        self.bus = Bus()
        self.ctx = {"get_chain": get_chain, "orders_factory": orders_factory}
        self.agents = []
        self.running = False
        # Restore historical trades so P&L view survives restarts/updates
        history = load_persisted_trades()
        self.bus.set("closed_trades", history)
        # Only "today's" trades count toward the daily cap
        today = now_ist().strftime("%Y-%m-%d")
        todays = [t for t in history
                  if str(t.get("closed_date", "")) == today
                  or str(t.get("opened", "")).startswith(today)]
        self.bus.set("trades_today", len(todays))
        if history:
            realized = sum(t.get("pnl", 0) for t in todays)
            self.bus.log("orchestrator",
                         f"restored {len(history)} historical trades "
                         f"({len(todays)} today, ₹{realized:.0f} realized today)")

    def start(self, symbol="NIFTY", symbols=None):
        symbols = [s.upper() for s in (symbols or
                   ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])]
        self.bus.set("symbols", symbols)
        self.bus.set("active_symbol", symbol.upper())
        if self.running:
            return self.status()
        self.bus.set("trades_today", self.bus.get("trades_today", 0))
        self.agents = [cls(self.bus, self.ctx) for cls in AGENT_CLASSES]
        for a in self.agents:
            a.start()
        self.running = True
        self.bus.log("orchestrator",
                     f"all {len(self.agents)} agents started on "
                     f"{'+'.join(symbols)} "
                     f"({'PAPER' if config.load()['paper_mode'] else 'LIVE'})")
        return self.status()

    def stop(self):
        for a in self.agents:
            a.stop_evt.set()
        self.running = False
        self.bus.log("orchestrator", "agents stopped")
        return self.status()

    def confirm_pending(self):
        """Manual confirmation of a risk-approved order."""
        job = self.bus.get("pending_confirmation")
        if not job:
            return {"error": "no risk-approved order pending"}
        ex = next((a for a in self.agents if a.name == "execution"), None)
        if not ex:
            return {"error": "execution agent not running"}
        ex.place(job, manual=True)
        return self.status()

    def exit_position(self, reason="manual exit from dashboard"):
        ex = next((a for a in self.agents if a.name == "execution"), None)
        if ex:
            return {**(ex.exit(reason) or {}), **self.status()}
        # not running: nothing to exit
        return {"error": "agents not running", **self.status()}

    def manual_trade(self, symbol: str):
        """Manual 'Confirm & place': still goes through the risk agent."""
        if not self.running:
            return {"error": "Start the agents first — every order must "
                             "pass the risk agent."}
        # a risk-approved order may already be waiting
        if self.bus.get("pending_confirmation"):
            return self.confirm_pending()
        sym = symbol.upper()
        analysis = self.bus.get(f"analysis:{sym}")
        sig = self.bus.get("last_signal")
        if not (analysis and sig) or sig.get("signal") == "WAIT":
            return {"error": "No actionable signal — press Get signal first."}
        job = {"symbol": sym, "signal": sig, "analysis": analysis}
        risk = next((a for a in self.agents if a.name == "risk"), None)
        ex = next((a for a in self.agents if a.name == "execution"), None)
        ok, checks = risk.evaluate(job)
        self.bus.set("last_risk_check",
                     {"verdict": "APPROVED" if ok else "REJECTED",
                      "checks": checks})
        if not ok:
            self.bus.log("risk", "REJECTED manual order — " + " · ".join(checks))
            return {"error": "Risk agent rejected the order",
                    "checks": checks}
        ex.place(job, manual=True)
        return self.status()

    def status(self):
        cfg = config.load()
        return {
            "running": self.running,
            "symbol": self.bus.get("active_symbol"),
            "market_open": market_open(),
            "paper_mode": cfg["paper_mode"],
            "auto_execute": cfg["auto_execute"],
            "trades_today": self.bus.get("trades_today", 0),
            "max_trades_per_day": cfg["max_trades_per_day"],
            "agents": [a.info() for a in self.agents],
            "symbols": self.bus.get("symbols"),
            "ticker": self.bus.get("ticker", {}),
            "position": self.bus.get("position"),
            "last_signal": self.bus.get("last_signal"),
            "last_risk_check": self.bus.get("last_risk_check"),
            "pending_confirmation": bool(self.bus.get("pending_confirmation")),
            "news": self.bus.get("news"),
            "social": self.bus.get("social"),
            "macro": self.bus.get("macro"),
            "journal_latest": self.bus.get("journal_latest"),
            "ai_budget": _ai_budget(),
            "refreshed_at": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "risk_halted": any(getattr(a, "halted", False) for a in self.agents),
            "consecutive_losses": next(
                (a.consecutive_losses for a in self.agents
                 if a.name == "risk"), 0),
            "storage_dir": STORE_DIR,
            "alerts": list(self.bus.alerts)[-30:][::-1],
            "log": list(self.bus.feed)[-50:],
        }
