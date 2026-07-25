"""
LTP Option-Chain Monitor + Autopilot -- run with:  python app.py
Then open  http://127.0.0.1:8000
Credentials are managed in the dashboard's Settings panel (gear icon).
"""

import os
import time
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from nse_client import NSEClient, SensexClient
from analyzer import analyze, deep_ai_analysis, ai_signal, ai_visual, ai_deep_dive
from broker_adapter import DhanClient, DhanOrders
from agents import Orchestrator, compute_momentum

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


def get_chain(symbol: str) -> dict:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"Unknown symbol {symbol}")
    d = dhan_client()
    if d is not None:
        return d.option_chain(symbol)
    if symbol == "SENSEX":
        return bse.option_chain()
    return nse.option_chain(symbol)


pilot = Orchestrator(get_chain, orders_factory)
pilot.ctx["dhan_client"] = dhan_client


# ------------------------------------------------------------ data & AI
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
    anthropic_api_key: str | None = None
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
    auto_strategies: list[str] | None = None
    max_concurrent_spreads: int | None = None
    spread_reentry_cooldown_min: int | None = None
    daily_loss_limit: int | None = None
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


@app.get("/api/settings")
def get_settings():
    return config.public_view(config.load())


@app.post("/api/settings")
def set_settings(s: SettingsIn):
    updates = {k: v for k, v in s.model_dump().items() if v is not None}
    # empty string means "don't change" for secrets
    for k in ("dhan_client_id", "dhan_access_token", "anthropic_api_key", "zerodha_api_key", "zerodha_access_token", "kotak_consumer_key", "kotak_access_token", "kotak_sid", "kotak_auth_token"):
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
        out[sym] = {"spot": spot, "prev_close": pc, "ts": ts,
                    "change": round(spot - pc, 2) if spot and pc else None,
                    "pct": round((spot - pc) / pc * 100, 2) if spot and pc else None,
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
    pos = pilot.bus.get("position")   # legacy single-position mirror
    realized = sum(t.get("pnl", 0) for t in closed)
    total_fees = sum(t.get("fees", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    unrealized = sum(p.get("pnl", 0) for p in positions.values())

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
        "positions": positions,       # all concurrently open trades
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
    return {"symbol": sym,
            "regime": (regime or {}).get("regime", "unknown"),
            "paper_mode": config.load()["paper_mode"],
            "strategies": out,
            "open_spreads": pilot.bus.get("spreads", {})}


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
