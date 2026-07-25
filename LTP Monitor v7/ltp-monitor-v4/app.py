"""
LTP Option-Chain Monitor + Autopilot -- run with:  python app.py
Then open  http://127.0.0.1:8000
Credentials are managed in the dashboard's Settings panel (gear icon).
"""

import os
import time
import traceback

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
import urllib.error
import config
from nse_client import NSEClient, SensexClient
from analyzer import analyze, deep_ai_analysis, ai_signal, ai_visual, ai_deep_dive
from broker_adapter import DhanClient, DhanOrders
from agents import Orchestrator, compute_momentum
import agents

BASE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="LTP Option Chain Monitor")

nse = NSEClient()
bse = SensexClient()
_dhan = None
SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}


def dhan_client():
    """(Re)build the active broker client lazily. Named dhan_client for
    backward-compat but returns the broker selected in Settings."""
    global _dhan, _broker_name
    from broker_adapter import get_active_broker
    Client, _ = get_active_broker()
    name = config.load().get("broker", "dhan")
    if globals().get("_broker_name") != name:
        _dhan = None
        globals()["_broker_name"] = name
    if not Client.available():
        _dhan = None
        return None
    if _dhan is None:
        _dhan = Client()
    return _dhan


def reset_dhan():
    global _dhan
    _dhan = None


def orders_factory():
    c = dhan_client()
    if not c:
        return None
    from broker_adapter import get_active_broker
    _, Orders = get_active_broker()
    return Orders(c)


_chain_cache = {}   # symbol -> (fetched_at, chain) — short TTL, broker-agnostic


_dhan_fallback = None   # dedicated Dhan client for symbols the active
                        # broker can't serve (e.g. Kotak's NSE-only master
                        # lacks SENSEX, which trades on BSE) — independent
                        # of whichever broker is selected in Settings


def _dhan_fallback_client():
    global _dhan_fallback
    if not DhanClient.available():
        return None
    if _dhan_fallback is None:
        _dhan_fallback = DhanClient()
    return _dhan_fallback


def get_chain(symbol: str) -> dict:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"Unknown symbol {symbol}")
    # Several endpoints (analysis/signal/ai_visual) each fall back to a
    # fresh get_chain() call when nothing's cached in the bus yet — at
    # startup several can fire within the same second and multiply real
    # broker requests well past any single agent's pacing. A short TTL
    # here means near-simultaneous callers share one fetch.
    hit = _chain_cache.get(symbol)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]
    d = dhan_client()
    try:
        if d is not None:
            chain = d.option_chain(symbol)
        elif symbol == "SENSEX":
            chain = bse.option_chain()
        else:
            chain = nse.option_chain(symbol)
    except RuntimeError as e:
        # active broker structurally can't serve this symbol (e.g. Kotak
        # + SENSEX) — fall back to Dhan for just this symbol if available,
        # rather than losing the index entirely
        fb = _dhan_fallback_client()
        if fb is None or "lacks SENSEX" not in str(e):
            raise
        chain = fb.option_chain(symbol)
        chain["_fallback_broker"] = "dhan"
    _chain_cache[symbol] = (time.time(), chain)
    return chain


pilot = Orchestrator(get_chain, orders_factory)
pilot.ctx["dhan_client"] = dhan_client


# ------------------------------------------------------------ data & AI
@app.on_event("startup")
def _auto_start_agents():
    """Agents run in the backend automatically; the UI only reflects
    status. Disable with auto_start_agents=false in config."""
    import threading, time as _t
    def boot():
        _t.sleep(2)   # let uvicorn finish binding
        try:
            if not config.load().get("auto_start_agents", True):
                return
            if pilot.running:
                return
            if dhan_client() is None:
                print("  agents idle: no broker credentials yet")
                return
            pilot.start(symbols=["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])
            print("  agents auto-started (backend mode)")
        except Exception as e:
            print("  agent auto-start failed:", e)
    threading.Thread(target=boot, daemon=True).start()


@app.get("/api/analysis/{symbol}")
def api_analysis(symbol: str):
    try:
        sym = symbol.upper()
        # the symbol being viewed becomes the active one: market-data agent
        # refreshes it twice as often and the regime card follows it
        if pilot.running and sym in (pilot.bus.get("symbols") or []):
            pilot.bus.set("active_symbol", sym)
        # prefer the technical agent's momentum-aware analysis when fresh
        cached = pilot.bus.get(f"analysis:{sym}")
        ts = pilot.bus.get(f"chain_ts:{sym}") or 0
        if cached and time.time() - ts < 90:
            return cached
        from agents import compute_momentum
        mom = compute_momentum(pilot.bus.get(f"spot_hist:{sym}", []))
        return analyze(get_chain(sym), momentum=mom)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/signal/{symbol}")
def api_signal(symbol: str, force: bool = False):
    sym = symbol.upper()
    try:
        cached = pilot.bus.get(f"signal_cache:{sym}")
        ts = pilot.bus.get(f"signal_cache_ts:{sym}") or 0
        if cached and not force and time.time() - ts < 45:
            return {"signal": cached,
                    "analysis_snapshot": pilot.bus.get(f"signal_cache_snap:{sym}")}
        analysis = pilot.bus.get(f"analysis:{sym}")
        a_ts = pilot.bus.get(f"chain_ts:{sym}") or 0
        if not analysis or time.time() - a_ts > 90:
            mom = compute_momentum(pilot.bus.get(f"spot_hist:{sym}", []))
            analysis = analyze(get_chain(sym), momentum=mom)
        context = {"news": pilot.bus.get("news"),
                   "social_mood": (pilot.bus.get("social") or {}).get("mood"),
                   "macro": (pilot.bus.get("macro") or {}).get("stance")}
        sig = ai_signal(analysis, context=context)
        sig["symbol"] = sym
        snap = {"spot": analysis["spot"], "bias": analysis["bias"],
                "risk_meter": analysis["risk_meter"]}
        pilot.bus.set(f"signal_cache:{sym}", sig)
        pilot.bus.set(f"signal_cache_ts:{sym}", time.time())
        pilot.bus.set(f"signal_cache_snap:{sym}", snap)
        pilot.bus.set("last_signal", sig)
        pilot.bus.set(f"analysis:{sym}", analysis)
        return {"signal": sig, "analysis_snapshot": snap}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/ai/{symbol}")
def api_ai(symbol: str):
    """Structured deep-dive (writer behaviour, risk zones, scenarios,
    critique) — replaces the old plain-text narrative."""
    try:
        sym = symbol.upper()
        analysis = pilot.bus.get(f"analysis:{sym}") or analyze(get_chain(sym))
        context = {"news": pilot.bus.get("news"),
                   "social_mood": (pilot.bus.get("social") or {}).get("mood"),
                   "macro": (pilot.bus.get("macro") or {}).get("stance")}
        return ai_deep_dive(analysis, context=context)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


# ------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    broker: str | None = None
    zerodha_api_key: str | None = None
    zerodha_access_token: str | None = None
    kotak_consumer_key: str | None = None
    kotak_access_token: str | None = None
    kotak_sid: str | None = None
    kotak_auth_token: str | None = None
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    market_data_feed: str | None = None
    anthropic_api_key: str | None = None
    twelve_data_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    newsapi_api_key: str | None = None
    theme: str | None = None
    paper_mode: bool | None = None
    auto_execute: bool | None = None
    min_confidence: int | None = None
    max_trades_per_day: int | None = None
    lots_per_trade: int | None = None
    max_concurrent_positions: int | None = None
    fee_per_lot: int | None = None
    trail_sl_enabled: bool | None = None
    trail_sl_trigger_pct: int | None = None
    trail_sl_gap_pct: int | None = None
    stop_mode: str | None = None
    atr_stop_multiplier: float | None = None
    trail_sl_mode: str | None = None
    atr_trail_multiplier: float | None = None
    step_trail_enabled: bool | None = None
    step_trail_lock_trigger_rupees: float | None = None
    step_trail_lock_profit_rupees: float | None = None
    step_trail_step_rupees: float | None = None
    step_trail_step_gain_rupees: float | None = None
    auto_strategies: list[str] | None = None
    max_concurrent_spreads: int | None = None
    spread_ai_auto_exit_enabled: bool | None = None
    spread_profit_target_pct: float | None = None
    spread_profit_lock_trigger_pct: float | None = None
    spread_profit_lock_pct: float | None = None
    spread_profit_lock_min_rupees: float | None = None
    mtf_confluence_enabled: bool | None = None
    mtf_min_confidence: float | None = None
    mtf_max_trades_per_day: int | None = None
    tradingview_webhook_secret: str | None = None
    spread_reentry_cooldown_min: int | None = None
    backtest_capital: int | None = None
    margin_per_lot_spread: int | None = None
    dynamic_sizing_enabled: bool | None = None
    risk_pct_per_trade: float | None = None
    max_lots_per_trade: int | None = None
    portfolio_kill_switch_enabled: bool | None = None
    portfolio_max_drawdown: int | None = None
    portfolio_halt_cooldown_min: int | None = None
    time_stop_minutes: int | None = None
    daily_loss_limit: int | None = None
    daily_profit_target: float | None = None
    transaction_stop_loss_rupees: float | None = None
    transaction_target_rupees: float | None = None
    cooldown_after_loss_min: int | None = None
    stop_after_consecutive_losses: int | None = None
    regime_gate_enabled: bool | None = None
    require_tf_confluence: bool | None = None
    ai_enabled: bool | None = None
    ai_engine: str | None = None
    ollama_model: str | None = None
    ai_active_only: bool | None = None
    ai_min_interval: int | None = None
    ai_daily_call_cap: int | None = None
    news_block_minutes: int | None = None
    news_realert_cooldown_minutes: int | None = None


@app.get("/api/settings")
def get_settings():
    return config.public_view(config.load())


@app.post("/api/settings")
def set_settings(s: SettingsIn):
    updates = {k: v for k, v in s.model_dump().items() if v is not None}
    # empty string means "don't change" for secrets
    for k in ("dhan_client_id", "dhan_access_token", "anthropic_api_key", "zerodha_api_key", "zerodha_access_token", "kotak_consumer_key", "kotak_access_token", "kotak_sid", "kotak_auth_token", "twelve_data_api_key", "alpha_vantage_api_key", "newsapi_api_key", "tradingview_webhook_secret"):
        if updates.get(k) == "":
            updates.pop(k)
    cfg = config.save(updates)
    if any(k.startswith(("dhan","zerodha","kotak")) or k=="broker" for k in updates):
        reset_dhan()
    return config.public_view(cfg)


# ------------------------------------------------------------ trading
class ExecuteIn(BaseModel):
    symbol: str


@app.post("/api/execute")
def api_execute(body: ExecuteIn):
    """Manual confirm — still passes through the risk agent."""
    try:
        return pilot.manual_trade(body.symbol)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


class TradingViewWebhookIn(BaseModel):
    """Shape of the JSON payload configured in a TradingView alert's
    'Message' field (see docs/tradingview-webhook-setup.md for the
    exact Pine Script + alert setup). Field names deliberately match
    common TradingView webhook conventions (action/direction, ticker/
    symbol) since different Pine Script authors use different names —
    this accepts either."""
    secret: str
    symbol: str | None = None
    ticker: str | None = None       # alias some Pine templates use
    direction: str | None = None
    action: str | None = None       # alias ("buy"/"sell") some templates use
    strategy: str = "tradingview"
    atr: float | None = None
    confidence: float = 70


@app.post("/api/tradingview/webhook")
def api_tradingview_webhook(body: TradingViewWebhookIn):
    """Receives a TradingView alert webhook and turns it into an actual
    option trade through the standard risk pipeline.

    HONEST STATUS: TradingView has no query API for "analysis" — the
    only real integration is alert webhooks (a Pine Script strategy/
    indicator you write on tradingview.com, with an alert configured
    to POST here when its condition fires). This endpoint is the
    receiving half of that; the sending half is a Pine Script you set
    up on TradingView's own site, using your paid plan (webhooks
    require Essential/Pro/Pro+/Premium, confirmed as of July 2026 —
    the free plan has 0 technical alerts). TradingView's own servers
    must be able to reach this URL — if this app runs locally, that
    means a tunnel (ngrok / Cloudflare Tunnel) or a real public host,
    not just localhost.
    """
    cfg = config.load()
    expected_secret = cfg.get("tradingview_webhook_secret", "")
    if not expected_secret:
        raise HTTPException(503, "No tradingview_webhook_secret configured — "
                                 "set one in Settings before enabling this webhook "
                                 "(prevents anyone who finds this URL from placing trades)")
    if body.secret != expected_secret:
        pilot.bus.log("risk", "🔔 TradingView webhook rejected — wrong secret "
                              "(check the alert's payload against Settings)")
        raise HTTPException(401, "Invalid webhook secret")
    symbol = body.symbol or body.ticker
    direction = body.direction or body.action
    if not symbol or not direction:
        raise HTTPException(400, "Payload must include symbol (or ticker) and "
                                 "direction (or action)")
    result = pilot.webhook_signal(symbol, direction, strategy_name=body.strategy,
                                  atr=body.atr, confidence=body.confidence)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


def body_symbol(s):
    return s.upper()


@app.post("/api/exit")
def api_exit(symbol: str | None = None):
    return pilot.exit_position("manual exit from dashboard",
                               symbol=symbol.upper() if symbol else None)


@app.post("/api/confirm")
def api_confirm():
    return pilot.confirm_pending()


class PilotIn(BaseModel):
    symbol: str


@app.post("/api/autopilot/start")
def pilot_start(body: PilotIn):
    return pilot.start(body.symbol)


@app.post("/api/autopilot/stop")
def pilot_stop():
    return pilot.stop()


@app.get("/api/autopilot/status")
def pilot_status():
    return pilot.status()




# ------------------------------------------------------------ candles & ticker
_prev_close = {}     # symbol -> (date, prev_close)


@app.get("/api/candles/{symbol}")
def api_candles(symbol: str, interval: str = "5"):
    d = dhan_client()
    if d is None:
        return JSONResponse(status_code=502, content={
            "error": "Candles need the Dhan feed — add credentials in Settings."})
    try:
        data = d.intraday(symbol, interval)
        # attach current levels so the chart can draw them
        try:
            a = pilot.bus.get(f"analysis:{symbol.upper()}") or                 analyze(get_chain(symbol))
            data["levels"] = {"support": a["support"],
                              "resistance": a["resistance"],
                              "signal_lines": a.get("signal_lines"),
                              "max_pain": a["max_pain"], "spot": a["spot"]}
        except Exception:
            data["levels"] = None
        return data
    except RuntimeError as e:
        # expected, documented broker limitations (e.g. Kotak has no
        # candle endpoint) — a clean one-line message, not a stack trace
        return JSONResponse(status_code=200, content={
            "error": str(e), "candles": [], "unavailable": True})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


def prev_close_for(symbol: str):
    """Previous session close, cached for the day, derived from candles."""
    import datetime as _dt
    sym = symbol.upper()
    today = _dt.date.today().isoformat()
    hit = _prev_close.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    d = dhan_client()
    if d is None:
        return None
    try:
        c = d.intraday(sym, "15")["candles"]
        days = {}
        for k in c:
            day = _dt.datetime.fromtimestamp(k["time"]).date().isoformat()
            days[day] = k["close"]
        past = sorted([dy for dy in days if dy < today])
        pc = days[past[-1]] if past else None
        if pc:
            _prev_close[sym] = (today, pc)
        return pc
    except Exception:
        return None


@app.get("/api/ticker")
def api_ticker():
    out = {}
    tick = pilot.bus.get("ticker", {}) if pilot.running else {}
    # fast path: all four spots in ONE Dhan quote call (own rate limit)
    quotes = {}
    d = dhan_client()
    if d is not None:
        try:
            quotes = d.quote_ltp()
        except Exception:
            quotes = {}
    import time as _t
    for sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        spot = quotes.get(sym) or (tick.get(sym) or {}).get("spot")
        ts = _t.strftime("%H:%M:%S") if quotes.get(sym) else             (tick.get(sym) or {}).get("ts")
        if quotes.get(sym) and pilot.running:
            hist = pilot.bus.get(f"spot_hist:{sym}", [])
            hist.append((_t.time(), spot))
            pilot.bus.set(f"spot_hist:{sym}", hist[-800:])
        if spot is None:
            ch = pilot.bus.get(f"chain:{sym}")
            spot = ch.get("spot") if ch else None
        pc = prev_close_for(sym) if spot else None
        native = tick.get(sym) or {}
        if pc:
            change = round(spot - pc, 2) if spot else None
            pct = round((spot - pc) / pc * 100, 2) if spot else None
        else:
            # candle-derived prev_close unavailable (e.g. Kotak has no
            # candle endpoint) — fall back to the broker's own
            # change/% fields when it provides them directly
            change = native.get("chg")
            pct = native.get("chg_pct")
        out[sym] = {"spot": spot, "prev_close": pc, "ts": ts,
                    "change": change, "pct": pct,
                    "iv_alert": bool(pilot.bus.get(f"iv_alert_ts:{sym}") and
                                     time.time() - pilot.bus.get(f"iv_alert_ts:{sym}", 0) < 900)}
    return out


@app.get("/api/ai_visual/{symbol}")
def api_ai_visual(symbol: str):
    try:
        sym = symbol.upper()
        analysis = pilot.bus.get(f"analysis:{sym}") or analyze(get_chain(sym))
        context = {"news": pilot.bus.get("news"),
                   "social_mood": (pilot.bus.get("social") or {}).get("mood"),
                   "macro": (pilot.bus.get("macro") or {}).get("stance")}
        return ai_visual(analysis, context=context)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/engine")
def api_engine():
    import llm
    h = llm.health()
    from analyzer import ai_budget_status
    h["budget"] = ai_budget_status()
    return h


@app.get("/api/trades")
def api_trades():
    """Closed + open positions for the P&L dashboard."""
    closed = pilot.bus.get("closed_trades", [])
    positions = pilot.bus.get("positions", {}) or {}
    spreads = pilot.bus.get("spreads", {}) or {}
    pos = pilot.bus.get("position")   # legacy single-position mirror
    realized = sum(t.get("pnl", 0) for t in closed)
    total_fees = sum(t.get("fees", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    unrealized = sum(p.get("pnl", 0) for p in positions.values()) + \
        sum(s.get("pnl", 0) for s in spreads.values())

    # day-wise breakdown (uses closed_date; falls back to date part of
    # 'closed_at' or 'opened' for older trades that predate that field)
    from collections import defaultdict
    by_day = defaultdict(lambda: {"pnl": 0, "fees": 0, "count": 0, "wins": 0})
    for t in closed:
        day = t.get("closed_date") or (t.get("closed_at") or "")[:10] or "unknown"
        d = by_day[day]
        d["pnl"] += t.get("pnl", 0)
        d["fees"] += t.get("fees", 0)
        d["count"] += 1
        if t.get("pnl", 0) > 0:
            d["wins"] += 1
    daily = [{"date": k, "pnl": round(v["pnl"], 0), "fees": v["fees"],
              "trades": v["count"],
              "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0}
             for k, v in sorted(by_day.items(), reverse=True)]

    return {
        "open": pos,
        "positions": positions,       # all concurrently open single-leg trades
        "open_spreads": spreads,      # bug found 2026-07-24: spreads (most of a
                                       # typical day's open exposure, given
                                       # auto_strategies is spread-driven) were
                                       # never included here at all — only on
                                       # the Strategies page. The P&L page's
                                       # "Open Position" section looked empty
                                       # or incomplete even with real capital
                                       # deployed, because it was.
        "closed": closed[::-1],
        "daily": daily,
        "stats": {
            "count": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
            "total_fees": total_fees,
        },
    }


@app.get("/api/strategies/{symbol}")
def api_strategies(symbol: str):
    """Evaluate every library strategy for a symbol: eligibility + preview."""
    import strategies as slib
    sym = symbol.upper()
    analysis = pilot.bus.get(f"analysis:{sym}")
    if not analysis:
        try:
            analysis = analyze(get_chain(sym))
        except Exception as e:
            return {"error": f"no analysis available: {e}"}
    regime = pilot.bus.get(f"regime:{sym}")
    out = []
    auto = config.load().get("auto_strategies") or []
    for name, meta in slib.META.items():
        ev = slib.evaluate(name, analysis, regime) or {}
        out.append({"name": name, **meta,
                    "auto": name in auto,
                    "regime_fit": list(slib.REGIME_FIT[name]),
                    "eval": ev})
    mtf = pilot.bus.get(f"mtf_confluence:{sym}")
    cfg = config.load()
    return {"symbol": sym,
            "regime": (regime or {}).get("regime", "unknown"),
            "paper_mode": config.load()["paper_mode"],
            "strategies": out,
            "open_spreads": pilot.bus.get("spreads", {}),
            "mtf_confluence": {
                "enabled": cfg.get("mtf_confluence_enabled", True),
                "min_confidence": cfg.get("mtf_min_confidence", 70),
                "result": mtf,   # None (no confluence) or the full eval dict
            }}


class StrategyToggleIn(BaseModel):
    name: str
    enabled: bool


@app.post("/api/strategies/toggle")
def api_strategies_toggle(body: StrategyToggleIn):
    """Enable/disable server-side auto-deployment for a strategy."""
    cfg = config.load()
    auto = set(cfg.get("auto_strategies") or [])
    if body.enabled:
        auto.add(body.name)
    else:
        auto.discard(body.name)
    config.save({"auto_strategies": sorted(auto)})
    return {"auto_strategies": sorted(auto)}


class SpreadDeployIn(BaseModel):
    name: str
    symbol: str


@app.post("/api/strategies/deploy")
def api_strategies_deploy(body: SpreadDeployIn):
    """Open a paper credit spread after re-evaluating live prices."""
    import strategies as slib
    if not pilot.running:
        return {"error": "Start the agents first."}
    sym = body.symbol.upper()
    analysis = pilot.bus.get(f"analysis:{sym}")
    regime = pilot.bus.get(f"regime:{sym}")
    if not analysis:
        return {"error": "No analysis yet for " + sym}
    ev = slib.evaluate(body.name, analysis, regime)
    if not ev or not ev.get("eligible"):
        return {"error": "Not eligible right now: "
                + "; ".join((ev or {}).get("reasons", ["unknown"]))}
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "execution agent not running"}
    return ex.enter_spread(ev)


@app.post("/api/strategies/exit")
def api_strategies_exit(id: str):
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "agents not running"}
    return ex.exit_spread(id, "manual exit from Strategies tab")


@app.get("/api/journal/shadow")
def api_journal_shadow(limit: int = 200, from_date: str = "", to_date: str = "",
                        symbol: str = "", verdict: str = ""):
    """Every signal decision, approved or rejected, with resolution
    for rejected ones: did it hit target or stoploss anyway? Supports
    real date-range/symbol/verdict filtering instead of dumping
    everything — needed once this log has weeks of continuous testing
    behind it."""
    import os as _os, json as _j
    if not _os.path.exists(agents.SHADOW_PATH):
        return {"signals": [], "stats": {}, "total_before_filter": 0}
    lines = open(agents.SHADOW_PATH).readlines()
    all_entries = []
    for l in reversed(lines):
        try:
            all_entries.append(_j.loads(l))
        except Exception:
            continue
    total_before_filter = len(all_entries)

    def matches(e):
        d = (e.get("ts") or "")[:10]
        if from_date and d and d < from_date:
            return False
        if to_date and d and d > to_date:
            return False
        if symbol and e.get("symbol") != symbol:
            return False
        if verdict and e.get("verdict") != verdict:
            return False
        return True

    out = [e for e in all_entries if matches(e)][:limit]
    rejected_resolved = [e for e in out if e["verdict"] == "REJECTED"
                         and e["resolution"] not in ("pending",)]
    hit_target = sum(1 for e in rejected_resolved
                     if e["resolution"] == "would_have_hit_target1")
    hit_sl = sum(1 for e in rejected_resolved
                if e["resolution"] == "would_have_hit_stoploss")
    timed_out = sum(1 for e in rejected_resolved
                    if e["resolution"] == "unresolved_timeout")
    # Accuracy must be computed only over CONCLUSIVE outcomes
    # (hit_target or hit_sl) — a timeout means "we don't know", not
    # "the rejection was wrong", and including it in the denominator
    # understates accuracy. Found 2026-07-21: a day with 91 "resolved"
    # entries (including timeouts) reported 36.3% accuracy; the real
    # figure over the 61 conclusive outcomes was 54.1% — a materially
    # different picture of whether the risk agent is adding value.
    conclusive = hit_target + hit_sl
    return {"signals": out, "total_before_filter": total_before_filter,
            "stats": {"total": len(out),
                      "approved": sum(1 for e in out if e["verdict"] == "APPROVED"),
                      "rejected": sum(1 for e in out if e["verdict"] == "REJECTED"),
                      "rejected_resolved": len(rejected_resolved),
                      "rejected_timed_out": timed_out,
                      "rejected_would_have_won": hit_target,
                      "rejected_would_have_lost": hit_sl,
                      "risk_agent_accuracy_pct": round(hit_sl / conclusive * 100, 1)
                          if conclusive else None}}


@app.get("/api/journal")
def api_journal(days: int = 14, from_date: str = "", to_date: str = "", symbol: str = ""):
    """Day-wise readable journal built from closed trades: what was
    traded, why each exited, and a plain-language summary per day.
    Supports a real date range + symbol filter instead of dumping every
    day on one page — long-running paper/live testing makes that
    unusable otherwise."""
    from collections import defaultdict
    closed = pilot.bus.get("closed_trades", [])
    if symbol:
        closed = [t for t in closed if t.get("symbol") == symbol]
    by_day = defaultdict(list)
    for t in closed:
        day = t.get("closed_date") or (t.get("closed_at") or "")[:10]
        if day:
            by_day[day].append(t)
    all_days = sorted(by_day, reverse=True)
    if from_date or to_date:
        day_keys = [d for d in all_days
                    if (not from_date or d >= from_date) and (not to_date or d <= to_date)]
    else:
        day_keys = all_days[:days]
    out = []
    for day in day_keys:
        trades = sorted(by_day[day], key=lambda t: t.get("closed", ""))
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        net = sum(t.get("pnl", 0) for t in trades)
        fees = sum(t.get("fees", 0) for t in trades)
        # plain-language summary (rule-based, not LLM — deterministic and free)
        bits = [f"{len(trades)} trade{'s' if len(trades)!=1 else ''}",
               f"{len(wins)} win{'s' if len(wins)!=1 else ''}",
               f"{len(losses)} loss{'es' if len(losses)!=1 else ''}"]
        top_reason = None
        if losses:
            reasons = [t.get("reason", "") for t in losses]
            sl_count = sum(1 for r in reasons if "stop" in r or "loss" in r)
            if sl_count == len(losses):
                top_reason = "every loss hit its stoploss/loss-limit as designed"
            elif sl_count > len(losses) / 2:
                top_reason = "most losses hit stoploss; a few exited manually or on other rules"
        summary = (f"Net {'profit' if net>=0 else 'loss'} of Rs {abs(round(net)):,} "
                  f"across {', '.join(bits)}"
                  + (f" (fees Rs {round(fees):,})" if fees else "")
                  + (f". {top_reason}." if top_reason else "."))
        out.append({
            "date": day, "net_pnl": round(net, 0), "fees": round(fees, 0),
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "summary": summary,
            "entries": [{"time": t.get("closed"), "symbol": t.get("symbol"),
                        "leg": t.get("leg"), "strike": t.get("strike"),
                        "strategy": t.get("strategy"), "entry": t.get("entry"),
                        "exit": t.get("ltp"), "pnl": t.get("pnl"),
                        "reason": t.get("reason"), "mode": "paper" if t.get("paper") else "live"}
                       for t in trades],
        })
    return {"days": out, "total_days_available": len(all_days)}


@app.get("/api/quality")
def api_quality(from_date: str = "", to_date: str = "", symbol: str = ""):
    """Trade quality analytics (roadmap #9): expectancy, win rate broken
    down by hour-of-day / setup / symbol, and exit efficiency (how much
    of each trade's peak favorable move — MFE — was actually captured).
    All derived from the same closed-trade records the journal uses."""
    closed = pilot.bus.get("closed_trades", []) or []

    def _hour_of(t):
        # prefer opened HH:MM:SS (both single-leg and spreads carry it);
        # fall back to closed time if opened is missing
        hhmmss = t.get("opened") or t.get("closed") or ""
        try:
            return int(hhmmss.split(":")[0])
        except Exception:
            return None

    def _setup_of(t):
        # PA signals carry `source` (orb/vwap_pullback/ema_mtf); spreads
        # carry `strategy`; plain option buys are labeled by leg
        return t.get("source") or t.get("strategy") or \
            (f"{t.get('leg', '?')}-buy" if t.get("leg") != "SPREAD" else "spread")

    def _matches(t):
        d = str(t.get("closed_date", ""))
        if from_date and d and d < from_date:
            return False
        if to_date and d and d > to_date:
            return False
        if symbol and t.get("symbol") != symbol:
            return False
        return True

    trades = [t for t in closed if _matches(t)]
    if not trades:
        return {"has_data": False, "n_trades": 0}

    pnls = [t.get("pnl", 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(trades)
    win_rate = len(wins) / n * 100 if n else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    # Expectancy per trade = (win% * avg win) - (loss% * |avg loss|).
    # This is the single most important number: expected ₹ per trade.
    expectancy = (win_rate / 100 * avg_win) + ((100 - win_rate) / 100 * avg_loss)
    # Profit factor = gross profit / gross loss
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else None

    def _bucket_stats(key_fn):
        buckets = {}
        for t in trades:
            k = key_fn(t)
            if k is None:
                continue
            buckets.setdefault(k, []).append(t.get("pnl", 0))
        out = []
        for k, ps in buckets.items():
            w = [p for p in ps if p > 0]
            out.append({
                "key": k, "trades": len(ps),
                "win_rate": round(len(w) / len(ps) * 100, 1),
                "net_pnl": round(sum(ps), 0),
                "avg_pnl": round(sum(ps) / len(ps), 0),
            })
        return out

    by_hour = sorted(_bucket_stats(_hour_of), key=lambda x: x["key"])
    by_setup = sorted(_bucket_stats(_setup_of),
                      key=lambda x: x["net_pnl"], reverse=True)
    by_symbol = sorted(_bucket_stats(lambda t: t.get("symbol")),
                       key=lambda x: x["net_pnl"], reverse=True)

    # Exit efficiency: for WINNING trades, how much of the peak favorable
    # move (MFE) did we actually keep? capture = final_pnl / mfe. Low
    # capture = leaving money on the table (giving back gains); this is
    # exactly the pattern the profit-lock ratchet was built to address,
    # so it's worth measuring directly.
    eff_samples = []
    for t in trades:
        mfe = t.get("mfe", 0)
        pnl = t.get("pnl", 0)
        if mfe and mfe > 0:
            eff_samples.append(max(0.0, min(1.0, pnl / mfe)))
    exit_efficiency = round(sum(eff_samples) / len(eff_samples) * 100, 1) \
        if eff_samples else None

    # Average adverse excursion endured on WINNERS (how much heat taken
    # before the trade worked) vs on LOSERS.
    win_mae = [t.get("mae", 0) for t in trades if t.get("pnl", 0) > 0 and t.get("mae")]
    loss_mae = [t.get("mae", 0) for t in trades if t.get("pnl", 0) <= 0 and t.get("mae")]

    # Per-trade scatter points for MFE/MAE correlation charts (roadmap
    # extension, mirroring the TradesViz MFE/MAE dashboards): each point
    # is one trade with its MFE, MAE, P&L, and volume (qty), tagged
    # win/loss so the frontend can color them. MFE/MAE here are in ₹
    # (the values already tracked on positions), not price points.
    scatter = []
    for t in trades:
        pnl = t.get("pnl", 0)
        scatter.append({
            "mfe": round(t.get("mfe", 0), 0),
            "mae": round(t.get("mae", 0), 0),
            "pnl": round(pnl, 0),
            "volume": t.get("qty", 0),
            "win": pnl > 0,
            "symbol": t.get("symbol"),
            "setup": _setup_of(t),
        })

    return {
        "has_data": True,
        "n_trades": n,
        "headline": {
            "win_rate": round(win_rate, 1),
            "expectancy": round(expectancy, 0),
            "profit_factor": round(profit_factor, 2) if profit_factor else None,
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "net_pnl": round(sum(pnls), 0),
            "gross_profit": round(gross_profit, 0),
            "gross_loss": round(gross_loss, 0),
            "exit_efficiency_pct": exit_efficiency,
            "avg_mae_winners": round(sum(win_mae) / len(win_mae), 0) if win_mae else None,
            "avg_mae_losers": round(sum(loss_mae) / len(loss_mae), 0) if loss_mae else None,
        },
        "by_hour": by_hour,
        "by_setup": by_setup,
        "by_symbol": by_symbol,
        "scatter": scatter,
    }


@app.get("/api/macro")
def api_macro(days: int = 5):
    """Structured global-markets/macro-news event log fed by NewsMacroAgent:
    Date | Time | Event Type | Event | Impact | Action. Also reports which
    data-provider keys are configured so the UI can prompt for them if not."""
    cfg = config.load()
    events = pilot.bus.get("macro_events", []) or []
    now = time.time()
    out = []
    for e in events:
        age_days = (now - e.get("ts", now)) / 86400
        limit = 30 if e.get("major") else days
        if age_days <= limit:
            out.append(e)
    agent_info = None
    for a in getattr(pilot, "agents", []):
        if getattr(a, "name", "") == "news_macro":
            agent_info = a.info()
            break
    return {
        "events": out,
        "market_data": pilot.bus.get("macro_market_data", {}) or {},
        "risk": pilot.bus.get("macro_risk") or {},
        "agent": agent_info,
        "providers_configured": {
            "twelve_data": bool(cfg.get("twelve_data_api_key")),
            "alpha_vantage": bool(cfg.get("alpha_vantage_api_key")),
            "newsapi": bool(cfg.get("newsapi_api_key")),
        },
    }


@app.websocket("/ws/candles/{symbol}")
async def ws_candles(websocket: WebSocket, symbol: str):
    """FastAPI WebSocket Server, per the requested architecture:
    DhanHQ WS -> Market Data Service -> Candle Builder ->
    **FastAPI WebSocket Server** -> TradingView Lightweight Charts.

    On connect: sends historical 1m candles for today from
    history.py's candles table (persisted by MarketDataAgent's
    _build_candle — reusing the existing schema, security_id
    convention "{symbol}_SPOT_1m"), so the chart isn't empty on load.
    Then streams the live-forming candle (bus key live_candle:{symbol},
    updated on every tick by _build_candle) every ~1s — Lightweight
    Charts' update() call handles both "still the current bar" (in-
    place update) and "a new bar started" (append) from the same
    message shape, so no separate "candle closed" event type is
    needed on the wire.
    """
    import asyncio
    import history
    symbol = symbol.upper()
    await websocket.accept()
    try:
        today = agents.now_ist().strftime("%Y-%m-%d")
        today_start = int(agents.datetime.strptime(today, "%Y-%m-%d")
                          .replace(tzinfo=agents.IST).timestamp())
        conn = history._conn()
        rows = conn.execute(
            """SELECT ts, o, h, l, c FROM candles
               WHERE security_id=? AND ts>=? ORDER BY ts""",
            (f"{symbol}_SPOT_1m", today_start)).fetchall()
        conn.close()
        history_payload = [{"time": r[0], "open": r[1], "high": r[2],
                           "low": r[3], "close": r[4]} for r in rows]
        # Fallback (2026-07-25, fixing "candles are not loaded" reported
        # live): the candle builder is brand new, so its own DB
        # accumulation (candles table above) is genuinely empty until
        # it's run through at least a few minutes of live market data.
        # regime_candles:{symbol} is ALREADY fetched every 90s by
        # RegimeAgent (Feature #2/#3's data) — reused here as an
        # immediate seed so the chart shows something on first load
        # rather than waiting for its own accumulation. These are 5m
        # bars, not 1m, so the chart will show coarser candles until
        # real 1m data builds up and takes over — a real, disclosed
        # tradeoff, not hidden.
        used_fallback = False
        fallback_source = "candle_builder_1m"
        diag = {"db_candles_found": len(rows), "regime_candles_found": 0,
               "regime_candles_today": 0, "rest_error": None}

        def most_recent_session(candles):
            """Bug found live 2026-07-25: strictly requiring calendar-
            date 'today' meant the chart showed nothing whenever
            markets were closed (evening/night/pre-market) — confirmed
            directly: REST returned 350 real candles, every single one
            correctly rejected by the same-day filter simply because
            it wasn't past 00:00 IST of a day the market had actually
            traded. A trader looking at a chart outside market hours
            wants to see the LAST trading session, not a blank panel —
            same principle TradingView's own charts follow. Prefers
            genuine today's candles when the market IS open (keeps the
            live-session behavior correct during trading hours);
            otherwise groups by IST calendar date and returns the most
            recent date's full candle set."""
            if not candles:
                return []
            todays = [c for c in candles if c.get("time", 0) >= today_start]
            if todays:
                return todays
            by_date = {}
            for c in candles:
                t = c.get("time")
                if t is None:
                    continue
                d_str = agents.datetime.fromtimestamp(t, agents.IST).strftime("%Y-%m-%d")
                by_date.setdefault(d_str, []).append(c)
            if not by_date:
                return []
            return by_date[max(by_date.keys())]

        # 2026-07-25 — new DB-backed tiers, ahead of the bus/live-REST
        # ones below: RegimeAgent now persists 1m/5m/15m candles for
        # ALL FOUR symbols on every ~90s cycle (history.upsert_index_
        # candles), so the DB has real coverage regardless of which
        # symbol happens to be receiving live websocket ticks or
        # whether the bus still holds today's in-memory state (e.g.
        # after a restart). Network-independent and faster than a
        # live REST call, so tried first.
        if not history_payload:
            most_recent = history.most_recent_session_candles(f"{symbol}_SPOT_1m")
            diag["db_1m_most_recent_found"] = len(most_recent)
            if most_recent:
                history_payload = most_recent
                used_fallback = True
                fallback_source = "db_1m_most_recent_session"
        if not history_payload:
            most_recent_5m = history.most_recent_session_candles(f"{symbol}_SPOT_5m")
            diag["db_5m_most_recent_found"] = len(most_recent_5m)
            if most_recent_5m:
                history_payload = most_recent_5m
                used_fallback = True
                fallback_source = "db_5m_most_recent_session"
        if not history_payload:
            regime_candles = pilot.bus.get(f"regime_candles:{symbol}") or []
            diag["regime_candles_found"] = len(regime_candles)
            todays = most_recent_session(regime_candles)
            diag["regime_candles_today"] = len(todays)
            if todays:
                history_payload = [{"time": c["time"], "open": c["open"],
                                   "high": c["high"], "low": c["low"],
                                   "close": c["close"]} for c in todays]
                used_fallback = True
                fallback_source = "regime_5m_fallback"
        # Third tier: the existing Price Chart panel reliably shows
        # data because /api/candles makes a live REST call
        # (d.intraday()) rather than depending on any bus/DB state —
        # reused directly here as the most reliable fallback.
        #
        # Bug found 2026-07-25: this tier's exception was silently
        # swallowed (`except Exception: pass`) — directly violating
        # this project's own "fail loud, not silent" principle
        # (documented in agents.py's own design notes, e.g. the
        # NameError-swallowing bug fixed in the Zerodha adapter
        # earlier). If this REST call was the reason a symbol showed
        # no candles, there was previously NO WAY to tell that apart
        # from "genuinely no data anywhere" — fixed to capture and
        # surface the actual error instead.
        if not history_payload:
            try:
                d = dhan_client()
                if d is None:
                    diag["rest_error"] = "no Dhan client (check broker credentials)"
                else:
                    data = d.intraday(symbol, "1")
                    raw = data.get("candles", [])
                    diag["rest_candles_found"] = len(raw)
                    if data.get("error"):
                        diag["rest_error"] = data["error"]
                    todays_rest = most_recent_session(raw)
                    if todays_rest:
                        history_payload = [{"time": c["time"], "open": c["open"],
                                           "high": c["high"], "low": c["low"],
                                           "close": c["close"]} for c in todays_rest]
                        used_fallback = True
                        fallback_source = "rest_live_fallback"
                    elif raw and not todays_rest:
                        diag["rest_error"] = (f"{len(raw)} candles returned but none "
                                              f"are from today — check symbol/timezone")
            except Exception as e:
                diag["rest_error"] = f"{type(e).__name__}: {e}"
        if not history_payload:
            pilot.bus.log("execution", f"⚠ chart websocket: no candles found for "
                          f"{symbol} from any of 3 sources — {diag}")
        await websocket.send_json({"type": "history", "candles": history_payload,
                                   "source": fallback_source, "diagnostics": diag})

        last_sent = None
        no_live_ticks_since = time.time()
        warned_no_live = False
        while True:
            live = pilot.bus.get(f"live_candle:{symbol}")
            if live and live != last_sent:
                await websocket.send_json({"type": "live", "candle": live})
                last_sent = live
                warned_no_live = False
            elif not live and not warned_no_live and \
                    time.time() - no_live_ticks_since > 30:
                # Diagnostic for exactly the reported symptom ("chart
                # only updates for NIFTY, not others"): if this
                # symbol's live_candle bus key has never been set at
                # all after 30s connected, the websocket tick pipeline
                # isn't reaching this symbol — surfaced here rather
                # than silently doing nothing forever.
                #
                # 2026-07-25 — made context-aware rather than a vague
                # "may be closed, or ... isn't connected" hedge: the
                # server already knows whether the market is open, so
                # say so plainly instead of forcing the person to
                # guess which half of the sentence applies.
                warned_no_live = True
                if agents.market_open():
                    msg_text = (f"no live ticks received for {symbol} in the "
                               f"last 30s while the market is open — this "
                               f"symbol's index websocket subscription may "
                               f"not be reaching it (other symbols may still "
                               f"be fine; this is symbol-specific)")
                else:
                    msg_text = (f"no live ticks for {symbol} — market is "
                               f"closed right now, so this is expected; "
                               f"showing the most recent session's candles")
                pilot.bus.log("execution", f"⚠ chart websocket: no live "
                              f"tick ever received for {symbol} after 30s "
                              f"— check whether the index websocket "
                              f"subscription actually covers this symbol "
                              f"(market_open={agents.market_open()})")
                await websocket.send_json({"type": "diagnostic",
                                          "message": msg_text})
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


@app.get("/api/ltp-monitor")
def api_ltp_monitor():
    """Feature #1 (institutional-grade dashboard spec) — Spot vs
    Futures side by side per index: LTP, %, O/H/L, VWAP, previous
    close, live-tick freshness. Extends existing data paths only, no
    new subscriptions:
      - Spot LTP/%/prev_close: already computed in broker_adapter.py's
        option_chain() from Dhan's own previous_close_price field,
        already flowing through chain.chg/chg_pct.
      - Spot O/H/L/VWAP: newly DERIVED here from spot_hist (already
        accumulated by MarketDataAgent every REST cycle) — no new
        tracking added, just reading what already exists.
      - Futures LTP/%/O/H/L/VWAP: newly tracked by
        MarketDataAgent._update_future_ohlc(), extending the existing
        futures websocket tick pipeline built for OI-buildup
        classification — same ticks, additional bookkeeping, no new
        subscription.
    VWAP here is a TWAP proxy (mean of LTP across ticks), not a true
    volume-weighted average — same documented tradeoff already used
    elsewhere in this codebase (AnchorPullback's "session anchor") for
    the same reason: no clean per-trade volume delta available from
    the data source. Labeled "vwap" for trader-familiar terminology,
    but this is stated plainly here and in the module docstring.
    """
    from datetime import datetime
    out = {}
    for sym in pilot.bus.get("symbols", []):
        chain = pilot.bus.get(f"chain:{sym}") or {}
        spot = chain.get("spot")
        spot_chg = chain.get("chg")
        spot_chg_pct = chain.get("chg_pct")
        # broker_adapter.py computes prev_close internally to derive
        # chg (ltp - prev_close) but never stores prev_close itself in
        # the chain response — derived here instead of touching that
        # tested code path.
        prev_close = (round(spot - spot_chg, 2)
                      if spot is not None and spot_chg is not None else None)
        hist = pilot.bus.get(f"spot_hist:{sym}", []) or []
        today = agents.now_ist().strftime("%Y-%m-%d")
        today_spots = [v for ts, v in hist
                      if datetime.fromtimestamp(ts, agents.IST).strftime("%Y-%m-%d") == today]
        spot_open = today_spots[0] if today_spots else spot
        spot_high = max(today_spots) if today_spots else spot
        spot_low = min(today_spots) if today_spots else spot
        spot_vwap = round(sum(today_spots) / len(today_spots), 2) if today_spots else spot

        future_ohlc = pilot.bus.get(f"future_ohlc:{sym}") or {}
        future_ltp = future_ohlc.get("close")
        future_open = future_ohlc.get("open")
        future_chg = (round(future_ltp - future_open, 2)
                      if future_ltp is not None and future_open else None)
        future_chg_pct = (round(future_chg / future_open * 100, 2)
                          if future_chg is not None and future_open else None)

        out[sym] = {
            "spot": {"ltp": spot, "chg": spot_chg, "chg_pct": spot_chg_pct,
                    "open": spot_open, "high": spot_high, "low": spot_low,
                    "vwap": spot_vwap, "prev_close": prev_close},
            "futures": {"ltp": future_ltp, "chg": future_chg,
                       "chg_pct": future_chg_pct,
                       "open": future_ohlc.get("open"),
                       "high": future_ohlc.get("high"),
                       "low": future_ohlc.get("low"),
                       "vwap": future_ohlc.get("vwap")},
            "bias": pilot.bus.get(f"bias:{sym}"),
            "levels": pilot.bus.get(f"levels:{sym}"),
            "ts": pilot.bus.get("ticker", {}).get(sym, {}).get("ts"),
        }
    return {"symbols": out, "as_of": time.time()}


@app.get("/api/macro/digest")
def api_macro_digest():
    """Genuine SUMMARY digest of the global-markets/macro picture —
    NOT new data plumbing, this reads the same macro_market_data and
    macro_events bus keys /api/macro already exposes, just compressed
    into a compact "what matters right now" view instead of a raw
    event log/table. Roadmap item: DJI/Nasdaq/Crude/Gold/Silver/macro-
    event summary view."""
    market = pilot.bus.get("macro_market_data", {}) or {}
    events = pilot.bus.get("macro_events", []) or []
    now = time.time()
    # Major events from the last 24h get a headline slot; everything
    # else is just counted by category so the digest stays compact.
    recent_major = [e for e in events if e.get("major")
                    and (now - e.get("ts", now)) < 86400]
    recent_major.sort(key=lambda e: e.get("ts", 0), reverse=True)
    by_category = {}
    for e in events:
        if (now - e.get("ts", now)) < 86400:
            cat = e.get("type", "Other")
            by_category[cat] = by_category.get(cat, 0) + 1
    return {
        "as_of": now,
        # Global risk-on/risk-off read, added 2026-07-24 in response to
        # "Global Market Snapshot, are just a number, it can be used" —
        # this is the same classification MTFConfluenceAgent now reads
        # as a supportive confidence input; shown here too so it's
        # visible, not just used internally.
        "global_risk_sentiment": pilot.bus.get("global_risk_sentiment"),
        "global_risk_sentiment_detail": pilot.bus.get("global_risk_sentiment_detail"),
        # Bug found 2026-07-24: macro_market_data's real keys are
        # UPPERCASE ("DJI", "NASDAQ", etc. — confirmed directly from
        # news_macro_agent.py's fetch symbol list) but this endpoint
        # checked lowercase keys, so `k in market` was always False —
        # indices/commodities_fx were always empty dicts regardless of
        # how much real data macro_market_data actually had. This is
        # exactly the "no summary output" bug reported live: the
        # underlying data was there (confirmed in the activity log —
        # yfinance fallback succeeding for every symbol), the digest
        # just never looked at the right key casing to find it.
        "indices": {k: market.get(k) for k in
                   ("DJI", "NASDAQ", "SPX", "RUSSELL2000", "NIKKEI")
                   if k in market},
        "commodities_fx": {k: market.get(k) for k in
                          ("CRUDE", "GOLD", "SILVER", "USDINR") if k in market},
        "headline_events_24h": [
            {"type": e.get("type"), "note": e.get("note"),
             "impact": e.get("impact"), "time": e.get("time")}
            for e in recent_major[:8]
        ],
        "event_counts_24h": by_category,
    }


@app.get("/api/news/tracker")
def api_news_tracker(category: str = "", region: str = "",
                     valid_only: bool = False, limit: int = 200):
    """Unified, deduplicated news tracker — every item classified by
    source, category, market impact (bullish/bearish/neutral), impact
    window (1m/5m/15m estimate), action, and validity. Fed by BOTH
    NewsAgent (RSS) and NewsMacroAgent (NewsAPI supplementary), shared-
    deduplicated so the same story never appears twice regardless of
    which agent first surfaced it."""
    import news_engine as ne
    events = ne.read_tracked_events(limit=limit)
    if category:
        events = [e for e in events if e.get("category") == category]
    if region:
        events = [e for e in events if e.get("region") == region]
    if valid_only:
        events = [e for e in events if e.get("valid")]
    return {"events": events, "categories": sorted(ne.CATEGORY_KEYWORDS.keys()) + ["other"]}


@app.get("/api/news/feeds")
def api_news_feeds_list():
    import news_engine as ne
    return {"feeds": ne.load_feeds()}


class NewsFeedIn(BaseModel):
    name: str
    url: str
    category: str = "market"
    region: str = "india"
    id: str | None = None


@app.post("/api/news/feeds")
def api_news_feeds_add(body: NewsFeedIn):
    import news_engine as ne
    try:
        feed_id = ne.add_feed(body.name, body.url, body.category,
                              body.region, feed_id=body.id)
        return {"ok": True, "id": feed_id, "feeds": ne.load_feeds()}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/news/feeds/{feed_id}")
def api_news_feeds_delete(feed_id: str):
    import news_engine as ne
    try:
        ne.delete_feed(feed_id)
        return {"ok": True, "feeds": ne.load_feeds()}
    except ValueError as e:
        raise HTTPException(404, str(e))


class NewsFeedTestIn(BaseModel):
    url: str


@app.post("/api/news/feeds/test")
def api_news_feeds_test(body: NewsFeedTestIn):
    import news_engine as ne
    return ne.test_feed(body.url)


@app.get("/api/regression/run")
def api_regression_run(capital: int = 1000000):
    import regression
    return regression.run_requested_battery(capital=capital)


@app.get("/api/backtest/status")
def api_backtest_status():
    import backtester, history, os, json as _j
    p = os.path.expanduser("~/.ltp-monitor/backtests.json")
    ran_at = None
    if os.path.exists(p):
        try:
            ran_at = _j.load(open(p)).get("at")
        except Exception:
            pass
    return {"coverage": history.coverage(),
            "results": pilot.bus.get("bt_results") or _load_bt_results(),
            "results_at": ran_at,
            "versions": backtester.load_versions(),
            "agent_running": pilot.running,
            "backtest_capital": config.load().get("backtest_capital", 200000),
            "margin_per_lot_spread": config.load().get("margin_per_lot_spread", 85000)}


@app.get("/api/backtest/sync-log")
def api_backtest_sync_log(days: int = 14):
    """Was write-only until 2026-07-22 — every daily archive attempt has
    always logged its outcome (probe failed / market closed / N legs
    failed) here, there was just no way to read it back."""
    import history
    return {"entries": history.recent_sync_log(days)}


def _load_bt_results():
    import os, json as _j
    p = os.path.expanduser("~/.ltp-monitor/backtests.json")
    return _j.load(open(p))["results"] if os.path.exists(p) else {}


@app.post("/api/backtest/run")
def api_backtest_run(sync: bool = False):
    """Queue a manual backtest run (executes in the agent thread)."""
    if not pilot.running:
        return {"error": "Start the agents first."}
    pilot.bus.set("bt_manual_job", {"sync": sync})
    return {"ok": True, "note": "queued — watch the backtest agent box"}


@app.post("/api/backtest/backfill")
def api_backtest_backfill(symbol: str = "NIFTY", years: int = 2):
    """One-time deep index backfill (runs inline; can take minutes)."""
    import history
    d = dhan_client()
    if not d:
        return {"error": "broker not configured"}
    lines = []
    n = history.sync_index_history(d, symbol.upper(), years,
                                   log=lambda m: lines.append(m))
    return {"candles": n, "log": lines[-10:]}


class RollbackIn(BaseModel):
    name: str
    symbol: str
    version: int


@app.get("/api/backtest/docs")
def api_backtest_docs():
    import strategy_docs
    return strategy_docs.DOCS


class SymbolToggleIn(BaseModel):
    name: str
    symbol: str
    disabled: bool


@app.post("/api/backtest/toggle_symbol")
def api_backtest_toggle_symbol(body: SymbolToggleIn):
    """Manual kill-switch: force a (strategy, symbol) off live trading
    regardless of backtest profitability, or clear that override."""
    import backtester
    v = backtester.load_versions()
    entry = backtester._symbol_entry(v, body.name, body.symbol)
    entry["manually_disabled"] = body.disabled
    if body.disabled:
        entry["live_enabled"] = False
    else:
        # re-check profitability of the currently active version
        active = next((x for x in entry["versions"] if x["v"] == entry["active"]), None)
        r = (active or {}).get("results") or {}
        cfg = config.load()
        entry["live_enabled"] = bool(
            r.get("trades", 0) >= cfg.get("pa_min_trades_for_confidence", 15)
            and r.get("net_pnl", -1) > 0)
    backtester.save_versions(v)
    return {"manually_disabled": entry["manually_disabled"],
            "live_enabled": entry["live_enabled"]}


@app.post("/api/backtest/rollback")
def api_backtest_rollback(body: RollbackIn):
    """Manual activation always allowed (an informed human overriding the
    auto-gate is different from the agent silently deploying a loser)."""
    import backtester
    v = backtester.load_versions()
    entry = backtester._symbol_entry(v, body.name, body.symbol)
    if not any(x["v"] == body.version for x in entry["versions"]):
        return {"error": "unknown version"}
    entry["active"] = body.version
    entry["manually_disabled"] = False
    for x in entry["versions"]:
        if x["v"] == body.version:
            x.setdefault("reason", "")
            if "manually activated" not in x["reason"]:
                x["reason"] += " | manually activated"
            r = x.get("results") or {}
            entry["live_enabled"] = bool(r.get("net_pnl", -1) > 0)
    backtester.save_versions(v)
    return {"active": body.version, "live_enabled": entry["live_enabled"]}


class KotakLoginIn(BaseModel):
    access_token: str | None = None
    mobile: str | None = None
    ucc: str | None = None
    totp: str
    mpin: str


@app.post("/api/kotak/login")
def api_kotak_login(body: KotakLoginIn):
    """Does today's TOTP+MPIN login in-app instead of the terminal
    script — same 2-step flow as kotak_login.py (tradeApiLogin then
    tradeApiValidate), saving the session into config the same way.
    TOTP/MPIN are used only for this exchange and never stored —
    only the resulting session token/sid/baseUrl are persisted,
    identical to what the terminal script has always done."""
    import urllib.request
    cfg = config.load()
    tok = body.access_token or cfg.get("kotak_access_token")
    mob = body.mobile or cfg.get("kotak_mobile")
    ucc = body.ucc or cfg.get("kotak_ucc")
    if not tok or not mob or not ucc:
        return {"error": "Access Token, mobile, and UCC are required "
                         "(first time only — remembered after that)"}

    def post(url, headers, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", str(e))
            except Exception:
                msg = str(e)
            raise RuntimeError(msg)

    h = {"Authorization": tok, "neo-fin-key": "neotradeapi",
        "Content-Type": "application/json"}
    try:
        resp1 = post("https://mis.kotaksecurities.com/login/1.0/tradeApiLogin",
                     h, {"mobileNumber": mob, "ucc": ucc, "totp": body.totp})
        d = resp1["data"]
    except Exception as e:
        return {"error": f"Step 1 (TOTP) failed: {e}"}

    h2 = dict(h, Auth=d["token"], sid=d["sid"])
    try:
        resp2 = post("https://mis.kotaksecurities.com/login/1.0/tradeApiValidate",
                     h2, {"mpin": body.mpin})
        d2 = resp2["data"]
    except Exception as e:
        return {"error": f"Step 2 (MPIN) failed: {e}"}

    config.save({"kotak_access_token": tok, "kotak_mobile": mob,
                "kotak_ucc": ucc, "kotak_session_token": d2["token"],
                "kotak_sid": d2["sid"], "kotak_base_url": d2["baseUrl"]})
    return {"ok": True, "base_url": d2["baseUrl"],
           "note": "Session saved. Select Kotak Neo as the active "
                   "broker if not already selected."}


@app.post("/api/reset_halt")
def api_reset_halt():
    """Reset the risk agent's consecutive-loss halt (user acknowledgement)."""
    if not pilot.running:
        return {"error": "agents not running"}
    risk = next((a for a in pilot.agents if a.name == "risk"), None)
    if risk:
        risk.halted = False
        risk.consecutive_losses = 0
        pilot.bus.log("risk", "halt reset by user")
        return {"ok": True}
    return {"error": "risk agent not found"}


@app.get("/api/trades_csv")
def api_trades_csv():
    """Export all trade history as CSV."""
    from fastapi.responses import Response
    import csv, io
    closed = pilot.bus.get("closed_trades", [])
    if not closed:
        return Response(content="", media_type="text/csv")
    fields = ["closed_at", "symbol", "leg", "strike", "qty", "entry",
              "ltp", "stoploss", "target1", "target2", "pnl", "reason",
              "paper", "opened"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for t in closed:
        w.writerow(t)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="ltp-trades.csv"'})


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "dashboard.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    cfg = config.load()
    print("\n  LTP Option Chain Monitor  ->  http://127.0.0.1:8000")
    if DhanClient.available():
        print("  ✓ Dhan credentials found — live feed enabled")
    else:
        print("  ! Add Dhan credentials in the dashboard Settings (gear icon)")
    print(f"  mode: {'PAPER (simulated orders)' if cfg['paper_mode'] else '⚠ LIVE ORDERS'}\n")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if host != "127.0.0.1":
        print("  ⚠ serving on the network — anyone who can reach this port "
              "can trade with your keys; firewall it to trusted IPs only")
    uvicorn.run(app, host=host, port=port)
