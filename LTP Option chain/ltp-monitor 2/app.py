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
    """(Re)build the Dhan client lazily so Settings changes apply live."""
    global _dhan
    if not DhanClient.available():
        _dhan = None
        return None
    if _dhan is None:
        _dhan = DhanClient()
    return _dhan


def reset_dhan():
    global _dhan
    _dhan = None


def orders_factory():
    c = dhan_client()
    return DhanOrders(c) if c else None


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
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    anthropic_api_key: str | None = None
    theme: str | None = None
    paper_mode: bool | None = None
    auto_execute: bool | None = None
    min_confidence: int | None = None
    max_trades_per_day: int | None = None
    lots_per_trade: int | None = None
    daily_loss_limit: int | None = None
    cooldown_after_loss_min: int | None = None
    stop_after_consecutive_losses: int | None = None
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
    for k in ("dhan_client_id", "dhan_access_token", "anthropic_api_key"):
        if updates.get(k) == "":
            updates.pop(k)
    cfg = config.save(updates)
    if any(k.startswith("dhan") for k in updates):
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
def api_exit():
    return pilot.exit_position("manual exit from dashboard")


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
    pos = pilot.bus.get("position")
    realized = sum(t.get("pnl", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    unrealized = pos.get("pnl", 0) if pos else 0
    return {
        "open": pos,
        "closed": closed[::-1],
        "stats": {
            "count": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
        },
    }


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
