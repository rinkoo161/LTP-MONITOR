"""
LTP Option-Chain Monitor + Autopilot -- run with:  python app.py
Then open  http://127.0.0.1:8000
Credentials are managed in the dashboard's Settings panel (gear icon).
"""

import os
import store
import time
import traceback

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
APP_VERSION = "v59.0"   # maintained per explicit request; last delivered was v49

app = FastAPI(title="LTP Option Chain Monitor")

# ====================================================================
# Authentication (v58.74). See auth.py for the account/TOTP mechanics.
#
# Enforced by ONE middleware rather than a decorator on each route.
# There are 65+ endpoints and every one of them can move money or leak
# a credential; a per-route decorator protects exactly the routes
# somebody remembered, and this session has already produced three bugs
# of that shape (the S10 call site, the futures risk cap, the candle
# write gate). The rule belongs at the boundary they all cross.
#
# Anything not on the allowlist requires a session, so a NEW endpoint is
# protected by default rather than by being noticed.
# ====================================================================
import auth

SESSION_COOKIE = "ltp_session"
_AUTH_FREE = ("/login", "/setup", "/favicon.ico", "/api/version",
              "/api/auth/login", "/api/auth/setup", "/api/auth/status")


def _auth_free(path):
    return path in _AUTH_FREE or path.startswith("/static/")


def current_user(request):
    return auth.session(request.cookies.get(SESSION_COOKIE))


@app.middleware("http")
async def _require_login(request, call_next):
    cfg = config.load()
    if not cfg.get("auth_enabled", False) or _auth_free(request.url.path):
        return await call_next(request)
    sess = current_user(request)
    if not sess:
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401,
                                content={"error": "not authenticated",
                                         "login": "/login"})
        return RedirectResponse("/login", status_code=302)
    request.state.user = sess
    resp = await call_next(request)
    # Attribute every state-changing call. "Same access, separate
    # accounts" only means anything if the record says who acted.
    if request.method in ("POST", "PUT", "DELETE"):
        auth.audit("api", sess["user"], {"method": request.method,
                                         "path": request.url.path})
    return resp


def _set_session_cookie(resp, token, cfg):
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    path="/", secure=bool(cfg.get("auth_cookie_secure", False)),
                    max_age=int(float(cfg.get("auth_session_hours", 12)) * 3600))
    return resp


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""
    code: str = ""


class SetupIn(BaseModel):
    username: str = ""
    password: str = ""


class UserIn(BaseModel):
    username: str = ""
    password: str = ""
    role: str = "user"


@app.get("/api/auth/status")
def auth_status(request: Request):
    """Unauthenticated on purpose — the login page needs to know whether
    auth is on and whether an admin exists yet."""
    cfg = config.load()
    sess = current_user(request)
    return {"enabled": bool(cfg.get("auth_enabled", False)),
            "needs_setup": auth.user_count() == 0,
            "require_mfa": bool(cfg.get("auth_require_mfa", True)),
            "user": sess["user"] if sess else None,
            "role": sess["role"] if sess else None}


@app.post("/api/auth/setup")
def auth_setup(body: SetupIn):
    """Create the FIRST admin. Refuses once any account exists, so it
    cannot be used to mint a second admin later."""
    if auth.user_count() > 0:
        raise HTTPException(403, "setup already completed — sign in instead")
    try:
        u = auth.create_user(body.username, body.password, "admin")
    except ValueError as e:
        raise HTTPException(400, str(e))
    secret, uri = auth.begin_mfa_enrollment(u)
    return {"ok": True, "username": u, "secret": secret, "uri": uri,
            "qr_svg": auth.qr_svg(uri),
            "next": "scan or type the secret, then POST /api/auth/mfa/confirm"}


@app.post("/api/auth/login")
def auth_login(body: LoginIn):
    cfg = config.load()
    token, err = auth.authenticate(body.username, body.password, body.code, cfg)
    if err:
        raise HTTPException(401, err)
    return _set_session_cookie(JSONResponse({"ok": True}), token, cfg)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.logout(request.cookies.get(SESSION_COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r


@app.get("/api/auth/users")
def auth_users(request: Request):
    _require_admin(request)
    return {"users": auth.list_users()}


@app.post("/api/auth/users")
def auth_create_user(body: UserIn, request: Request):
    _require_admin(request)
    try:
        u = auth.create_user(body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "username": u}


@app.delete("/api/auth/users/{username}")
def auth_delete_user(username: str, request: Request):
    _require_admin(request)
    try:
        auth.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/auth/users/{username}/password")
def auth_reset_password(username: str, body: SetupIn, request: Request):
    """Admin resets another account's password. The other half of
    "forgot password"; the host-side half is manage_users.py, which is
    the only thing that helps a locked-out sole admin."""
    _require_admin(request)
    try:
        auth.set_password(username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "username": username}


@app.post("/api/auth/users/{username}/mfa-reset")
def auth_reset_mfa(username: str, request: Request):
    """Admin clears MFA for an account — the lost-phone case."""
    _require_admin(request)
    auth.disable_mfa(username)
    return {"ok": True, "username": username,
            "next": "the user signs in and the page offers pairing"}


@app.post("/api/auth/mfa/enroll")
def auth_mfa_enroll(request: Request):
    sess = current_user(request)
    if not sess:
        raise HTTPException(401, "not authenticated")
    secret, uri = auth.begin_mfa_enrollment(sess["user"])
    return {"secret": secret, "uri": uri, "qr_svg": auth.qr_svg(uri)}


@app.post("/api/auth/mfa/restart")
def auth_mfa_restart(body: LoginIn):
    """Re-issue an enrollment QR for an account whose MFA never completed.
    Password-authenticated; refuses once MFA is active. See
    auth.restart_enrollment for why this exists."""
    try:
        secret, uri = auth.restart_enrollment(body.username, body.password)
    except ValueError as e:
        raise HTTPException(401 if "invalid" in str(e) else 400, str(e))
    return {"ok": True, "username": (body.username or "").strip().lower(),
            "secret": secret, "uri": uri, "qr_svg": auth.qr_svg(uri)}


@app.post("/api/auth/mfa/confirm")
def auth_mfa_confirm(body: LoginIn, request: Request):
    """Usable during first-run setup (no session yet) or from a session."""
    sess = current_user(request)
    user = sess["user"] if sess else (body.username or "").strip().lower()
    if not user:
        raise HTTPException(400, "username required")
    if not auth.confirm_mfa(user, body.code):
        raise HTTPException(400, "that code did not verify — check the clock "
                                 "on the phone and try the next one")
    return {"ok": True}


def _require_admin(request):
    """Account management is the one admin-only surface: both roles have
    the same operational access by explicit choice, so this is what the
    role actually means."""
    sess = current_user(request)
    if not config.load().get("auth_enabled", False):
        return None                      # auth off: local-only, as before
    if not sess:
        raise HTTPException(401, "not authenticated")
    if sess.get("role") != "admin":
        auth.audit("admin.denied", sess["user"],
                   {"path": str(request.url.path)}, ok=False)
        raise HTTPException(403, "admin role required")
    return sess


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(BASE, "static", "login.html"),
                        headers={"Cache-Control": "no-store"})


@app.get("/setup")
def setup_page():
    return FileResponse(os.path.join(BASE, "static", "login.html"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}

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
    """Drop EVERY cached broker client so the next call rebuilds with the
    credentials currently in Settings.

    2026-07-31 — this cleared only `_dhan`. `_dhan_fallback` — the
    dedicated Dhan client SENSEX falls back to when the active broker
    cannot serve it — was left alone, and DhanClient.__init__ snapshots
    client_id/token into its request headers at construction. So after
    pasting a fresh token the fallback kept sending the OLD one: every
    SENSEX prev_close_for() 401'd, each 401 re-armed the 30-minute AUTH
    backoff, and the "Dhan token expired" alert re-fired seconds after
    the operator had already fixed it.

    From outside that is indistinguishable from the setting rolling
    back, which is exactly how it was reported. The stored token was
    correct the whole time; a cached object was not.

    Any future cached client belongs in this function, not beside it.
    """
    global _dhan, _dhan_fallback
    _dhan = None
    _dhan_fallback = None


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


@app.get("/api/technical/{symbol}")
def api_technical(symbol: str):
    """Feature #7 (Technical Analysis Engine) — increment 1 (VWAP
    Engine only). Same "not yet computed" graceful response pattern as
    /api/institutional/{symbol}."""
    sym = symbol.upper()
    result = pilot.bus.get(f"technical:{sym}")
    if result is None:
        return JSONResponse(status_code=200, content={
            "symbol": sym, "available": False,
            "reason": "not yet computed — needs at least one TechnicalAgent "
                      "cycle (~60s) with live chain data for this symbol"})
    return {"symbol": sym, "available": True, **result}


@app.get("/api/institutional/{symbol}")
def api_institutional(symbol: str):
    """Feature #5 (Institutional Activity Engine) — per the spec's own
    "Only expose processed intelligence to Market Bias/Risk/Trade
    Recommendation/AI Narrative Engines" instruction, this is the one
    exposure point. Reads institutional:{symbol}, computed every
    TechnicalAgent cycle (~60s) alongside analysis:{symbol} — returns
    a clear "not computed yet" response rather than a bare 404/500 if
    the agent loop hasn't run for this symbol yet (e.g. right after
    startup, or market closed and this symbol was never analyzed)."""
    sym = symbol.upper()
    result = pilot.bus.get(f"institutional:{sym}")
    if result is None:
        return JSONResponse(status_code=200, content={
            "symbol": sym, "available": False,
            "reason": "not yet computed — needs at least one TechnicalAgent "
                      "cycle (~60s) with live chain data for this symbol"})
    return {"symbol": sym, "available": True, **result}


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
        # v58.55 — this is the endpoint the index panels read, and it was
        # the one deferred in v58.54 as "a separate decision". That
        # deferral was wrong: /api/ai_visual degraded gracefully while
        # THIS path still raised, so the panels got a 502 and showed
        # "start agents". Deferring the endpoint that was causing the
        # reported symptom is not scoping, it is missing the point.
        #
        # Same throttle as the display helper (60s per symbol) and the
        # same shared rate limiter, so a broken or expired credential
        # cannot be retried on every poll.
        import rate_limit as _rl
        if _rl.is_auth_failure("quote"):
            return {"unavailable": True, "auth_expired": True, "symbol": sym,
                    "reason": "broker token expired - paste a fresh Access "
                              "Token in Settings; no data can load until then"}
        _hit = _disp_cache.get(sym)
        if _rl.is_limited("quote") and _hit:
            return _hit[1]
        if _hit and (time.time() - _hit[0]) < _DISP_MIN_INTERVAL:
            return _hit[1]
        from agents import compute_momentum
        mom = compute_momentum(pilot.bus.get(f"spot_hist:{sym}", []))
        try:
            fresh = analyze(get_chain(sym), momentum=mom)
        except Exception as fe:
            _rl.note_failure(fe, "quote")
            if _hit:
                return _hit[1]          # stale beats blank
            # A structured 200 rather than a 502: the panel can render
            # "unavailable, here is why" instead of failing to parse an
            # error page and showing "start agents".
            return {"unavailable": True, "symbol": sym,
                    "reason": f"{type(fe).__name__}: {fe}"}
        _disp_cache[sym] = (time.time(), fresh)
        return fresh
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
        # AI Decision Engine + AI Probability Engine preview (Feature
        # #8) — risk.evaluate() is read-only (no order placement, no
        # persistent side effects beyond an idempotent news-score
        # cache write), so it's safe to run here purely so the
        # confidence adjustment/probability estimate are visible to
        # the person BEFORE they decide whether to confirm a trade,
        # not only computed silently at actual approval time. Wrapped
        # so a problem here can never break the signal preview itself.
        if sig.get("signal") in ("BUY_CE", "BUY_PE"):
            try:
                risk = next((a for a in pilot.agents if a.name == "risk"), None)
                if risk:
                    job = {"symbol": sym, "signal": sig, "analysis": analysis}
                    _ok, checks = risk.evaluate(job)
                    sig["risk_preview_checks"] = checks
            except Exception:
                pass
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
    critique) — replaces the old plain-text narrative.

    2026-07-28 — enriched per direct request: this previously only saw
    option-chain-derived fields (spot/PCR/max pain/support-resistance)
    plus news/social/macro. Added three more existing, already-
    computed inputs so the analysis genuinely reflects what the other
    Dashboard panels are showing, not a narrower slice of it:
      - technical: from technical:{sym} (the same bus key the
        Technical Analysis Engine panel reads — bias, score,
        confidence, trend/momentum/volatility)
      - institutional: from institutional:{sym} (the same bus key the
        Institutional & Smart Money panel reads — score, label)
      - trading_behavior: aggregated directly from closed_trades (the
        same store P&L/Journal read) filtered to this symbol's most
        recent trades — recent win rate and exit-reason pattern, not
        a new tracking mechanism.
    """
    try:
        sym = symbol.upper()
        analysis, _warm = bus_analysis_or_warming(sym)
        if _warm:
            return _warm
        technical = pilot.bus.get(f"technical:{sym}") or {}
        institutional = pilot.bus.get(f"institutional:{sym}") or {}
        closed = pilot.bus.get("closed_trades", []) or []
        recent = [t for t in closed if t.get("symbol") == sym][-10:]
        wins = sum(1 for t in recent if t.get("pnl", 0) > 0)
        trading_behavior = {
            "recent_trades": len(recent),
            "recent_win_rate_pct": round(wins / len(recent) * 100, 1) if recent else None,
            "recent_exit_reasons": [str(t.get("reason", ""))[:50] for t in recent[-5:]],
        }
        context = {"news": pilot.bus.get("news"),
                   "social_mood": (pilot.bus.get("social") or {}).get("mood"),
                   "macro": (pilot.bus.get("macro") or {}).get("stance"),
                   "technical": {"bias": technical.get("technical_bias"),
                                "score": technical.get("technical_score"),
                                "confidence_pct": technical.get("confidence_pct")},
                   "institutional": {"score": institutional.get("score"),
                                     "label": institutional.get("label")},
                   "trading_behavior": trading_behavior}
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
    max_spread_capital_pct: float | None = None
    spread_ai_auto_exit_enabled: bool | None = None
    option_ai_auto_exit_enabled: bool | None = None
    futures_ai_auto_exit_enabled: bool | None = None
    spread_profit_target_pct: float | None = None
    dynamic_spread_targets_enabled: bool | None = None
    spread_target_low_iv_pct: float | None = None
    spread_target_normal_iv_pct: float | None = None
    spread_target_elevated_iv_pct: float | None = None
    spread_target_elevated_iv_stable_pct: float | None = None
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
    spread_stop_after_consecutive_losses: int | None = None
    regime_gate_enabled: bool | None = None
    require_tf_confluence: bool | None = None
    ai_enabled: bool | None = None
    ai_engine: str | None = None
    ollama_model: str | None = None
    ai_active_only: bool | None = None
    ai_min_interval: int | None = None
    ai_daily_call_cap: int | None = None
    news_block_minutes: int | None = None
    news_ai_classification_enabled: bool | None = None
    news_ai_classification_daily_cap: int | None = None
    news_realert_cooldown_minutes: int | None = None
    # 2026-07-26 (v54) — CRITICAL FIX. These 46 keys existed in
    # config.DEFAULTS (several since v51/v52) but were never declared
    # here. FastAPI/pydantic silently drops any field not declared on
    # the request model, so every one of these was discarded BEFORE
    # config.save() ever saw it — meaning the S7 and Futures Settings
    # subcards built in v51/v52 have never actually persisted anything
    # through the Save button, despite rendering correctly and reading
    # back correctly (the GET path always worked; only POST was
    # silently broken). This is one layer earlier than the "config.save
    # drops unregistered keys" trap fixed in v53 — that fix logs a
    # warning INSIDE config.save(), but these keys never reached it to
    # be warned about. Found while wiring the new Strategies-page table's
    # auto-deploy toggles through this same endpoint. Declaring every
    # DEFAULTS key here that was missing, not just the ones needed right
    # now, so this exact failure mode can't recur for a sibling setting.
    strategy7_enabled: bool | None = None
    s7_ema_fast: int | None = None
    s7_ema_slow: int | None = None
    s7_mtf_confirm: int | None = None
    s7_require_structure: bool | None = None
    s7_require_ai_bias: bool | None = None
    s7_min_ai_bias: int | None = None
    s7_structural_stop_buffer_pct: float | None = None
    s7_rr_target: float | None = None
    s7_max_trades_per_day: int | None = None
    s7_auto_deploy: bool | None = None
    s7_markers_enabled: bool | None = None
    s7_show_rejected_markers: bool | None = None
    strategy8_enabled: bool | None = None
    s8_auto_deploy: bool | None = None
    s8_ending_diagonal_enabled: bool | None = None
    s8_hs_enabled: bool | None = None
    s8_failed_hs_enabled: bool | None = None
    s8_zigzag_deviation_pct: float | None = None
    s8_require_macd_divergence: bool | None = None
    s8_require_tide: bool | None = None
    s8_min_pattern_bars: int | None = None
    s8_shoulder_tol_pct: float | None = None
    s8_neckline_buffer_pct: float | None = None
    s8_stop_buffer_pct: float | None = None
    s8_rr_target: float | None = None
    s8_max_trades_per_day: int | None = None
    s8_markers_enabled: bool | None = None
    s8_require_tide_all_detectors: bool | None = None
    s8_use_shared_tide: bool | None = None
    ta_elliott_enabled: bool | None = None
    ta_auto_deploy: bool | None = None
    ta_min_confluence: int | None = None
    ta_require_tide: bool | None = None
    ta_bb_period: int | None = None
    ta_bb_stdev: float | None = None
    ta_bb_slope_eps: float | None = None
    ta_gmma_timeframe: str | None = None
    option_strike_policy: str | None = None
    signal_min_rr: float | None = None
    dynamic_exits_enabled: bool | None = None
    oi_composite_enabled: bool | None = None
    oi_composite_auto_deploy: bool | None = None
    oi_composite_risk_pct: float | None = None
    oi_composite_max_concurrent: int | None = None
    oi_composite_rr_target: float | None = None
    oi_composite_cost_per_leg: float | None = None
    oi_composite_cost_is_per_lot: int | None = None
    oi_composite_otm_strikes_checked: int | None = None
    oi_composite_spread_width_strikes: int | None = None
    oi_composite_require_churn_filter: int | None = None
    oi_composite_min_delta_for_long_leg: float | None = None
    oi_composite_condor_enabled: int | None = None
    oi_composite_max_trades_per_day: int | None = None
    ws_subscribe_chunk_size: int | None = None
    ws_subscribe_delay_ms: int | None = None
    ta_gmma_compression_pct: float | None = None
    ta_adx_dynamic_min: float | None = None
    ta_rsi_period: int | None = None
    ta_zigzag_deviation_pct: float | None = None
    ta_stop_buffer_pct: float | None = None
    ta_rr_target: float | None = None
    ta_max_trades_per_day: int | None = None
    ta_require_corrective_phase: bool | None = None
    ta_tide_use_15m: bool | None = None
    ta_calibration_logging: bool | None = None
    ta_calibration_retention_days: int | None = None
    rupee_profit_floor_enabled: bool | None = None
    rupee_profit_floor_arm_rupees: float | None = None
    rupee_profit_floor_keep_pct: float | None = None
    rupee_profit_floor_min_rupees: float | None = None
    rupee_profit_floor_as_stop: bool | None = None
    # v58.74 — auth. Registered here as well as in DEFAULTS: a key absent
    # from this model cannot reach config.save() from the Settings page
    # at all, which is how `auth_enabled` would have been unreachable
    # from the UI that is supposed to switch it on. Caught by
    # test_settings_model_sync, which exists for exactly this.
    # v59.0 §3.2 — registered here as well as in DEFAULTS, or config.save()
    # drops them silently and the cost model stays on its defaults forever.
    index_dividend_calendar: dict | None = None
    require_basis_agreement: bool | None = None
    s11_require_basis_agreement: bool | None = None
    s12_require_basis_agreement: bool | None = None
    s13_require_basis_agreement: bool | None = None
    s14_require_basis_agreement: bool | None = None
    futures_require_basis_agreement: bool | None = None

    fut_financing_rate_pct: float | None = None
    fut_dividend_yield_pct: float | None = None
    fut_residual_z_window: int | None = None
    fut_brokerage_per_order: float | None = None
    fut_stt_sell_pct: float | None = None
    fut_exchange_txn_pct: float | None = None
    fut_sebi_turnover_pct: float | None = None
    fut_stamp_duty_pct: float | None = None
    fut_gst_pct: float | None = None
    fut_slippage_points: float | None = None
    auth_enabled: bool | None = None
    auth_require_mfa: bool | None = None
    auth_session_hours: float | None = None
    auth_max_failed: int | None = None
    auth_lockout_minutes: float | None = None
    auth_cookie_secure: bool | None = None
    ai_exit_advisory_logging: bool | None = None
    ai_exit_advisory_danger_interval_sec: int | None = None
    ai_exit_advisory_min_interval_sec: int | None = None
    ai_exit_advisory_max_interval_sec: int | None = None
    ai_exit_advisory_move_trigger_pct: float | None = None
    ai_exit_advisory_giveback_trigger_pct: float | None = None
    futures_symbols: list | None = None
    futures_stop_mode: str | None = None
    futures_atr_period: int | None = None
    futures_atr_stop_mult: float | None = None
    futures_atr_target_mult: float | None = None
    futures_risk_per_trade_rupees: float | None = None
    futures_min_adx: float | None = None
    risk_budgets_enabled: bool | None = None
    budget_futures_daily_loss: float | None = None
    budget_spread_daily_loss: float | None = None
    budget_option_daily_loss: float | None = None
    rupee_profit_floor_arm_rupees_spread: float | None = None
    rupee_profit_floor_keep_pct_spread: float | None = None
    rupee_profit_floor_min_rupees_spread: float | None = None
    rupee_profit_floor_arm_rupees_futures: float | None = None
    rupee_profit_floor_keep_pct_futures: float | None = None
    rupee_profit_floor_min_rupees_futures: float | None = None
    rupee_profit_floor_arm_rupees_option: float | None = None
    rupee_profit_floor_keep_pct_option: float | None = None
    rupee_profit_floor_min_rupees_option: float | None = None
    futures_strategy_enabled: bool | None = None
    futures_auto_deploy: bool | None = None
    futures_require_oi_confirm: bool | None = None
    futures_min_regime_confidence: int | None = None
    futures_cooldown_min: int | None = None
    futures_max_trades_per_day: int | None = None
    futures_sl_pct: float | None = None
    futures_target_pct: float | None = None
    futures_trail_trigger_pct: float | None = None
    futures_trail_gap_pct: float | None = None
    futures_defense_enabled: bool | None = None
    futures_defense_zone_pct: float | None = None
    futures_defense_tighten_pct: float | None = None
    margin_per_lot_future: int | None = None
    futures_live_enabled: bool | None = None
    # v59.0 Phase D — shadow only, no orders in live or paper.
    option_risk_per_trade_rupees: float | None = None
    fhedge_shadow_enabled: bool | None = None
    fhedge_trigger_buffer_pct: float | None = None
    fhedge_max_lots: int | None = None
    fhedge_min_parent_lots: int | None = None
    chain_snapshot_retention_days: int | None = None
    # v59.0 item 18 — tiered chain retention. See
    # history.prune_chain_snapshots(); tier 2's interval is what decides
    # whether replay timestamps can still be matched to a premium.
    chain_tier1_days: int | None = None
    chain_tier2_days: int | None = None
    chain_tier2_interval_sec: int | None = None
    chain_snapshot_interval_sec: int | None = None
    chart_history_days_1m: int | None = None
    chart_history_days_5m: int | None = None
    chart_history_days_15m: int | None = None
    ai_signal_on_change_only: bool | None = None
    kotak_base_url: str | None = None
    kotak_mobile: str | None = None
    kotak_session_token: str | None = None
    kotak_ucc: str | None = None
    lot_sizes: dict[str, int] | None = None
    ollama_keep_alive: str | None = None
    ollama_num_ctx: int | None = None
    ollama_num_thread: int | None = None
    ollama_timeout: int | None = None
    pa_min_trades_for_confidence: int | None = None
    pa_retune_cooldown_days: int | None = None
    pa_tuning_improvement_threshold: float | None = None
    pa_tuning_max_attempts: int | None = None
    spread_ai_exit_confidence_threshold: int | None = None
    option_ai_exit_confidence_threshold: int | None = None
    futures_ai_exit_confidence_threshold: int | None = None
    spread_defense_enabled: bool | None = None
    spread_require_liquidity_confluence: bool | None = None
    spread_liquidity_proximity_pct: float | None = None
    spread_defense_tighten_pct: int | None = None
    spread_defense_zone_pct: int | None = None
    spread_loss_limit_multiple: float | None = None
    pa_enabled: list[str] | None = None
    ai_decision_engine_enabled: bool | None = None
    learning_feedback_enabled: bool | None = None


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
        # v58.56 — clearing the rate-limit cooldown is REQUIRED here, and
        # its absence was a bug I introduced in v58.53. That release gave
        # auth failures a 1800s backoff on the reasoning that an expired
        # token "cannot fix itself". True -- but pasting a new one IS the
        # fix, and nothing cleared the cooldown when it arrived. So the
        # system kept refusing quote calls for up to 30 minutes AFTER the
        # problem was solved, which is the opposite of the intent and
        # exactly the "not showing on start, appeared 5 minutes later"
        # symptom reported.
        #
        # A long backoff is only correct if the recovery path resets it.
        _reset_quote_rate_limit()
        # Drop cached "unavailable" panels so the next poll refetches with
        # the new credential instead of serving a stale failure.
        _disp_cache.clear()
        pilot.bus.log("app", "broker credentials updated — rate-limit "
                             "cooldown cleared, panels will refetch")
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
_prev_close_batch_tried = None   # date string once the shared batch fetch below has SUCCEEDED today
_quote_rate_limited_until = 0.0  # epoch; set on a 429 from the quote endpoint


def _quote_rate_limited():
    """True while the /marketfeed/quote endpoint is in a 429 cooldown.

    2026-07-29, from a live log at 00:51: the batch prev_close call hit
    429, and every one of the four symbols then fell through to its OWN
    per-symbol quote_batch() call — each of which also 429'd. Five
    requests where one had already been refused. A 429 does not mean
    "that batch was malformed, try them individually"; it means stop.
    The per-symbol path is a genuine safety net for a batch that came
    back missing a symbol, but it is exactly the wrong response to a
    rate limit, and it fires precisely when the endpoint is already
    under contention.

    Mirrors the cooldown convention broker_adapter already uses for the
    sibling LTP endpoint (60s on 429) rather than inventing a second
    mechanism.
    """
    # v58.49 — defers to the SHARED registry so a 429 seen by any
    # caller of this endpoint slows every caller, not just this one.
    # The module global is kept in sync for the existing tests that
    # read it directly.
    import rate_limit
    return rate_limit.is_limited("quote") or time.time() < _quote_rate_limited_until


def _reset_quote_rate_limit():
    """Clear the quote-endpoint cooldown. Exists so tests (and any
    future caller that stubs the broker) can reset this module-level
    state by name instead of poking the global directly — the leak that
    made test_authoritative_prev_close case 10 fail after this cooldown
    was introduced: it reset `_prev_close_batch_tried` but had no way to
    know a second piece of state now also gates the fetch."""
    global _quote_rate_limited_until
    import rate_limit
    rate_limit.reset("quote")
    globals()["_auth_alerted"] = False
    _quote_rate_limited_until = 0.0


def _note_quote_failure(e):
    """Record a quote-endpoint failure, with a longer cooldown for a
    429 than for a transient network error — the same asymmetry
    agents.py already applies to the option-chain fetch."""
    global _quote_rate_limited_until
    import rate_limit
    is_429, secs = rate_limit.note_failure(e, "quote")
    _quote_rate_limited_until = time.time() + secs
    return is_429


def _batch_fetch_prev_close(today):
    """2026-07-27 (part 4) — real root cause CONFIRMED from a live log:
    NIFTY and FINNIFTY's authoritative quote path succeeded outright
    (previous_close_price IS the right field — the earlier fix's field
    guess was correct), but BANKNIFTY and SENSEX both hit a 429 Too
    Many Requests on the SAME quote_batch() call, in the SAME log
    window futures REST polling was ALSO hitting 429s on the identical
    endpoint. The bug wasn't the field name or the approach — it was
    calling quote_batch() FOUR separate times in quick succession (one
    per symbol) against an endpoint already under real contention.

    Fixed by batching all 4 index symbols into ONE quote_batch() call
    instead of four — removes the self-inflicted rate-limit pressure
    entirely rather than trying to out-race it with retries or delays.
    Runs at most once per day (module-level `_prev_close_batch_tried`
    guard) since prev_close is itself only needed once per symbol per
    day. Uses the dedicated Dhan fallback client specifically (not
    whichever broker is active) since this batch always includes
    SENSEX, which may not be served by a non-Dhan active broker at
    all — same reasoning already established for SENSEX specifically,
    just applied to the whole batch since it's one shared call now.
    """
    global _prev_close_batch_tried
    if _prev_close_batch_tried == today:
        return
    # 2026-07-29 — `_prev_close_batch_tried = today` used to be set HERE,
    # before the call. A single transient 429 (observed at 00:51, well
    # outside market hours) therefore disabled the authoritative
    # prev_close path for the WHOLE DAY: every symbol silently fell back
    # to candle reconstruction, which is precisely the small
    # displayed-change drift this whole feature was built to eliminate.
    # The flag is now set only on SUCCESS; a failure schedules a retry
    # instead of permanently giving up.
    if _quote_rate_limited():
        return
    fb = _dhan_fallback_client()
    if fb is None or not hasattr(fb, "quote_batch"):
        pilot.bus.log("app", "prev_close_for — batch fetch skipped, no Dhan "
                     "fallback client with quote_batch available")
        return
    from broker_adapter import UNDERLYINGS
    syms = [s for s in SYMBOLS if s in UNDERLYINGS]
    sec_ids = [UNDERLYINGS[s] for s in syms]
    if not syms:
        return
    try:
        data = fb.quote_batch({"IDX_I": sec_ids})
    except Exception as e:
        is_429 = _note_quote_failure(e)
        import rate_limit as _rl
        if _rl.is_auth_failure("quote"):
            # An expired token cannot resolve itself. Say so ONCE, loudly,
            # with the fix — rather than repeating a 401 every 30s all
            # night, which is what the 2026-07-30 01:55 log shows.
            if not globals().get("_auth_alerted"):
                globals()["_auth_alerted"] = True
                pilot.bus.log("app", "Dhan token EXPIRED (401) — paste a fresh "
                                     "Access Token in Settings. Quote calls are "
                                     "paused for 30min; nothing will recover "
                                     "until the token is replaced.")
                try:
                    pilot.bus.alert("high", "app", "", "Dhan token expired")
                except Exception:
                    pass
            return
        pilot.bus.log("app", f"prev_close_for — batch quote_batch() for all "
                     f"{len(syms)} symbols raised {type(e).__name__}: {e}"
                     + (f" — quote endpoint rate-limited, backing off "
                        f"{int(_quote_rate_limited_until - time.time())}s; "
                        f"per-symbol retries SUPPRESSED (they would make "
                        f"{len(syms)} more calls into the same limit) and "
                        f"the batch will retry after the cooldown"
                        if is_429 else " — will retry shortly"))
        return
    _prev_close_batch_tried = today
    idx_data = data.get("IDX_I") or {}
    for sym, sec_id in zip(syms, sec_ids):
        q = idx_data.get(str(sec_id)) or {}
        pc = q.get("previous_close_price") or (q.get("ohlc") or {}).get("close")
        last_price = q.get("last_price")
        # 2026-07-27 (part 5) — real regression found from live testing:
        # NIFTY and FINNIFTY both started showing 0 (0%) change after
        # this fix shipped. Root cause: the quote was tested AFTER
        # market close (per the person's own screenshot — "MARKET
        # CLOSED" badge) — and it now looks like Dhan's
        # `previous_close_price` field reflects the JUST-CONCLUDED
        # session's own close once the market has closed for the day,
        # not genuinely yesterday's close as assumed. Since `last_price`
        # (also in this same response, no extra call needed) equals
        # today's own last traded price when the market is shut, a
        # previous_close that EXACTLY matches last_price is a strong
        # signal the field is reporting today's close mislabeled as
        # "previous" — a real prior-day close essentially never matches
        # the current price exactly. Distrust it in that case and fall
        # through to candle reconstruction instead, which is unaffected
        # by this after-hours field-semantics ambiguity.
        if pc and last_price and abs(float(pc) - float(last_price)) < 0.01:
            pilot.bus.log("app", f"{sym}: prev_close_for — batched call's "
                         f"previous_close_price ({pc}) exactly matches "
                         f"last_price ({last_price}) — distrusting this "
                         f"(likely today's own close reported as "
                         f"'previous' after market close), NOT caching it, "
                         f"falling through to candle reconstruction instead")
            continue
        if pc:
            pc = round(float(pc), 2)
            _prev_close[sym] = (today, pc)
            pilot.bus.log("app", f"{sym}: prev_close_for — authoritative "
                         f"quote path succeeded via the batched call, "
                         f"previous_close={pc}")
        else:
            pilot.bus.log("app", f"{sym}: prev_close_for — batched call "
                         f"succeeded but no previous_close_price/ohlc.close "
                         f"present; raw q={q!r}")


def require_symbol(symbol):
    """Reject anything outside the four supported indices.

    v58.75 — the SQL layer is parameterised (audited: every
    execute(f"…") in history.py interpolates a schema identifier or a
    fragment of `?` placeholders, never a caller value), so a payload
    like "' OR '1'='1" was harmless — but it still returned 200 with an
    empty body. Answering a nonsense symbol as though it were a real one
    is how a probe learns which parameters reach a database at all.
    Reject it at the edge, in the one place, so the answer is the same
    everywhere.
    """
    sym = (symbol or "").upper()
    if sym not in SYMBOLS:
        raise HTTPException(400, f"unknown symbol {symbol!r} — expected one of "
                                 f"{', '.join(sorted(SYMBOLS))}")
    return sym


@app.get("/api/chain-snapshots/{symbol}")
def api_chain_snapshots(symbol: str, hours: float = 6,
                        strike: float | None = None, leg: str | None = None):
    """Feature #4 — direct read access to the persisted `chain_
    snapshots` table (increment 1's every-60s-by-default historical
    capture) for manual backtesting/review against real captured data,
    per explicit request. Returns raw rows within the last `hours`
    (default 6), optionally filtered to one strike and/or leg — e.g.
    `/api/chain-snapshots/NIFTY?strike=23900&leg=ce&hours=24`. Empty
    `rows` (not an error) means nothing's been persisted yet for that
    filter — same graceful-degradation convention as the rest of
    Feature #4."""
    symbol = require_symbol(symbol)
    import history
    conn = history._conn()
    cutoff = int(time.time() - hours * 3600)
    q = ("SELECT strike, leg, ts, ltp, oi, oi_chg, volume, iv, delta, "
        "gamma, theta, vega, bid, ask FROM chain_snapshots "
        "WHERE symbol=? AND ts>=?")
    params = [symbol.upper(), cutoff]
    if strike is not None:
        q += " AND strike=?"
        params.append(strike)
    if leg is not None:
        q += " AND leg=?"
        params.append(leg.lower())
    q += " ORDER BY ts"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"symbol": symbol.upper(), "hours": hours, "count": len(rows),
           "rows": [{"strike": r[0], "leg": r[1], "ts": r[2], "ltp": r[3],
                     "oi": r[4], "oi_chg": r[5], "volume": r[6], "iv": r[7],
                     "delta": r[8], "gamma": r[9], "theta": r[10],
                     "vega": r[11], "bid": r[12], "ask": r[13]}
                    for r in rows]}


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
            a, _w = bus_analysis_or_warming(symbol.upper())
            if _w:
                a = None
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
    """Previous session close, cached for the day.

    2026-07-27 (part 2) — a live report showed ALL FOUR indices'
    displayed change off from TradingView by a small, consistent
    amount (e.g. NIFTY ~19.55 pts, ~0.08% of spot) even with `broker:
    "dhan"` already active — ruling out the broker-mismatch root cause
    the earlier SENSEX-specific fix addressed. Investigated further:
    this function derived prev_close by reconstructing it from 15-
    minute candles, when Dhan's own quote API already returns an
    authoritative `previous_close_price` field directly (confirmed
    already used this way for OPTION LEGS in broker_adapter.py's
    `_leg()` — the option-leg `chg` field never had this problem,
    only this index-level one did, since it took a different, less
    direct path). Reconstructing from the last 15-min candle's close
    is inherently one step removed from the exchange's own official
    reference print and can drift by the amount observed.

    Now tries Dhan's quote_batch() (IDX_I segment, the same endpoint
    and pacing already established for futures polling) FIRST, and
    only falls back to the candle-reconstruction approach below if
    that field isn't present or the call fails — kept as a genuine
    safety net, not deleted, since the exact schema Dhan returns for
    an INDEX (as opposed to already-confirmed OPTION LEG) quote hasn't
    been verified against a live server from this environment.

    2026-07-27 (part 1) — two earlier bugs, still in effect: prefers
    the dedicated Dhan fallback client for SENSEX specifically
    (matching get_chain()'s established reasoning), and all day-
    boundary comparisons are explicitly IST-aware rather than relying
    on the server process's ambient system timezone.
    """
    import datetime as _dt
    sym = symbol.upper()
    today = agents.now_ist().date().isoformat()
    hit = _prev_close.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    # 2026-07-27 (part 4) — try the shared batch fetch first (all 4
    # symbols in ONE quote_batch() call, fixing the confirmed 429
    # rate-limit from calling it 4 separate times). Re-check the cache
    # immediately after — if this symbol was included, we're done. The
    # rest of this function (per-symbol quote_batch, candle
    # reconstruction) remains as the safety net for whatever the batch
    # didn't cover.
    _batch_fetch_prev_close(today)
    hit = _prev_close.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    d = dhan_client()
    if sym == "SENSEX":
        # SENSEX trades on BSE — the active broker may not serve it
        # correctly (or at all) even if `d` itself is non-None, same
        # reasoning get_chain() already documents. Prefer the dedicated
        # Dhan fallback client outright for this symbol rather than
        # trusting whichever broker happens to be active in Settings.
        fb = _dhan_fallback_client()
        if fb is not None:
            d = fb
    if d is None:
        return None
    if hasattr(d, "quote_batch") and not _quote_rate_limited():
        try:
            from broker_adapter import UNDERLYINGS
            sec_id = UNDERLYINGS.get(sym)
            if sec_id is None:
                pilot.bus.log("app", f"{sym}: prev_close_for — no UNDERLYINGS "
                             f"security_id mapping, can't try the authoritative "
                             f"quote path, falling back to candle reconstruction")
            else:
                data = d.quote_batch({"IDX_I": [sec_id]})
                q = (data.get("IDX_I") or {}).get(str(sec_id)) or {}
                pc = q.get("previous_close_price") or (q.get("ohlc") or {}).get("close")
                last_price = q.get("last_price")
                suspicious = bool(pc and last_price and abs(float(pc) - float(last_price)) < 0.01)
                if pc and not suspicious:
                    pc = round(float(pc), 2)
                    _prev_close[sym] = (today, pc)
                    pilot.bus.log("app", f"{sym}: prev_close_for — authoritative "
                                 f"quote path succeeded, previous_close={pc}")
                    return pc
                # 2026-07-27 (part 3) — real gap found: this fallback
                # was COMPLETELY SILENT — a bare `except: pass` with no
                # logging at all, meaning there was no way to tell,
                # from a live server, whether the authoritative path
                # actually worked or silently fell back, or why. A live
                # report showed the SAME discrepancy persisting after
                # this fix shipped — this logging is what actually
                # answers that, instead of guessing again. Logs the
                # RAW quote payload received so the real field names
                # Dhan returns for an INDEX quote (as opposed to the
                # already-confirmed option-leg one) can finally be
                # seen from a real server, not assumed.
                #
                # 2026-07-27 (part 5) — a SECOND real regression found
                # from live testing: NIFTY/FINNIFTY both started
                # showing 0 (0%) change after part 4 shipped. Tested
                # AFTER market close (per the person's own screenshot
                # — "MARKET CLOSED" badge) — Dhan's
                # previous_close_price appears to reflect the JUST-
                # CONCLUDED session's OWN close once the market has
                # shut for the day, not genuinely yesterday's close as
                # assumed. Since last_price (same response, no extra
                # call) equals today's own last traded price once the
                # market is closed, a previous_close that EXACTLY
                # matches last_price is a strong signal of this — a
                # real prior-day close essentially never matches the
                # current price exactly. Distrusted and NOT cached in
                # that case, falling through to candle reconstruction
                # instead, which is unaffected by this after-hours
                # field-semantics ambiguity.
                if suspicious:
                    pilot.bus.log("app", f"{sym}: prev_close_for — "
                                 f"previous_close_price ({pc}) exactly "
                                 f"matches last_price ({last_price}) — "
                                 f"distrusting this (likely today's own "
                                 f"close reported as 'previous' after "
                                 f"market close), falling back to candle "
                                 f"reconstruction instead")
                else:
                    pilot.bus.log("app", f"{sym}: prev_close_for — quote_batch() "
                                 f"call succeeded but neither previous_close_price "
                                 f"nor ohlc.close was present in the response; "
                                 f"raw q={q!r} — falling back to candle "
                                 f"reconstruction (the known-less-accurate path)")
        except Exception as e:
            pilot.bus.log("app", f"{sym}: prev_close_for — quote_batch() call "
                         f"itself raised {type(e).__name__}: {e} — falling "
                         f"back to candle reconstruction")
    try:
        c = d.intraday(sym, "15")["candles"]
        days = {}
        for k in c:
            day = _dt.datetime.fromtimestamp(k["time"], tz=agents.IST).date().isoformat()
            days[day] = k["close"]
        past = sorted([dy for dy in days if dy < today])
        pc = days[past[-1]] if past else None
        if pc:
            _prev_close[sym] = (today, pc)
            pilot.bus.log("app", f"{sym}: prev_close_for — candle "
                         f"reconstruction fallback produced previous_close="
                         f"{pc} (from {past[-1] if past else 'n/a'}'s last "
                         f"15m candle)")
        return pc
    except Exception as e:
        pilot.bus.log("app", f"{sym}: prev_close_for — candle reconstruction "
                     f"fallback ALSO failed: {type(e).__name__}: {e} — "
                     f"returning None")
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


_disp_cache = {}          # sym -> (fetched_at, analysis)
_DISP_MIN_INTERVAL = 60   # seconds between broker fetches PER SYMBOL


def bus_analysis_or_warming(sym, allow_fetch=True, max_age=None):
    """Analysis for a display panel: bus first, then a THROTTLED fetch.

    History, because this function has now been wrong in both
    directions.

    v58.53 removed a fetch that looked reckless:

        analysis = pilot.bus.get(f"analysis:{sym}") or analyze(get_chain(sym))

    On a cold bus that fetched on EVERY poll of EVERY panel, which
    produced a 502 and a rate-limit storm. Banning the fetch fixed that
    and immediately broke something else: MarketDataAgent does not
    populate the bus when the market is closed ("not fetching, last data
    retained"), so after a restart out of hours NOTHING set
    analysis:{sym} and every index panel showed "start agents"
    indefinitely. That worked in v58.38 precisely because of the fetch
    I removed.

    The defect was never "display endpoints fetch" — it was "display
    endpoints fetch UNTHROTTLED". So: fetch, but at most once per
    _DISP_MIN_INTERVAL per symbol, serving a cached result in between.
    Twenty panels polling every 5s now cost one broker call a minute per
    symbol instead of hundreds, and out-of-hours viewing works again.

    Returns (analysis_or_None, problem_dict_or_None).
    """
    a = pilot.bus.get(f"analysis:{sym}")
    if a:
        return a, None

    import rate_limit
    # An expired token is NOT "warming up" and must not be reported as
    # though it will resolve on its own — it needs a human.
    if rate_limit.is_auth_failure("quote"):
        return None, {"warming_up": False, "auth_expired": True,
                      "reason": "Dhan token expired - paste a fresh Access "
                                "Token in Settings; no data can load until then"}
    if rate_limit.is_limited("quote"):
        cached = _disp_cache.get(sym)
        if cached:
            return cached[1], None
        return None, {"warming_up": True,
                      "reason": f"broker rate-limited, retrying in "
                                f"{rate_limit.remaining('quote'):.0f}s"}

    hit = _disp_cache.get(sym)
    if hit and (time.time() - hit[0]) < (max_age or _DISP_MIN_INTERVAL):
        return hit[1], None

    if not allow_fetch:
        return None, {"warming_up": True,
                      "reason": "no analysis on the bus yet"}
    try:
        fresh = analyze(get_chain(sym))
        _disp_cache[sym] = (time.time(), fresh)
        return fresh, None
    except Exception as e:
        import rate_limit as _rl
        _rl.note_failure(e, "quote")
        if hit:
            # A stale panel beats an empty one, as long as it is honest
            # about being stale.
            return hit[1], None
        return None, {"warming_up": True, "reason": f"{type(e).__name__}: {e}"}


@app.get("/api/ai_visual/{symbol}")
def api_ai_visual(symbol: str):
    try:
        sym = symbol.upper()
        analysis, _warm = bus_analysis_or_warming(sym)
        if _warm:
            return _warm
        context = {"news": pilot.bus.get("news"),
                   "social_mood": (pilot.bus.get("social") or {}).get("mood"),
                   "macro": (pilot.bus.get("macro") or {}).get("stance")}
        return ai_visual(analysis, context=context)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/dynamic-risk")
def api_dynamic_risk():
    """Feature #9 Dynamic Risk Monitoring — active real-time risk
    signals for all currently open positions (ExecutionAgent._monitor_
    one, checked every 2s). Returns {symbol: [events]} for whichever
    symbols currently have open positions with active signals."""
    positions = pilot.bus.get("positions", {}) or {}
    result = {}
    for sym in positions:
        events = pilot.bus.get(f"dynamic_risk:{sym}")
        if events:
            result[sym] = events
    return {"available": True, "signals": result}


@app.get("/api/risk-score")
def api_risk_score():
    """Feature #9 (Institutional Portfolio Risk Engine) — AI Risk
    Score composite + Portfolio Greeks aggregation. Computed ambiently
    by RiskAgent.cycle() every ~10s (agents.py), read here for
    dashboard display. Same "not yet computed" graceful pattern as
    the other engine endpoints."""
    score = pilot.bus.get("ai_risk_score")
    greeks = pilot.bus.get("portfolio_greeks")
    if score is None:
        return {"available": False,
               "reason": "not yet computed — needs at least one RiskAgent cycle (~10s)"}
    return {"available": True, "risk_score": score, "portfolio_greeks": greeks}


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
    # 2026-07-27 — real bug found from a live report: futures positions
    # were completely absent here. Not just a display gap — Unrealized
    # P&L itself was UNDERSTATED any time a futures position was open,
    # since its pnl was never included in the sum below either.
    futures_positions = pilot.bus.get("futures_positions", {}) or {}
    pos = pilot.bus.get("position")   # legacy single-position mirror
    realized = sum(t.get("pnl", 0) for t in closed)
    total_fees = sum(t.get("fees", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    unrealized = sum(p.get("pnl", 0) for p in positions.values()) + \
        sum(s.get("pnl", 0) for s in spreads.values()) + \
        sum(f.get("pnl", 0) for f in futures_positions.values())

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

    # 2026-07-28 — Guardrails panel data, per the wireframe's P&L sheet
    # ("exactly the conditions the risk gate checks"). Every value here
    # already exists elsewhere (config limits; consecutive_losses is
    # already pushed to /api/autopilot/status; portfolio drawdown uses
    # the EXACT same computation as ExecutionAgent._check_portfolio_
    # kill_switch, reused rather than re-derived differently) — this
    # just gathers them into one place for display instead of requiring
    # the frontend to poll multiple endpoints and know the formulas.
    cfg = config.load()
    today_realized = sum(t.get("pnl", 0) for t in closed
                        if (t.get("closed_date") or (t.get("closed_at") or "")[:10])
                        == agents.now_ist().strftime("%Y-%m-%d"))
    consecutive_losses = next(
        (a.consecutive_losses for a in pilot.agents if a.name == "risk"), 0)
    guardrails = {
        "daily_pnl": round(today_realized, 0),
        "daily_loss_limit": cfg.get("daily_loss_limit", 5000),
        "daily_profit_target": cfg.get("daily_profit_target", 0),
        "consecutive_losses": consecutive_losses,
        "consecutive_losses_limit": cfg.get("stop_after_consecutive_losses", 2),
        "portfolio_drawdown": round(-unrealized, 0) if unrealized < 0 else 0,
        "portfolio_max_drawdown": cfg.get("portfolio_max_drawdown", 15000),
    }

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
        "open_futures": futures_positions,   # 2026-07-27 — the SAME gap,
                                       # never caught when futures positions
                                       # were added: absent here entirely,
                                       # so a real open futures position
                                       # showed nowhere on the P&L page and
                                       # its pnl was silently excluded from
                                       # Unrealized P&L above.
        "closed": closed[::-1],
        "daily": daily,
        "guardrails": guardrails,
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


def _s8_detector_summary(detectors):
    """Turn Strategy 8's detector map into one readable line.

    Without this the page would show a bare "not eligible" for S8 and
    give no clue WHICH of the three detectors looked, what each saw, or
    whether one was simply switched off — the same opacity that hid the
    fact S8 was not being evaluated at all until v58.37.
    """
    if not detectors:
        return "no detector data"
    parts = []
    for k, v in detectors.items():
        if v is True:
            parts.append(f"{k}: FIRED")
        elif isinstance(v, str):
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}: no pattern")
    return " · ".join(parts)


@app.get("/api/strategies/{symbol}")
def api_strategies(symbol: str):
    """Evaluate every library strategy for a symbol: eligibility + preview."""
    import strategies as slib
    sym = symbol.upper()
    # v58.54 — routed through the throttled helper like the other
    # display endpoints. This page is polled too, and an unthrottled
    # fetch here has the same amplification problem.
    analysis, _warm = bus_analysis_or_warming(sym)
    if not analysis:
        return {"error": "no analysis available: "
                         + str((_warm or {}).get("reason", "unknown"))}
    # 2026-07-26 — falls back to the last-session regime so this page
    # isn't useless outside market hours. Without it, `regime` is None,
    # slib.evaluate() sees reg="unknown", and every strategy reports
    # "regime 'unknown' not suited" — which reads as "the regime is
    # wrong for this strategy" when the truth is "there is no regime
    # data right now". Showing Friday's read is genuinely useful for
    # planning Monday.
    #
    # This is a DISPLAY path. The deploy endpoint below deliberately
    # does NOT take this fallback and refuses outright while the regime
    # is stale — the guard is server-side because a disabled button is
    # only a UI hint, and /api/strategies/deploy is callable directly.
    regime = pilot.bus.get(f"regime:{sym}")
    regime_stale = False
    if not regime:
        regime = pilot.bus.get(f"regime_last_session:{sym}")
        regime_stale = bool(regime)
    out = []
    auto = config.load().get("auto_strategies") or []
    # 2026-07-26 (v54) — live/disabled status for the consolidated
    # Strategies-page table's Status column. Deliberately reads ONLY
    # the persisted version file (a cheap JSON read) rather than
    # running any backtest computation, which the Backtest page's own
    # heavier endpoint already owns.
    import backtester as bt
    versions = bt.load_versions()
    for name, meta in slib.META.items():
        ev = slib.evaluate(name, analysis, regime) or {}
        v_entry = versions.get(name, {}).get("symbols", {}).get(sym, {})
        out.append({"name": name, **meta,
                    "auto": name in auto,
                    "regime_fit": list(slib.REGIME_FIT[name]),
                    "eval": ev,
                    "live_enabled": bool(v_entry.get("live_enabled")),
                    "manually_disabled": bool(v_entry.get("manually_disabled"))})
    # S4 Phase 2 (v52) — futures auto-strategy eligibility, same
    # "visibility without deployment" philosophy as S7's card: works
    # whether or not futures_auto_deploy is on. Pure read (the eval
    # function does no bus writes / no order placement).
    fut_sig = {"enabled": config.load().get("futures_strategy_enabled", False),
               "auto": config.load().get("futures_auto_deploy", False),
               "eligible": None, "gates": None, "why": None}
    try:
        ex = next((a for a in pilot.agents if a.name == "execution"), None)
        if ex:
            ev, gates = ex._futures_signal_eval(sym, config.load())
            fut_sig["eligible"] = bool(ev)
            fut_sig["gates"] = gates
            if ev:
                fut_sig["why"] = ev["why"]
    except Exception as e:
        fut_sig["why"] = f"evaluation error: {type(e).__name__}: {e}"
    # Strategy 7 (v51) — live gate breakdown for the eligibility card.
    # Evaluated on demand so the card works with s7_auto_deploy OFF
    # (visibility without deployment, same philosophy as the stale-
    # regime listing above). Uses the SAME evaluate_sg_ema + the SAME
    # zigzag the chart draws — parity by construction.
    s7 = {"enabled": config.load().get("strategy7_enabled", True),
          "auto": config.load().get("s7_auto_deploy", False),
          "eligible": None, "gates": None, "why": None}
    try:
        import pa_strategies as pa_lib
        import structure
        pack = pilot.bus.get(f"pa_candles:{sym}")
        if pack and pack.get("c1"):
            cfg_now = config.load()
            params = {"fast": cfg_now.get("s7_ema_fast", 5),
                      "slow": cfg_now.get("s7_ema_slow", 13),
                      "mtf_confirm": cfg_now.get("s7_mtf_confirm", 1),
                      "require_structure": 1 if cfg_now.get("s7_require_structure", True) else 0,
                      "require_ai_bias": 1 if cfg_now.get("s7_require_ai_bias", True) else 0,
                      "min_ai_bias": cfg_now.get("s7_min_ai_bias", 20),
                      "structural_stop_buffer_pct": cfg_now.get("s7_structural_stop_buffer_pct", 0.05),
                      "rr_target": cfg_now.get("s7_rr_target", 2.0),
                      "max_trades_per_day": cfg_now.get("s7_max_trades_per_day", 2)}
            ev, gates = pa_lib.evaluate_sg_ema(
                pack["c1"], pack["c5"], pack.get("c15"), params=params,
                pivots=structure.zigzag_series(pack["c1"]),
                ai_bias=pilot.bus.get(f"bias:{sym}"))
            s7["eligible"] = bool(ev)
            s7["gates"] = gates
            s7["why"] = (ev or {}).get("why")
        else:
            s7["why"] = ("no session candles yet (pa_candles empty — "
                         "populated by RegimeAgent during market hours)")
    except Exception as e:
        s7["why"] = f"evaluation error: {type(e).__name__}: {e}"
    mtf = pilot.bus.get(f"mtf_confluence:{sym}")
    cfg = config.load()
    # 2026-07-26 (v54) — regime confidence/confluence and the AI Market
    # Bias (Feature #2) weren't previously in this payload; the label
    # alone ("trending-up") doesn't read as a combined "Bias/Regime"
    # column the way "Neutral-bearish \u00b7 trending-down (62%
    # confidence)" does. Added for the consolidated Strategies-page
    # table rather than a separate endpoint, since regime/bias are
    # already loaded above for the same request.
    ai_bias = pilot.bus.get(f"bias:{sym}")
    # 2026-07-26 (v54) — "current position" per strategy, for the
    # consolidated Strategies-page table. Spreads live in a separate
    # `spreads` bus dict keyed by a composite spread_id
    # ("SYM:strategy:strike" — see §5.7); single-leg positions
    # (momentum_buy/orb/vwap_pullback/ema_mtf/sg_ema/mtf_confluence) all
    # share ONE `positions` dict keyed by symbol only, distinguished by
    # the `setup` field every entry tags itself with (falls back to
    # `source` when a strategy doesn't set `setup` explicitly \u2014
    # mtf_confluence's signal already carries source="mtf_confluence",
    # confirmed by reading agents.py before relying on it here rather
    # than assuming). Futures has its own `futures_positions` dict.
    open_spreads_all = pilot.bus.get("spreads", {}) or {}
    positions = pilot.bus.get("positions", {}) or {}
    futures_positions = pilot.bus.get("futures_positions", {}) or {}
    sym_position = positions.get(sym)

    def _spread_position(name):
        return next((v for k, v in open_spreads_all.items()
                    if k.startswith(f"{sym}:{name}:")), None)

    def _setup_position(setup_name):
        return sym_position if (sym_position or {}).get("setup") == setup_name else None

    current_positions = {
        "bull_put_spread": _spread_position("bull_put_spread"),
        "bear_call_spread": _spread_position("bear_call_spread"),
        "mtf_confluence": _setup_position("mtf_confluence"),
        "sg_ema": _setup_position("sg_ema"),
        "futures_signal": futures_positions.get(sym),
        "orb": _setup_position("orb"),
        "vwap_pullback": _setup_position("vwap_pullback"),
        "ema_mtf": _setup_position("ema_mtf"),
        "momentum_buy": _setup_position("momentum_buy"),
    }
    # 2026-07-26 (v54.1) — the other three PA strategies (ORB, Anchor
    # Pullback, 9/20 EMA Cross) and the core momentum_buy engine were
    # missing from the consolidated Strategies-page table entirely
    # (flagged directly by the user rather than silently left out).
    # Added using the SAME pattern S7 already established: pure,
    # side-effect-free evaluation reusing the exact functions the live
    # agents call, not a second reimplementation that could drift.
    #
    # 2026-07-27 — real gap found while wiring in a new PA strategy
    # (momentum_confluence): this tuple was hardcoded and had ALREADY
    # silently excluded sg_ema (Strategy 7) since it was added — the
    # exact "two lists that drift" class of bug already found and
    # fixed once this session elsewhere (news_engine.py's duplicate
    # regexes). Derives from pa_lib2.PA_NAMES directly now instead of
    # a manually-maintained tuple, so any FUTURE new PA strategy is
    # automatically included here with no separate line to remember.
    import pa_strategies as pa_lib2
    pack = pilot.bus.get(f"pa_candles:{sym}")
    pa_previews = {}
    # sg_ema (Strategy 7) is deliberately excluded from this generic
    # loop: it needs a DIFFERENT call (evaluate_sg_ema(), with
    # pivots/ai_bias arguments the generic evaluate() dispatcher
    # doesn't accept) — calling it through evaluate() here would
    # silently fall through to a bare `return None`, misreporting it
    # as "not eligible" rather than reflecting its real structure/
    # AI-bias-gated logic. Its own preview is handled separately
    # elsewhere. Every other PA strategy (including new ones added to
    # PA_NAMES going forward) works correctly through this generic path.
    # v58.37 — ew_reversal joins sg_ema as an exclusion for exactly the
    # reason the comment above gives: pa_lib2.evaluate() does NOT
    # dispatch it (Strategy 8 lives in its own module with its own
    # signature), so routing it through the generic path fell to a bare
    # `return None` and reported it as "not eligible" permanently. Its
    # real per-detector verdict is published by PriceActionAgent every
    # cycle on the bus, so it is read from there instead.
    for pa_name in (n for n in pa_lib2.PA_NAMES
                    if n not in ("sg_ema", "ew_reversal")):
        if pack and pack.get("c1"):
            try:
                p = pa_lib2.PA_DEFAULTS.get(pa_name, {})
                ev = pa_lib2.evaluate(pa_name, pack["c1"], pack["c5"],
                                      pack.get("c15"), params=p)
                pa_previews[pa_name] = {"eligible": bool(ev),
                                        "why": (ev or {}).get("why")}
            except Exception as e:
                pa_previews[pa_name] = {"eligible": None,
                                        "why": f"evaluation error: {e}"}
        else:
            pa_previews[pa_name] = {"eligible": None,
                                    "why": "no session candles yet "
                                          "(pa_candles empty)"}
        # These three (unlike momentum_buy) go through the SAME
        # backtest-approval gate as spreads — confirmed by grepping
        # agents.py for is_live_enabled rather than assuming (it's
        # checked in PriceActionAgent for orb/vwap_pullback/ema_mtf/
        # sg_ema, but NOT in StrategyAgent for momentum_buy, which has
        # no such gate at all). Reuses the same `versions` dict already
        # loaded above for spreads — no extra read.
        pv_entry = versions.get(pa_name, {}).get("symbols", {}).get(sym, {})
        pa_previews[pa_name]["live_enabled"] = bool(pv_entry.get("live_enabled"))
        pa_previews[pa_name]["manually_disabled"] = bool(pv_entry.get("manually_disabled"))
    # momentum_buy: the deterministic RULE-ENGINE preview
    # (analyzer._rule_signal), NOT the live path's ai_signal() — that
    # function can call out to an LLM and is neither free nor fast
    # enough to run on every poll of this page. The live agent may use
    # the full AI Decision Engine when ai_engine != "off"; this preview
    # is honestly narrower (rule-engine only) and says so.
    # Strategy 8 — read the verdict PriceActionAgent publishes rather
    # than re-deriving it (one evaluation, one source of truth).
    _s8 = pilot.bus.get(f"s8_eligibility:{sym}")
    if _s8:
        pa_previews["ew_reversal"] = {
            "eligible": _s8.get("eligible"),
            "why": _s8.get("why") or _s8_detector_summary(_s8.get("detectors"))}
    else:
        pa_previews["ew_reversal"] = {
            "eligible": None,
            "why": "not evaluated yet (strategy8_enabled off, or no "
                   "session candles)"}
    # Strategy 9 lives in its own agent and is deliberately NOT in
    # PA_NAMES, so it has never appeared on this page at all. Read its
    # published state the same way.
    _ta = pilot.bus.get(f"ta_state:{sym}")
    _tc = pilot.bus.get(f"ta_confluence:{sym}") or {}
    if _ta and _ta.get("ok"):
        pa_previews["ta_elliott"] = {
            "eligible": False if _tc.get("count") else None,
            "why": (f"phase {_ta.get('phase')} · tide "
                    f"{_ta.get('tide')} · confluence {_tc.get('count', '?')}"
                    + (f" · {_tc['phase']}" if isinstance(_tc.get('phase'), str) else ""))}
    else:
        pa_previews["ta_elliott"] = {
            "eligible": None,
            "why": (_ta or {}).get("reason", "no state yet (needs 5m candles)")}

    momentum_preview = {"eligible": None, "why": "no analysis yet"}
    if analysis and not analysis.get("error"):
        try:
            from analyzer import _rule_signal
            rsig = _rule_signal(analysis)
            direction_ok = rsig["signal"] in (regime or {}).get("allowed_signals", [])
            confluence = (regime or {}).get("confluence", "no-alignment")
            conf_ok = ((rsig["signal"] == "BUY_CE" and confluence in ("strong-bull", "mixed-bull")) or
                      (rsig["signal"] == "BUY_PE" and confluence in ("strong-bear", "mixed-bear")))
            eligible = rsig["signal"] != "WAIT" and (not regime or (direction_ok and conf_ok))
            momentum_preview = {"eligible": eligible,
                                "why": ("; ".join(rsig.get("reasons", [])) or rsig["signal"])
                                       + " (rule-engine preview \u2014 live path may use AI when enabled)"}
        except Exception as e:
            momentum_preview = {"eligible": None, "why": f"evaluation error: {e}"}
    return {"symbol": sym,
            "regime": (regime or {}).get("regime", "unknown"),
            "regime_confidence": (regime or {}).get("confidence"),
            "regime_confluence": (regime or {}).get("confluence"),
            "ai_bias": ai_bias,
            # Set when the eligibility above was computed from a
            # previous session's regime rather than today's. The
            # dashboard uses this to label the cards and disable the
            # deploy buttons; the deploy endpoint enforces it too.
            "regime_stale": regime_stale,
            "regime_session_date": (regime or {}).get("session_date"),
            "s7": s7,
            "futures_signal": fut_sig,
            "pa_previews": pa_previews,
            "momentum_preview": momentum_preview,
            "current_positions": current_positions,
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


@app.post("/api/strategies/pa_toggle")
def api_strategies_pa_toggle(body: StrategyToggleIn):
    """v55.1 — mirrors /api/strategies/toggle above, but for the PA
    strategies' `pa_enabled` list (orb/vwap_pullback/ema_mtf/sg_ema),
    which PriceActionAgent already reads
    (`cfg.get("pa_enabled", list(pa.PA_NAMES))`) but had no endpoint to
    modify per-strategy — closing the gap flagged since v54.1 where the
    Strategies-page table's Auto Deploy checkbox for these three was
    read-only. sg_ema is technically also in this list but has its OWN
    dedicated s7_auto_deploy gate checked separately in
    PriceActionAgent, so toggling it here is a valid additional way to
    disable it but the Settings-page s7_auto_deploy switch remains the
    primary control for that one — this endpoint is really for
    orb/vwap_pullback/ema_mtf."""
    cfg = config.load()
    import pa_strategies as pa
    enabled = set(cfg.get("pa_enabled", list(pa.PA_NAMES)))
    if body.name not in pa.PA_NAMES:
        return {"error": f"'{body.name}' is not a PA strategy "
                         f"(expected one of {list(pa.PA_NAMES)})"}
    if body.enabled:
        enabled.add(body.name)
    else:
        enabled.discard(body.name)
    config.save({"pa_enabled": sorted(enabled)})
    return {"pa_enabled": sorted(enabled)}


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
    # 2026-07-26 — explicit guard. The Strategies LISTING now falls back
    # to the last session's regime so the page is readable after hours,
    # but a deploy must never be evaluated against it: entry filters for
    # both credit spreads are regime-conditional (REGIME_FIT), so
    # Friday's "trending-up" could green-light a bull put spread on
    # Monday's gap-down open. Enforced here rather than only by
    # disabling the button, because this endpoint is directly callable.
    # Also replaces the misleading fall-through message this produced
    # before: with regime=None, slib.evaluate() reported "regime
    # 'unknown' not suited", which reads as a regime mismatch rather
    # than missing data.
    if not regime:
        last = pilot.bus.get(f"regime_last_session:{sym}")
        if last:
            return {"error": f"Regime data is from "
                             f"{last.get('session_date')} (last session), "
                             f"not today — eligibility is shown for "
                             f"analysis only. Deploy is blocked until "
                             f"today's regime read completes (~90s after "
                             f"market open)."}
        return {"error": f"No regime data for {sym} yet — deploy blocked "
                         f"until the regime engine has classified today's "
                         f"session (~15 min after open)."}
    ev = slib.evaluate(body.name, analysis, regime)
    if not ev or not ev.get("eligible"):
        return {"error": "Not eligible right now: "
                + "; ".join((ev or {}).get("reasons", ["unknown"]))}
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "execution agent not running"}
    return ex.enter_spread(ev)


class FutureIn(BaseModel):
    symbol: str
    side: str = "LONG"
    lots: int = 1


# ---- Futures Research page (v59.0 §8, item 30) — READ ONLY ----------
# This page is an evidence record, not a deploy surface. Every endpoint
# below is a GET that reads state. There is deliberately NO
# /api/futures/hedge/toggle and no deploy POST: the spec listed one, but
# the engagement's finding is that nothing here should be deployable
# yet, and a toggle on an evidence page is how that gets forgotten.
@app.get("/api/futures/research/state")
def api_futures_research_state():
    import futures_research_api as fra
    return fra.research_state(pilot.bus)


@app.get("/api/futures/postmortem")
def api_futures_postmortem():
    import futures_research_api as fra
    return fra.postmortem()


@app.get("/api/futures/gate")
def api_futures_gate():
    """The promotion-gate table — the OPTIONS finding, not the futures one."""
    import futures_research_api as fra
    return fra.promotion_gate_table()


@app.get("/api/futures/costs")
def api_futures_costs():
    import futures_research_api as fra
    return fra.cost_readout()


@app.get("/api/futures/hedge")
def api_futures_hedge():
    import futures_research_api as fra
    return fra.hedge_monitor()


@app.get("/api/futures/basis/{symbol}")
def api_futures_basis(symbol: str):
    obs = pilot.bus.get(f"basis_residual:{symbol.upper()}") or {}
    import history as _h
    return {"observation": obs,
            "series": _h.basis_residual_series(symbol.upper(), 500)}


@app.post("/api/futures/enter")
def api_futures_enter(body: FutureIn):
    """S4 (v50) — open a paper futures position (Phase 1: manual/API
    only; auto-strategies are Phase 2). All gating (paper-only, market
    open, margin, kill-switch cooldown, one position per symbol) is
    enforced inside ExecutionAgent.enter_future, not here, so a direct
    API call gets exactly the same protections as the UI button."""
    if not pilot.running:
        return {"error": "Start the agents first."}
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "execution agent not running"}
    return ex.enter_future(body.symbol.upper(), body.side, body.lots)


@app.post("/api/futures/exit")
def api_futures_exit(body: FutureIn):
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "agents not running"}
    return ex.exit_future(body.symbol.upper(), "manual exit from dashboard")


class ManualDeployIn(BaseModel):
    symbol: str


@app.post("/api/futures/manual_deploy")
def api_futures_manual_deploy(body: ManualDeployIn):
    """v57.1 — manual-deploy for the Futures Signal engine (S4 Phase
    2), closing the honesty gap the Strategies-page table has shown
    since v54 ("—" where no manual trigger existed). Reuses the exact
    two functions the automatic engine and the eligibility-preview API
    already call — `_futures_signal_eval()` (pure, no side effects,
    already used for the table's "why is/isn't this eligible right
    now" text) and `enter_future()` (already carries every real safety
    gate: paper-mode, market hours, margin, kill-switch cooldown, one
    position per symbol). No new evaluation logic, no new gate — this
    endpoint is "evaluate right now, and if it says yes, enter right
    now," identical in spirit to clicking Deploy on a spread."""
    sym = body.symbol.upper()
    cfg = config.load()
    # 2026-07-27 — real gap found: _futures_signal_eval() (the pure
    # eligibility function) never checked futures_strategy_enabled at
    # all — only _futures_signal_engine() (the AUTOMATIC loop) did.
    # This meant turning the flag off in Settings correctly stopped
    # auto-deploy but did NOT stop this manual "Fire Now" endpoint,
    # which called the eval function directly. Direct instruction
    # after real trading data showed consistent futures losses: the
    # flag must actually gate every signal-driven entry path, not just
    # the automatic one.
    if not cfg.get("futures_strategy_enabled", False):
        return {"error": "Futures Signal is disabled in Settings — "
                         "futures data is still used for analysis, "
                         "but the signal engine won't propose or place "
                         "a trade while this is off."}
    ex = next((a for a in pilot.agents if a.name == "execution"), None)
    if not ex:
        return {"error": "Start the agents first."}
    ev, gates = ex._futures_signal_eval(sym, cfg)
    if not ev:
        return {"error": f"Not eligible right now for {sym}: " +
                         "; ".join(f"{k}={v}" for k, v in gates.items())}
    return ex.enter_future(sym, ev["side"], ev["lots"])


@app.post("/api/strategies/manual_fire")
def api_strategies_manual_fire(body: ManualDeployIn, name: str = "sg_ema"):
    """v57.1/v57.2 — manual-deploy for Strategy 7 (sg_ema) and, as of
    v57.2, MTF Confluence too, closing the LAST piece of the "no
    manual-deploy endpoint for MTF/S7/Futures" gap. Both branches
    re-evaluate RIGHT NOW using the exact same per-symbol method the
    automatic agent loop already calls (`MTFConfluenceAgent.
    _evaluate_and_fire()`, extracted this round the same way sg_ema's
    `build_pa_signal()` was extracted last round — one shared method,
    not a second copy of the entry/stop/target formula), and publish
    through the standard `signal` bus topic so RiskAgent runs the FULL
    existing gate exactly as if the automatic cycle had fired it."""
    sym = body.symbol.upper()
    cfg = config.load()
    if name == "mtf_confluence":
        if not cfg.get("mtf_confluence_enabled", True):
            return {"error": "MTF Confluence is disabled in Settings"}
        if cfg.get("broker", "dhan") != "dhan":
            return {"error": "MTF Confluence requires Dhan as the active broker "
                             "(historical_daily isn't built for other brokers yet)"}
        d = dhan_client()
        if d is None:
            return {"error": "No Dhan client available"}
        ag = next((a for a in pilot.agents if a.name == "mtf_confluence"), None)
        if not ag:
            return {"error": "Start the agents first."}
        outcome = ag._evaluate_and_fire(sym, d, cfg)
        if outcome.startswith("FIRED"):
            return {"ok": True, "note": "queued to the risk pipeline — watch the "
                                        "risk/execution agent boxes", "outcome": outcome}
        return {"error": f"Not eligible right now for {sym}: {outcome}"}
    if name != "sg_ema":
        return {"error": "manual_fire currently only supports sg_ema and mtf_confluence"}
    if not cfg.get("strategy7_enabled", True):
        return {"error": "Strategy 7 is disabled in Settings"}
    if not cfg.get("paper_mode", True):
        return {"error": "Strategy 7 is paper-only — no live path exists yet"}
    import pa_strategies as pa
    import structure
    import backtester
    pack = pilot.bus.get(f"pa_candles:{sym}")
    if not pack or time.time() - pack.get("ts", 0) > 240:
        return {"error": f"no fresh session candles for {sym} yet"}
    analysis = pilot.bus.get(f"analysis:{sym}")
    if not analysis:
        return {"error": f"no analysis for {sym} yet"}
    p = backtester.get_params("sg_ema", sym)
    p = dict(p, fast=cfg.get("s7_ema_fast", 5), slow=cfg.get("s7_ema_slow", 13),
             mtf_confirm=cfg.get("s7_mtf_confirm", 1),
             require_structure=1 if cfg.get("s7_require_structure", True) else 0,
             require_ai_bias=1 if cfg.get("s7_require_ai_bias", True) else 0,
             min_ai_bias=cfg.get("s7_min_ai_bias", 20),
             structural_stop_buffer_pct=cfg.get("s7_structural_stop_buffer_pct", 0.05),
             rr_target=cfg.get("s7_rr_target", 2.0),
             max_trades_per_day=cfg.get("s7_max_trades_per_day", 2))
    pivots = structure.zigzag_series(pack["c1"])
    ex = next((a for a in pilot.agents if a.name == "price_action"), None)
    taken_today = getattr(ex, "_taken", {}).get(f"{sym}:sg_ema", 0) if ex else 0
    ev, gates = pa.evaluate_sg_ema(pack["c1"], pack["c5"], pack.get("c15"),
                                   params=p, taken_today=taken_today,
                                   pivots=pivots, ai_bias=pilot.bus.get(f"bias:{sym}"))
    if not ev:
        return {"error": f"Not eligible right now for {sym}: " +
                         "; ".join(f"{k}={v}" for k, v in gates.items())}
    row = next((r for r in analysis.get("strikes", [])
               if r["strike"] == analysis.get("atm")), None)
    leg = "ce" if ev["dir"] > 0 else "pe"
    entry = row and row[leg].get("ltp")
    if not entry:
        return {"error": "no ATM ltp available right now"}
    spot_risk_pct = abs(ev["entry_spot"] - ev["structural_stop"]) / max(ev["entry_spot"], 1e-9)
    risk_pct = min(0.30, max(0.05, spot_risk_pct * ev["entry_spot"] * 0.5 / max(entry, 1e-9)))
    sig = agents.build_pa_signal("sg_ema", ev, entry, leg, row, analysis, p, risk_pct, gates)
    pilot.bus.publish("signal", {"symbol": sym, "signal": sig, "analysis": analysis})
    if ex:
        if not hasattr(ex, "_taken"):
            ex._taken, ex._cool, ex._day = {}, {}, None
        key = f"{sym}:sg_ema"
        ex._taken[key] = ex._taken.get(key, 0) + 1
        ex._cool[key] = time.time()
    return {"ok": True, "note": "queued to the risk pipeline — watch the risk/execution agent boxes",
           "signal": sig}


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
        # v58.39 — `setup` added, and futures named. Previously this
        # read only `source`/`strategy`: PA signals write `source` on
        # the SIGNAL but the closed-trade record keeps only `setup`, so
        # 12 real PA trades (vwap_pullback 5, orb 3, ema_mtf 2,
        # momentum_confluence 1, sg_ema 1) were invisible inside
        # "CE-buy"/"PE-buy". Futures carry none of the three and no
        # `leg`, so all 40 of them collapsed into "?-buy" — the single
        # biggest loss bucket in the system (-₹23,863) hidden behind an
        # unreadable label, and "buy" is wrong for a futures position
        # anyway.
        if t.get("kind") == "future" or float(t.get("entry") or 0) > 5000:
            side = str(t.get("side") or "").lower()
            return f"futures-{side}" if side else "futures"
        # v58.57 -- `source` is PROVENANCE, not a strategy, and reading it
        # first fragmented momentum_buy across five rows on the Quality
        # page: "AI" (the LLM produced the signal), "rule-engine (AI
        # returned an invalid signal value: None)", "rule-engine (AI
        # unavailable: Ollama/model 'qwen2.5:3b' not reachable -- ...)",
        # plus CE-buy/PE-buy when no field was set at all. An ERROR
        # MESSAGE became a strategy name, and the one strategy's real
        # record was split four ways so none of the rows meant anything.
        #
        # Strategy identity comes from `strategy`/`setup`. A bare `source`
        # of "AI" or "rule-engine (...)" identifies momentum_buy, which is
        # the only path that generates signals that way -- the provenance
        # is still available on the trade for anyone who wants to split
        # by it deliberately.
        name = t.get("strategy") or t.get("setup")
        if not name:
            src_f = str(t.get("source") or "")
            if src_f.startswith("AI") or src_f.startswith("rule-engine"):
                name = "momentum_buy"
            elif src_f:
                name = src_f
        return name or (f"{t.get('leg', '?')}-buy"
                        if t.get("leg") != "SPREAD" else "spread")

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


def _trade_events_to_markers(events):
    """Series Markers, per explicit request (Buy/Sell/Entry/Exit/AI
    Buy/AI Sell/Target Hit/Stop Loss Hit) — converts real trade
    lifecycle events (ExecutionAgent._record_chart_event, chart_events:
    {symbol}) into Lightweight Charts markers. Unlike `_smart_money_to_
    markers()`/`_institutional_to_markers()` above (current-state flags
    recomputed every cycle, needing the anchor_ts workaround), these
    are genuine historical events with their own real timestamp — used
    directly, bucketed to the minute to align with 1m candle
    granularity. ALL persisted events are converted every time (not
    just new ones this cycle) since Lightweight Charts' setMarkers()
    replaces the full marker set — the caller is responsible for
    combining this with the other marker types before sending."""
    markers = []
    for e in events or []:
        ts = int(e["time"] // 60) * 60
        kind = e.get("kind")
        if kind == "entry":
            markers.append({"time": ts, "position": "belowBar", "color": "#3fb950",
                            "shape": "arrowUp", "text": "Buy Entry: " + e.get("label", "")})
        elif kind == "target_hit":
            markers.append({"time": ts, "position": "aboveBar", "color": "#3fb950",
                            "shape": "circle", "text": "Target Hit: " + e.get("label", "")})
        elif kind == "stop_hit":
            markers.append({"time": ts, "position": "aboveBar", "color": "#f85149",
                            "shape": "circle", "text": "Stop Loss Hit: " + e.get("label", "")})
        elif kind == "ai_sell":
            markers.append({"time": ts, "position": "aboveBar", "color": "#d29922",
                            "shape": "arrowDown", "text": "AI Sell: " + e.get("label", "")})
        else:
            markers.append({"time": ts, "position": "aboveBar", "color": "#d29922",
                            "shape": "arrowDown", "text": "Sell Exit: " + e.get("label", "")})
    return markers


def _s7_rejections_to_markers(events):
    """v58.9 (item 11) — converts persisted Strategy 7 rejection events
    (agents.py's PriceActionAgent._record_s7_rejection, s7_rejected_
    events:{symbol}) into dim, distinct-from-confirmed-trades markers
    for the client's `lwSignalMarkers` layer (declared since v51 with
    "entries/exits/rejections" in its own comment, but never actually
    populated on either side before this — confirmed genuinely dead
    code, not a partially-wired feature). Same "convert the full
    persisted list every time" pattern as `_trade_events_to_markers`
    above, since these are already day-pruned and transition-detected
    at RECORD time (in PriceActionAgent) — no re-detection needed here.
    Deliberately dim/muted styling (small gray-ish square) so a near-
    miss reads as clearly secondary to a real trade marker, not
    competing with it visually."""
    markers = []
    for e in events or []:
        ts = int(e["time"] // 60) * 60
        gate = e.get("gate", "?")
        markers.append({"time": ts, "position": "inBar", "color": "#8b949e",
                        "shape": "square",
                        "text": f"S7 rejected \u2014 {gate} gate"})
    return markers


def _institutional_to_markers(institutional, anchor_ts=None, prev_active=None):
    """Converts Feature #5's institutional engine output (institutional
    _engine.institutional_output) into Lightweight Charts marker
    objects, distinct from (and not duplicating) _smart_money_to_
    markers() above — this covers breakout/breakdown validation and
    trap detection specifically, which live one level up from the
    per-strike smart_money events. Support/resistance shift markers
    are already handled by _smart_money_to_markers() from the same
    underlying smart_money data, so not repeated here.

    2026-07-25 — bug found live: markers always landed at wall-clock
    "now" regardless of symbol, so switching between symbols within
    the same ~10s window showed markers at nearly IDENTICAL
    timestamps, making them look like the same event repeated across
    every index rather than each symbol's own independent read (a live
    screenshot showed "False Breakout" at the same chart position for
    every symbol). Fixed: `anchor_ts` — the actual last real candle's
    timestamp FOR THIS SPECIFIC SYMBOL — is now required from the
    caller (which has that data) rather than defaulting to wall-clock
    time; falls back to `time.time()` only if the caller genuinely has
    no candle data at all (better than crashing, but callers should
    always have real data if the chart itself is showing anything).

    2026-07-27 — a SECOND, different bug behind the same "False
    Breakout stays visible all day" report, after the anchor-timestamp
    fix above (2026-07-27, part 1) turned out not to be the whole
    story: `breakout_validation()`'s classification can genuinely stay
    "False Breakout" for an extended stretch of a session — and this
    function ran on EVERY cycle of the signals websocket loop, so as
    long as the condition stayed active it kept emitting a FRESH marker
    at the CURRENT candle every single cycle. Each individual marker
    was correctly anchored (not stale), but visually this looked
    identical to "the same marker is stuck" — a new one just kept
    appearing right where the last one had been, following price
    forward all day. Fixed: now takes `prev_active` (this connection's
    own record of which events were already active last cycle) and
    only emits a marker on a genuine OFF->ON transition — a real new
    occurrence, not a continuous state re-announced every cycle.
    Returns (markers, new_active_state) so the caller can persist the
    state across its own loop iterations.
    """
    now_ts = int(anchor_ts) if anchor_ts is not None else int(time.time())
    prev_active = prev_active or {}
    new_active = {}
    markers = []

    def _rising_edge(key, active):
        new_active[key] = active
        return active and not prev_active.get(key, False)

    events = institutional.get("events_detail") or {}
    if _rising_edge("breakout_confirmation",
                    events.get("breakout_confirmation", {}).get("active", False)):
        markers.append({"time": now_ts, "position": "belowBar", "color": "#3fb950",
                        "shape": "arrowUp", "text": "Breakout Confirmed"})
    if _rising_edge("breakdown_confirmation",
                    events.get("breakdown_confirmation", {}).get("active", False)):
        markers.append({"time": now_ts, "position": "aboveBar", "color": "#f85149",
                        "shape": "arrowDown", "text": "Breakdown Confirmed"})
    if _rising_edge("false_breakout",
                    events.get("false_breakout", {}).get("active", False)):
        markers.append({"time": now_ts, "position": "aboveBar", "color": "#d29922",
                        "shape": "circle", "text": "False Breakout"})
    if _rising_edge("trap_formation",
                    events.get("trap_formation", {}).get("active", False)):
        markers.append({"time": now_ts, "position": "inBar", "color": "#d29922",
                        "shape": "square", "text": "Trap Formation"})
    return markers, new_active


def _classify_exit_reason(reason):
    """Classifies a trade's free-text exit `reason` string into one of
    the spec's own categories (Target Hit / Stop Loss Hit / generic
    Exit) by matching the actual prefixes ExecutionAgent's exit logic
    uses (read directly from that code before writing this — not
    guessed): "stoploss ("/"transaction stop loss (" -> Stop Loss Hit;
    "target-2 ("/"transaction target (" -> Target Hit. Everything else
    (spot invalidation, time stop, EOD square-off, manual exit,
    give-back-after-T1, step-trail floor) is a real, legitimate exit
    but doesn't cleanly map to either named category, so it stays a
    generic Exit rather than being force-fit into the wrong one."""
    r = (reason or "").lower()
    if r.startswith("stoploss") or r.startswith("transaction stop loss"):
        return "Stop Loss Hit"
    if r.startswith("target-2") or r.startswith("transaction target"):
        return "Target Hit"
    return "Exit"


def _trade_to_markers(trade, is_open=False):
    """Converts one trade record (the SAME dict shape ExecutionAgent
    already builds and persists via _append_trade/positions/spreads —
    no new fields, no new data) into Lightweight Charts Series Markers
    per explicit request: "Buy markers, Sell markers, Entry markers,
    Exit markers, AI Buy, AI Sell, Target Hit, Stop Loss Hit."

    Direction framing: this system only ever BUYS options (CE or PE),
    never sells single legs — so "Buy"/"Sell" here follows the
    DIRECTIONAL convention the spec's own equity/futures-style
    language implies: a CE entry (bullish bet) reads as Buy, a PE
    entry (bearish bet) reads as Sell — reusing the trade's own `leg`
    field, not a new signal. `manual` (already on every position
    dict) distinguishes AI-generated (manual=False, strategy signal)
    from user-initiated (manual=True) entries — AI Buy/AI Sell vs
    plain Buy/Sell. Spreads (leg="SPREAD") have no clean bullish/
    bearish framing, so they get generic Entry/Exit markers instead.
    """
    markers = []
    opened_ts = trade.get("opened_ts")
    leg = trade.get("leg")
    manual = trade.get("manual", True)

    if opened_ts:
        ts = int(opened_ts)
        if leg == "SPREAD":
            markers.append({"time": ts, "position": "belowBar", "color": "#58a6ff",
                            "shape": "arrowUp", "text": "Entry"})
        elif leg == "CE":
            markers.append({"time": ts, "position": "belowBar",
                            "color": "#3fb950" if manual else "#2ea043",
                            "shape": "arrowUp",
                            "text": "Buy" if manual else "AI Buy"})
        elif leg == "PE":
            markers.append({"time": ts, "position": "aboveBar",
                            "color": "#f85149" if manual else "#da3633",
                            "shape": "arrowDown",
                            "text": "Sell" if manual else "AI Sell"})

    if not is_open and trade.get("closed_at"):
        try:
            exit_dt = agents.datetime.fromisoformat(trade["closed_at"])
            exit_ts = int(exit_dt.timestamp())
        except (ValueError, TypeError):
            exit_ts = None
        if exit_ts:
            category = _classify_exit_reason(trade.get("reason"))
            if category == "Target Hit":
                markers.append({"time": exit_ts, "position": "aboveBar",
                                "color": "#3fb950", "shape": "circle",
                                "text": "Target Hit"})
            elif category == "Stop Loss Hit":
                markers.append({"time": exit_ts, "position": "belowBar",
                                "color": "#f85149", "shape": "square",
                                "text": "Stop Loss Hit"})
            else:
                markers.append({"time": exit_ts, "position": "inBar",
                                "color": "#8c8c8c", "shape": "circle",
                                "text": "Exit"})
    return markers


def _get_trade_markers_for_symbol(symbol, since_ts):
    """Assembles Series Markers for one symbol from real trade
    lifecycle events — closed trades (agents.load_persisted_trades(),
    already the authoritative on-disk record, written immediately on
    close) plus any CURRENTLY OPEN position/spread for that symbol
    (an Entry marker with no matching exit yet). No new persistence,
    reuses exactly what ExecutionAgent already maintains."""
    markers = []
    for t in agents.load_persisted_trades():
        if t.get("symbol") != symbol:
            continue
        if t.get("opened_ts") and t["opened_ts"] < since_ts:
            continue
        markers.extend(_trade_to_markers(t, is_open=False))

    positions = pilot.bus.get("positions", {}) or {}
    if symbol in positions:
        markers.extend(_trade_to_markers(positions[symbol], is_open=True))
    spreads = pilot.bus.get("spreads", {}) or {}
    for sp in spreads.values():
        if sp.get("symbol") == symbol:
            markers.extend(_trade_to_markers(sp, is_open=True))
    return markers


def _smart_money_to_markers(sm, anchor_ts=None, prev_active=None):
    """Converts Feature #4's smart_money engine output (analyzer.
    smart_money_engine) into Lightweight Charts marker objects
    (aboveBar/belowBar/inBar, arrowUp/arrowDown/circle/square, color,
    text) for the chart overlay — per explicit request ("update it
    signals on charts").

    2026-07-25 — same fix as _institutional_to_markers() above:
    `anchor_ts` (the actual last candle time for THIS symbol) replaces
    the previous always-"now" wall-clock timestamp, which made markers
    for every symbol land at nearly identical positions regardless of
    which chart was actually showing.

    2026-07-27 — this function's own docstring already said it plainly:
    "current-state flags recomputed each cycle" — the exact same
    "stays visible all day" problem found and fixed for
    _institutional_to_markers() applies here too (the smart_money
    engine's own output is itself a per-cycle snapshot comparison, so
    a strike sitting in e.g. strong_call_writing for several
    consecutive cycles would re-emit a marker every single cycle).
    Fixed the same way, but keyed by a composite (event type + the
    identifying detail, e.g. "strong_call_writing:23900") rather than
    one boolean per category, since this function has a LIST of
    entries per event type, not a single flag. Returns
    (markers, new_active_state) so the caller can persist state across
    its own loop iterations, same contract as _institutional_to_
    markers().
    """
    now_ts = int(anchor_ts) if anchor_ts is not None else int(time.time())
    prev_active = prev_active or {}
    new_active = {}
    markers = []

    def _rising_edge(key):
        new_active[key] = True
        return key not in prev_active

    for e in sm.get("strong_call_writing", [])[:3]:
        if _rising_edge(f"strong_call_writing:{e['strike']}"):
            markers.append({"time": now_ts, "position": "aboveBar", "color": "#f85149",
                            "shape": "arrowDown", "text": f"Strong CE Writing {e['strike']}"})
    for e in sm.get("strong_put_writing", [])[:3]:
        if _rising_edge(f"strong_put_writing:{e['strike']}"):
            markers.append({"time": now_ts, "position": "belowBar", "color": "#3fb950",
                            "shape": "arrowUp", "text": f"Strong PE Writing {e['strike']}"})
    if sm.get("resistance_shift"):
        rs = sm["resistance_shift"]
        key = f"resistance_shift:{rs['from']}-{rs['to']}"
        if _rising_edge(key):
            markers.append({"time": now_ts, "position": "aboveBar", "color": "#f85149",
                            "shape": "square", "text": f"R shift {rs['from']}\u2192{rs['to']}"})
    if sm.get("support_shift"):
        ss = sm["support_shift"]
        key = f"support_shift:{ss['from']}-{ss['to']}"
        if _rising_edge(key):
            markers.append({"time": now_ts, "position": "belowBar", "color": "#3fb950",
                            "shape": "square", "text": f"S shift {ss['from']}\u2192{ss['to']}"})
    for e in sm.get("volume_breakout", [])[:3]:
        key = f"volume_breakout:{e['strike']}{e['leg']}"
        if _rising_edge(key):
            markers.append({"time": now_ts, "position": "inBar", "color": "#d29922",
                            "shape": "circle", "text": f"Vol breakout {e['strike']}{e['leg']}"})
    for e in sm.get("aggressive_buyers", [])[:2]:
        key = f"aggressive_buyers:{e['strike']}"
        if _rising_edge(key):
            markers.append({"time": now_ts, "position": "belowBar", "color": "#3fb950",
                            "shape": "arrowUp", "text": f"Aggressive buyers {e['strike']}"})
    for e in sm.get("aggressive_writers", [])[:2]:
        key = f"aggressive_writers:{e['strike']}"
        if _rising_edge(key):
            markers.append({"time": now_ts, "position": "aboveBar", "color": "#f85149",
                            "shape": "arrowDown", "text": f"Aggressive writers {e['strike']}"})
    return markers[:8], new_active


def _atr_series(candles, period=14):
    """Full ATR series (not just the latest value `mtf_confluence_
    strategy.atr()` returns — that function's own docstring says so
    explicitly, since position sizing only ever needs the latest
    reading). Reuses `mcs.true_range()` directly and replicates ATR's
    exact Wilder-smoothing recurrence from that same function, so this
    series' last value always matches what `atr()` itself would
    return for the same candles — verified identical before use, not
    assumed."""
    import mtf_confluence_strategy as mcs
    if len(candles) < period + 1:
        return [None] * len(candles)
    tr = mcs.true_range(candles)
    out = [None] * len(candles)
    a = sum(tr[1:period + 1]) / period
    out[period] = a
    for i in range(period + 1, len(candles)):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    return out


def _stoch_rsi_series(closes, rsi_period=14, stoch_period=14, k_smooth=3, d_period=3):
    """Full %K/%D series for Stochastic-of-RSI (genuinely different
    from `mcs.stochastic()`, which operates on price H/L/C) — same
    formula `technical_engine.stoch_rsi_engine()` already uses,
    evaluated across the whole series instead of only the latest
    point. Reuses `mcs.rsi()` for the underlying RSI values and `mcs.
    _sma_skip_none()` for the smoothing steps — no new indicator math,
    just not truncated to the last value."""
    import mtf_confluence_strategy as mcs
    rsi_series = mcs.rsi(closes, rsi_period)
    valid = [(i, v) for i, v in enumerate(rsi_series) if v is not None]
    if len(valid) < stoch_period + d_period + 1:
        return [None] * len(closes), [None] * len(closes)
    idxs = [i for i, _ in valid]
    vals = [v for _, v in valid]
    k_raw = [None] * len(vals)
    for i in range(stoch_period - 1, len(vals)):
        window = vals[i - stoch_period + 1:i + 1]
        hh, ll = max(window), min(window)
        k_raw[i] = 50.0 if hh == ll else (vals[i] - ll) / (hh - ll) * 100
    k_smoothed = mcs._sma_skip_none(k_raw, k_smooth)
    d_smoothed = mcs._sma_skip_none(k_smoothed, d_period)
    k_full, d_full = [None] * len(closes), [None] * len(closes)
    for pos, orig_idx in enumerate(idxs):
        k_full[orig_idx] = k_smoothed[pos]
        d_full[orig_idx] = d_smoothed[pos]
    return k_full, d_full


def _pane_series(candles):
    """Feature #7 multi-pane chart integration — per explicit request
    ("full synced multi-pane MACD/RSI/StochRSI/ATR layout"), computes
    full historical SERIES (not single latest values) for each
    sub-pane indicator. Every series here reuses an existing
    calculation already built for Feature #7's confirmation engines —
    `mcs.macd()`/`mcs.rsi()` already return full series; StochRSI/ATR
    needed the "full series" variants above since their existing
    engine functions only return the latest point (sufeer position-
    sizing/confirmation only needs the latest reading, but a chart
    pane needs the whole history). Returns {"macd_line": [...],
    "macd_signal": [...], "macd_hist": [...], "rsi": [...],
    "stoch_k": [...], "stoch_d": [...], "atr": [...]}, each a list of
    {"time": ts, "value": v} points."""
    if not candles or len(candles) < 60:
        return {}
    import mtf_confluence_strategy as mcs
    times = [c["time"] for c in candles]
    closes = [c["close"] for c in candles]

    def series(values):
        return [{"time": t, "value": round(v, 4)} for t, v in zip(times, values)
               if v is not None]

    out = {}
    macd_line, signal_line, hist = mcs.macd(closes)
    out["macd_line"] = series(macd_line)
    out["macd_signal"] = series(signal_line)
    out["macd_hist"] = [{"time": t, "value": round(v, 4),
                        "color": "#3fb95088" if v >= 0 else "#f8514988"}
                       for t, v in zip(times, hist) if v is not None]

    out["rsi"] = series(mcs.rsi(closes))

    stoch_k, stoch_d = _stoch_rsi_series(closes)
    out["stoch_k"] = series(stoch_k)
    out["stoch_d"] = series(stoch_d)

    out["atr"] = series(_atr_series(candles))
    return out


def _in_market_session(ts):
    """True if `ts` falls inside NSE/BSE trading hours (Mon-Fri,
    09:15-15:30 IST, with a few minutes' tail for the closing auction).

    Added 2026-07-26 as read-side defence for the indicator path: the
    candles table contains flat keepalive bars persisted at weekend/
    evening timestamps before MarketDataAgent._build_candle was gated on
    market_open(). Those bars are not trades and must not reach the
    indicator math (a run of identical closes drives ATR to ~0 and
    flatlines every oscillator — visible in a live screenshot) or the
    chart's bar grid. Filtering at read time also means the already-
    contaminated history is neutralised without a risky destructive
    prune of the persisted table.

    2026-07-27 — now a thin wrapper around agents.in_market_session(),
    the single shared definition (moved there so history.py's new
    candle-pruning function can use the IDENTICAL logic rather than a
    second, potentially-drifting copy — this call sites here are
    unchanged, only the implementation moved).
    """
    return agents.in_market_session(ts)


def _indicator_candles(symbol, interval, display_candles, db_backed,
                       live_bars=None, cache=None):
    """Candle series to compute the chart's indicators on, plus the
    exact bar grid the chart is drawing.

    Added 2026-07-26 to replace a direct read of the `regime_candles:
    {symbol}` bus key, which had two defects: it only exists while
    RegimeAgent is running (market hours only), and it is always 5m
    bars regardless of the chart's selected interval — so on 1m/15m/60m
    the overlays and sub-panes were showing indicator values from a
    different timeframe than the candles they were drawn over.

    Revised later the same day after a live screenshot showed the panes
    drifting out of alignment with the price chart. Two causes, both
    handled here:

      1. `display_candles` is the history snapshot taken once at
         CONNECT time. Bars keep arriving after that (live ticks at 1m,
         RegimeAgent's persistence at 5m/15m), so indicators computed
         only from the snapshot fell progressively further behind the
         candles as a session ran — the panes visibly ended earlier
         than the price series. Now re-merged every cycle from the
         persisted table plus this connection's live bars.
      2. The caller needs the visible bar grid so indicator series can
         be padded to it (see _clip_series) — Lightweight Charts syncs
         panes by LOGICAL index, so a series that starts 34 bars later
         than the candles (MACD's warm-up) puts logical 0 on a
         different bar and shifts that whole pane.

    Returns (candles_for_indicators, visible_bar_times, note).
    """
    if not display_candles:
        return [], [], (f"no candles available for {symbol} at {interval}m, "
                        f"so there is nothing to compute indicators over")
    visible_from = display_candles[0]["time"]
    # Merge by timestamp. The connect-time snapshot wins for bars it
    # already has (it went through the tiered fallback and its source is
    # known good / non-degenerate); the DB and live feed contribute bars
    # that have appeared since.
    merged = {c["time"]: c for c in display_candles}
    # 2026-07-26 — `cache` is per-websocket-connection state, added to
    # fix a read-amplification regression this function caused. The
    # first version re-read the warm-up window AND up to 2000 rows from
    # the candles table on EVERY 30s refresh cycle. With ~2 years of
    # candles persisted and ~14 agents writing, that was enough to tip
    # SQLite into a lock storm ("database is locked" across regime,
    # technical, market_data and backtest — confirmed from a live log,
    # absent in the preceding 11 days). Now: warm-up bars are read once
    # per connection (they cannot change), and new bars are pulled
    # incrementally from the newest bar already held.
    if cache is None:
        cache = {}
    warm = cache.get("warm") or []
    warm_err = cache.get("warm_err")
    if db_backed:
        sec = f"{symbol}_SPOT_{interval}m"
        try:
            import history
            if "warm" not in cache:
                cache["warm"] = warm = [
                    c for c in history.candles_before(sec, visible_from, 400)
                    if _in_market_session(c["time"])]
            newest = cache.get("newest") or max(merged)
            for c in history.candles_since(sec, newest, 500):
                if not _in_market_session(c["time"]):
                    continue      # keepalive contamination — see helper
                if c["time"] >= visible_from and c["time"] not in merged:
                    merged[c["time"]] = c
                cache.setdefault("extra", {})[c["time"]] = c
            for t, c in (cache.get("extra") or {}).items():
                merged.setdefault(t, c)
        except Exception as e:
            # Fail loud, not silent: a DB problem here degrades
            # indicator warm-up and the person should know that's why,
            # rather than silently getting fewer indicators.
            warm_err = cache["warm_err"] = f"{type(e).__name__}: {e}"
    for t, c in (live_bars or {}).items():
        if t >= visible_from and _in_market_session(t):
            merged[t] = c
    visible = [merged[t] for t in sorted(merged)]
    bar_times = [c["time"] for c in visible]
    if bar_times:
        cache["newest"] = bar_times[-1]
    combined = warm + visible
    if len(combined) >= 60:
        note = None
    else:
        note = (f"only {len(combined)} {interval}m candles available "
                f"({len(visible)} on the chart"
                + (f" + {len(warm)} earlier bars for warm-up" if warm else "")
                + f") — indicators need at least 60")
        if not db_backed:
            note += (f"; {interval}m bars aren't persisted to the local DB, "
                     f"so no earlier bars are available to warm them up")
        elif not warm:
            note += ("; no earlier bars are persisted for this symbol yet, "
                     "so warm-up history will build up as sessions run")
    if warm_err:
        note = ((note + "; ") if note else "") + \
               f"warm-up candle read failed — {warm_err}"
    return combined, bar_times, note


def _clip_series(series_map, bar_times):
    """Align every indicator series to the chart's visible bar grid.

    Does two things at once:

      - Drops indicator points before the first visible bar. The warm-up
        bars added by _indicator_candles() exist only to give the
        indicators a run-up; drawing their output would stretch the time
        scale back into prior sessions that have no candles.
      - Pads each series with WHITESPACE points ({"time": t} and no
        value) for visible bars where that indicator has no value yet.

    The padding is the fix for the misalignment seen live on
    2026-07-26: the panes are synced to the price chart with
    setVisibleLogicalRange, which works in LOGICAL (data-index) space,
    not time. Each indicator drops its own warm-up region — MACD(12,26,9)
    has no value until bar ~34, StochRSI ~31, RSI/ATR ~14, candles from
    bar 0 — so logical index 0 meant a DIFFERENT bar in every pane, and
    propagating one chart's logical range to the others shifted each by a
    different amount. The visible symptom was every pane's data ending at
    a different x position, and the synced crosshair therefore landing on
    a different time in each pane even though the sync itself passes a
    time. Whitespace points make every series span the identical time
    range as the candles, so logical index N is the same bar everywhere,
    while not drawing anything for the warm-up region.
    """
    if not bar_times:
        return series_map or {}
    visible_from = bar_times[0]
    out = {}
    for key, points in (series_map or {}).items():
        by_time = {p["time"]: p for p in points
                   if p.get("time", 0) >= visible_from}
        if not by_time:
            # Nothing computable anywhere in view — send nothing at all
            # rather than an entirely blank whitespace series.
            continue
        out[key] = [by_time.get(t) or {"time": t} for t in bar_times]
    return out


def _levels_fallback(symbol, display_candles):
    """Rebuild the merged R1-R3/S1-S3 levels on demand when
    RegimeAgent hasn't published `levels:{symbol}` (it is idle outside
    market hours, and hasn't run yet right after a restart).

    Uses the SAME support_resistance.build_levels() call RegimeAgent
    itself uses, so the chart can't show a different level set than
    the rest of the app. Every input comes from a non-market-gated
    source: TechnicalAgent's `analysis:{symbol}` for the OI walls,
    `chain:{symbol}` for spot, history.py's persisted daily_ohlc for
    previous-day levels.

    Returns (levels_or_None, source_label).
    """
    try:
        import support_resistance as sr
        analysis = pilot.bus.get(f"analysis:{symbol}") or {}
        chain = pilot.bus.get(f"chain:{symbol}") or {}
        spot = chain.get("spot") or analysis.get("spot")
        future_ohlc = pilot.bus.get(f"future_ohlc:{symbol}") or {}
        levels = sr.build_levels(analysis, spot, display_candles or [],
                                 symbol=symbol,
                                 future_vwap=future_ohlc.get("vwap"))
        return levels, "computed_on_demand"
    except Exception as e:
        pilot.bus.log("execution", f"⚠ chart websocket: on-demand levels "
                      f"rebuild failed for {symbol} — "
                      f"{type(e).__name__}: {e}")
        return None, "error"


def _levels_unavailable_reason(symbol):
    """Plain-language reason the Key Levels panel is empty, so the
    chart shows a cause instead of an indefinite "Loading...". Names
    the specific missing input rather than a generic failure."""
    analysis = pilot.bus.get(f"analysis:{symbol}") or {}
    chain = pilot.bus.get(f"chain:{symbol}") or {}
    if not chain.get("spot") and not analysis.get("spot"):
        return (f"no spot price for {symbol} yet — the option chain hasn't "
                f"loaded, so support/resistance can't be positioned "
                f"relative to price")
    if not analysis.get("signal_lines"):
        return (f"no OI-wall levels for {symbol} yet — the option chain "
                f"analysis (TechnicalAgent) hasn't produced signal_lines, "
                f"which are the primary support/resistance source")
    return (f"levels could not be built for {symbol} from the available "
            f"data — see the activity log for details")


def _zigzag_series(candles, deviation_pct=0.5):
    """Delegates to structure.zigzag_series() — moved there in v51 so
    Strategy 7's structure gate and this chart overlay share ONE
    implementation (see structure.py's module docstring). Name kept so
    existing callers and tests are untouched."""
    import structure
    return structure.zigzag_series(candles, deviation_pct)


def _indicator_overlays(candles):
    """Feature #7 chart integration — per explicit request ("candle
    and strategies indicators on the chart"), computes overlay LINE
    SERIES (not just the latest single value) for the Lightweight
    Chart: EMA20/EMA50, Supertrend, and Bollinger upper/lower bands.
    Reuses existing calculations throughout — `mtf_confluence_
    strategy.ema()` already returns a full series aligned to closes;
    `market_bias.supertrend()` already returns a full (trend, direction)
    series; Bollinger bands reuse the identical rolling-mean/stdev
    formula `bollinger_percent_b()`/`technical_engine.bollinger_engine()`
    already use, just evaluated at every index instead of only the
    latest one. Returns {"ema20": [...], "ema50": [...], "supertrend":
    [...], "bb_upper": [...], "bb_lower": [...]}, each a list of
    {"time": ts, "value": v} points (Lightweight Charts' line-series
    format), skipping indices where the underlying indicator isn't
    computable yet (warm-up period)."""
    if not candles or len(candles) < 60:
        return {}
    import mtf_confluence_strategy as mcs
    import market_bias as mb
    times = [c["time"] for c in candles]
    closes = [c["close"] for c in candles]

    def series(values):
        return [{"time": t, "value": round(v, 2)} for t, v in zip(times, values)
               if v is not None]

    out = {}
    out["ema20"] = series(mcs.ema(closes, 20))
    out["ema50"] = series(mcs.ema(closes, 50))

    trend, _direction = mb.supertrend(candles, 10, 3.0)
    out["supertrend"] = series(trend)

    # ATR Bands, per explicit request ("evaluate whether the Bands
    # plugin can be reused for ATR Bands, Volatility Bands, Dynamic
    # Support Zones"). Reuses _atr_series() directly (already built
    # and verified byte-identical to the existing single-value atr()
    # for the MACD/RSI/StochRSI/ATR pane work) — the envelope is
    # simply close ± (ATR × multiplier), no new volatility calculation.
    atr_mult = 1.5
    atr_vals = _atr_series(candles)
    atr_upper = [c + a * atr_mult if a is not None else None
                for c, a in zip(closes, atr_vals)]
    atr_lower = [c - a * atr_mult if a is not None else None
                for c, a in zip(closes, atr_vals)]
    out["atr_band_upper"] = series(atr_upper)
    out["atr_band_lower"] = series(atr_lower)

    # Volatility Bands — per the same "evaluate ATR Bands, Volatility
    # Bands, Dynamic Support Zones" request. Deliberately a DIFFERENT
    # statistical basis from ATR Bands above: ATR measures true RANGE
    # (high-low-close spread per bar); this measures the standard
    # deviation of period-over-period % RETURNS — a genuinely distinct
    # volatility read (a market can have wide bars but calm returns,
    # or the reverse), not the same number relabeled. Window=20,
    # matching Bollinger's own convention for consistency across the
    # chart's volatility measures.
    vol_period = 20
    returns = [None] + [(closes[i] - closes[i - 1]) / closes[i - 1]
                        for i in range(1, len(closes))]
    vol_upper, vol_lower = [None] * len(closes), [None] * len(closes)
    for i in range(vol_period, len(closes)):
        window = [r for r in returns[i - vol_period + 1:i + 1] if r is not None]
        if len(window) < vol_period - 1:
            continue
        mean_r = sum(window) / len(window)
        var_r = sum((r - mean_r) ** 2 for r in window) / len(window)
        std_r = var_r ** 0.5
        vol_upper[i] = closes[i] * (1 + std_r * 2)
        vol_lower[i] = closes[i] * (1 - std_r * 2)
    out["vol_band_upper"] = series(vol_upper)
    out["vol_band_lower"] = series(vol_lower)

    # Dynamic Support Zone — per the same request. Reuses the ZigZag
    # pivots (_zigzag_series(), already built) rather than any new
    # swing-detection math: a "zone" band around the MOST RECENT
    # confirmed low pivot, width scaled by ATR (reusing atr_vals
    # already computed two paragraphs up) so the zone is wider in
    # volatile conditions and tighter in calm ones. Plotted as a flat
    # band from the pivot's own time forward to "now" (a genuine
    # support zone persists until price breaks it, not just at one
    # instant) rather than a single point.
    pivots = _zigzag_series(candles)
    low_pivots = [p for p in pivots if p["type"] == "low"]
    if low_pivots and atr_vals[-1]:
        last_low = low_pivots[-1]
        zone_width = atr_vals[-1] * 0.5
        pivot_idx = times.index(last_low["time"]) if last_low["time"] in times else None
        if pivot_idx is not None:
            zone_times = times[pivot_idx:]
            out["support_zone_upper"] = [{"time": t, "value": round(last_low["price"] + zone_width, 2)}
                                         for t in zone_times]
            out["support_zone_lower"] = [{"time": t, "value": round(last_low["price"] - zone_width, 2)}
                                         for t in zone_times]

    period, mult = 20, 2.0
    upper, lower = [None] * len(closes), [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        basis = sum(window) / period
        var = sum((c - basis) ** 2 for c in window) / period
        dev = mult * (var ** 0.5)
        upper[i], lower[i] = basis + dev, basis - dev
    out["bb_upper"] = series(upper)
    out["bb_lower"] = series(lower)

    # ATR Bands, per explicit request ("evaluate whether the official
    # Bands plugin can be reused for ATR Bands"). Reviewed the plugin
    # (github.com/tradingview/lightweight-charts/tree/master/plugin-
    # examples/src/plugins/bands-indicator) — it's a custom Pane
    # Primitive renderer for a fill-between-two-lines visual. This
    # codebase's Bollinger Bands (just above) already achieves the
    # same visual/functional result — two overlay line series, shaded
    # region via a translucent fill on the chart — with the SAME
    # simple pattern already proven and tested here, so ATR Bands
    # reuses that established pattern rather than introducing the
    # plugin's more complex custom-renderer machinery for an
    # equivalent result. Classic ATR/Keltner-style construction: EMA20
    # basis (already computed above, not recomputed) ± ATR × multiplier
    # — reuses the EMA series directly and the SAME ATR series function
    # (`_atr_series`) built for the ATR sub-pane, not a third
    # implementation of either.
    ema20_raw = mcs.ema(closes, 20)
    atr_raw = _atr_series(candles, 14)
    atr_mult = 2.0
    atr_upper = [e + a * atr_mult if e is not None and a is not None else None
                for e, a in zip(ema20_raw, atr_raw)]
    atr_lower = [e - a * atr_mult if e is not None and a is not None else None
                for e, a in zip(ema20_raw, atr_raw)]
    out["atr_band_upper"] = series(atr_upper)
    out["atr_band_lower"] = series(atr_lower)
    return out


def ws_alive(websocket) -> bool:
    """Is this websocket still actually connected?

    2026-07-30 -- the fix for a continuous `socket.send() raised
    exception.` flood during a live session.

    The handler already caught WebSocketDisconnect, which looked
    sufficient and was not. Starlette's `send_json()` frequently does
    NOT raise when the peer has gone: the write fails down in the
    asyncio transport, which prints "socket.send() raised exception."
    and returns normally. So the exception handler never fired, the
    `while True` push loop never exited, and every orphaned connection
    kept writing once per second FOREVER. Each symbol or timeframe
    switch left another one behind, which is why the rate GREW across
    the session instead of appearing once.

    Relying on a failed write to detect a dead peer is the mistake.
    Starlette tracks the state explicitly; ask it.
    """
    # Only an EXPLICITLY disconnected peer is dead. An object without
    # these attributes is treated as alive, deliberately:
    #
    # The first version returned False on any AttributeError, which muted
    # every send to anything that was not a full Starlette WebSocket --
    # including test_chart_indicators' FakeWS stand-in, whose whole
    # purpose is to drive this endpoint deterministically. The suite went
    # from passing to receiving zero messages, and the endpoint would
    # have been silently mute for any future caller passing a wrapper.
    #
    # "Unknown state" is not evidence of disconnection. Failing closed
    # here costs real functionality to guard against a case that cannot
    # occur with a real Starlette socket, which always sets both.
    try:
        if getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED:
            return False
        if getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED:
            return False
        return True
    except Exception:
        return True


async def ws_send(websocket, payload) -> bool:
    """Send only if still connected. Returns False when the caller should
    stop pushing, so a dead connection is reaped on its next loop
    iteration instead of accumulating."""
    if not ws_alive(websocket):
        return False
    try:
        await websocket.send_json(payload)
        return True
    except Exception:
        return False


@app.websocket("/ws/candles/{symbol}")
async def ws_candles(websocket: WebSocket, symbol: str, interval: str = "1"):
    """FastAPI WebSocket Server, per the requested architecture:
    DhanHQ WS -> Market Data Service -> Candle Builder ->
    **FastAPI WebSocket Server** -> TradingView Lightweight Charts.

    2026-07-25 — added `interval` query param (?interval=1/5/15/60) per
    explicit request for a timeframe selector matching TradingView's
    own 5m/15m/30m/1h buttons. 1m/5m/15m are already persisted to the
    DB (RegimeAgent's `_persist_candles`/`history.upsert_index_
    candles` — reused directly, no new write path). 60m ("1h") isn't
    persisted anywhere in this codebase, so that interval skips the DB
    tiers entirely and goes straight to a live REST call (Dhan's
    `intraday()` already accepts arbitrary interval strings) — a
    disclosed, deliberate gap rather than a silently degraded 1h view.

    On connect: sends historical candles for today from history.py's
    candles table (persisted by MarketDataAgent's _build_candle for
    1m, reusing the existing schema, security_id convention
    "{symbol}_SPOT_{interval}m"), so the chart isn't empty on load.
    Then streams the live-forming candle (bus key live_candle:{symbol},
    updated on every tick by _build_candle — 1m-only regardless of the
    selected chart interval, a real disclosed limitation: tick-by-tick
    live updates only make sense at the finest granularity; 5m/15m/60m
    views show historical bars without a live-updating current bar)
    every ~1s — Lightweight Charts' update() call handles both "still
    the current bar" (in-place update) and "a new bar started" (append)
    from the same message shape, so no separate "candle closed" event
    type is needed on the wire.
    """
    # v58.74 — the HTTP middleware cannot see a websocket handshake
    # (Starlette runs http middleware on the http scope only), so an
    # unauthenticated client could stream live prices from a protected
    # app through this one route. Checked explicitly here; the cookie
    # rides the handshake like any other request.
    if config.load().get("auth_enabled", False):
        if not auth.session(websocket.cookies.get(SESSION_COOKIE)):
            await websocket.close(code=1008)     # policy violation
            return

    import asyncio
    import history
    symbol = symbol.upper()
    interval = str(interval)
    if interval not in ("1", "5", "15", "60"):
        interval = "1"
    db_backed_interval = interval in ("1", "5", "15")
    await websocket.accept()
    try:
        today = agents.now_ist().strftime("%Y-%m-%d")
        today_start = int(agents.datetime.strptime(today, "%Y-%m-%d")
                          .replace(tzinfo=agents.IST).timestamp())
        history_payload = []
        rows = []
        db_error = None
        if db_backed_interval:
            security_id = f"{symbol}_SPOT_{interval}m"
            # 2026-07-27 — real gap found from a live report ("candles
            # older than the current day are not visible... it should
            # have all the candles in DB"): this query used to hard-cut
            # at `today_start`, discarding any older rows even though
            # they genuinely exist — the per-tick candle builder
            # (_build_candle in agents.py) runs continuously as part of
            # the server's own tick processing, independent of whether
            # any browser is connected, and persists every completed
            # minute to this exact security_id. Nothing ever prunes it.
            # The DB has real multi-day (potentially weeks/months of)
            # history sitting here; the chart just never asked for it.
            #
            # Widened to a configurable, interval-scaled lookback
            # instead of "today only" — scaled per interval so payload
            # size stays reasonable (a 1-minute candle count over N
            # days is ~15x a 15-minute count over the same N days, so
            # 1m gets a shorter default window than 15m for a
            # comparable total candle count).
            cfg_days = config.load()
            lookback_days = cfg_days.get(f"chart_history_days_{interval}m",
                                         {"1": 5, "5": 20, "15": 60}.get(interval, 5))
            history_cutoff = today_start - lookback_days * 86400
            # 2026-07-26 — was unprotected. A transient
            # sqlite3.OperationalError ("database is locked") here
            # propagated to this handler's outer `except`, which sends a
            # single {"type":"error"} and then ENDS the connection — so
            # one lock contention left the chart completely blank, with
            # levels stuck on "Loading..." and no indicators, rather than
            # falling through to the bus/REST/most-recent-session tiers
            # that exist precisely for this. Confirmed against a live log
            # where locks hit every persistence path within 3 minutes of
            # a restart. Now captured into diag like the REST tier's own
            # error, so the reason is visible instead of fatal.
            try:
                conn = history._conn()
                rows = conn.execute(
                    """SELECT ts, o, h, l, c FROM candles
                       WHERE security_id=? AND ts>=? ORDER BY ts""",
                    (security_id, history_cutoff)).fetchall()
                conn.close()
                # 2026-07-27 — this query now spans multiple days
                # (widened from "today only" above), which reopens a
                # documented, pre-existing risk: the candles table
                # still contains flat weekend/evening keepalive bars
                # persisted before _build_candle was gated on
                # market_open() (2026-07-26). _in_market_session()
                # already exists as read-side defence for exactly this
                # (used for the indicator path) — applied here too now
                # that this query can reach back far enough to
                # encounter that older contaminated data.
                history_payload = [{"time": r[0], "open": r[1], "high": r[2],
                                   "low": r[3], "close": r[4]} for r in rows
                                  if _in_market_session(r[0])]
            except Exception as e:
                rows = []
                history_payload = []
                db_error = f"{type(e).__name__}: {e}"

        def _is_degenerate(candles):
            """2026-07-25 — real root cause found from a live capture:
            the candle-builder DB tier can accumulate a long, sparse
            series of candles that all report the EXACT SAME price
            (e.g. 55 candles spanning ~11 hours, every single one
            open==high==low==close==23767.45). This happens when the
            live websocket tick feed re-broadcasts the last known LTP
            as a keepalive/reconnect artifact rather than a genuine
            trade — each sparse tick lands in a different 1-minute
            bucket, so _build_candle() persists a "new" candle each
            time, but every one is a single flat point, not real price
            action. This tier technically isn't EMPTY (so the original
            `if not history_payload` check let it through), but it's
            USELESS for a chart and — worse — it permanently blocked
            the REST fallback below, which reliably returns real
            varying OHLC (confirmed directly: the legacy Price Chart
            panel, which only ever uses that same REST call, showed
            genuine movement while this chart showed a flat line).
            Detects "no real price variation anywhere in this set" and
            treats it the same as empty, so the pipeline correctly
            falls through to a more trustworthy tier instead of
            accepting degenerate data just because it's non-empty."""
            if not candles:
                return False
            closes = {c.get("close") for c in candles}
            return len(closes) <= 1

        db_today_degenerate = _is_degenerate(history_payload)
        if db_today_degenerate:
            history_payload = []
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
        fallback_source = f"candle_builder_{interval}m"
        diag = {"db_candles_found": len(rows), "regime_candles_found": 0,
               "regime_candles_today": 0, "rest_error": None,
               "db_today_degenerate": db_today_degenerate,
               "db_error": db_error}

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

        # 2026-07-25 — regression found from a live report ("candles
        # were loading initially when using in-memory/regime data,
        # then stopped"): the DB "most recent session" tiers were
        # placed BEFORE the bus/REST live tiers. If the DB happened to
        # have accumulated a few candles earlier in the session and
        # then stalled (an agent restart, a gap in persistence, etc.),
        # THAT stale data would win here and permanently block the
        # fresher bus/REST tiers below from ever being tried — since
        # each tier only runs `if not history_payload`. Reordered so
        # every genuinely LIVE source (today's DB, the in-memory bus,
        # a live REST call) is tried first; the DB's "most recent
        # session" — which could be hours or days old — is now the
        # LAST resort, used only when nothing live is available at all
        # (no Dhan client configured, REST unreachable, etc).
        # Bus fallback: regime_candles:{symbol} is ALWAYS 5m bars
        # (RegimeAgent's own fetch cadence) — only useful as a seed
        # when the user actually selected the 5m timeframe; wrong
        # granularity for 1m/15m/60m views, so skipped for those.
        if not history_payload and interval == "5":
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
        # reused directly here as the most reliable fallback. Now uses
        # the actual SELECTED interval (was hardcoded to "1" before
        # the timeframe selector existed) — this is also the ONLY tier
        # that works at all for 60m ("1h"), which isn't persisted
        # anywhere in the DB.
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
                    data = d.intraday(symbol, interval)
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
        # Last resort: the DB's "most recent session" — could be hours
        # or days old (whatever RegimeAgent last persisted before the
        # gap that got us here). Only reached if NOTHING live worked
        # at all (no Dhan client, REST unreachable, bus empty) — moved
        # here from being tier 1 specifically because of the regression
        # noted above. Same degenerate-data check as tier 1 applies
        # here too — this reads the same `candles` table and is
        # equally susceptible to the flat-repeated-tick problem. Skips
        # entirely for 60m (nothing persisted at that granularity).
        if not history_payload and db_backed_interval:
            try:
                most_recent = history.most_recent_session_candles(
                    f"{symbol}_SPOT_{interval}m")
            except Exception as e:      # same lock exposure as tier 1
                most_recent = []
                diag["db_most_recent_error"] = f"{type(e).__name__}: {e}"
            diag["db_most_recent_found"] = len(most_recent)
            if most_recent and not _is_degenerate(most_recent):
                history_payload = most_recent
                used_fallback = True
                fallback_source = f"db_{interval}m_most_recent_session"
            elif most_recent:
                diag["db_most_recent_degenerate"] = True
        if not history_payload and interval != "5":
            # Cross-timeframe last resort: a 5m most-recent-session
            # exists even if the requested interval's own DB tier came
            # up empty/degenerate — coarser data beats a blank chart.
            try:
                most_recent_5m = history.most_recent_session_candles(
                    f"{symbol}_SPOT_5m")
            except Exception as e:
                most_recent_5m = []
                diag["db_5m_most_recent_error"] = f"{type(e).__name__}: {e}"
            diag["db_5m_most_recent_found"] = len(most_recent_5m)
            if most_recent_5m and not _is_degenerate(most_recent_5m):
                history_payload = most_recent_5m
                used_fallback = True
                fallback_source = "db_5m_most_recent_session"
                # 2026-07-28 — real bug found from a live screenshot:
                # the indicator overlays/panes visibly "started late"
                # (MACD/RSI blank for the first couple hours of an
                # otherwise-full session) specifically on the 60m view
                # whenever it fell back to this tier. Root cause: this
                # tier delivers genuine 5m-granularity candles, but
                # `interval`/`db_backed_interval` stayed at their
                # ORIGINALLY REQUESTED values ("60"/False) for the rest
                # of the connection — so _indicator_candles's `if
                # db_backed:` warm-up branch (400 prior bars from the
                # DB) was skipped entirely, even though 5m data genuinely
                # HAS a DB warm-up tier available. MACD/RSI need ~26-34
                # bars of lookback before producing a first value; with
                # zero warm-up and only the visible session's own bars
                # to work from, that lookback ate into the visible
                # range itself, visually delaying the indicators by
                # exactly that many bars. Updating both variables here
                # to reflect what was ACTUALLY delivered (not what was
                # originally asked for) fixes the warm-up path AND the
                # `"interval"` field reported to the client below.
                interval = "5"
                db_backed_interval = True
            elif most_recent_5m:
                diag["db_5m_most_recent_degenerate"] = True
        if not history_payload:
            pilot.bus.log("execution", f"⚠ chart websocket: no candles found for "
                          f"{symbol} (interval={interval}) from any source — {diag}")

        # Volume — per explicit instruction ("Volume is NOT optional").
        # Merges the persisted FUTURES volume series (the only real
        # volume source; the index itself has none) onto the price
        # candles by matching timestamp. Only meaningful at 1m (that's
        # the granularity _build_volume_candle persists at); coarser
        # intervals get no volume rather than a fabricated aggregate —
        # a disclosed gap, not silently faked data. Reuses history.
        # get_volume_history() directly — no new API call, this is the
        # SAME futures quote poll already fetched every cycle for the
        # Futures panel.
        if history_payload and interval == "1":
            try:
                vol_map = history.get_volume_history(f"{symbol}_FUT_1m",
                                                      history_payload[0]["time"])
                for c in history_payload:
                    c["volume"] = vol_map.get(c["time"])
                diag["volume_points_found"] = len(vol_map)
            except Exception as e:
                diag["volume_error"] = f"{type(e).__name__}: {e}"

        await ws_send(websocket, {"type": "history", "candles": history_payload,
                                   "source": fallback_source, "interval": interval,
                                   "diagnostics": diag})

        last_sent = None
        no_live_ticks_since = time.time()
        warned_no_live = False
        last_levels_sent = None
        last_signals_ts = 0
        last_overlays_ts = 0
        # Warn-once flags for the 2026-07-26 "stuck on Loading..." fix —
        # the reason is sent a single time per connection, not every
        # loop iteration.
        levels_warned = False
        indicators_warned = False
        indi_note = None
        # Bars that have arrived since the connect-time history snapshot.
        # Needed so the indicator series keeps pace with the candle
        # series — otherwise the panes end earlier than the price chart
        # and the logical-range sync misaligns them (see
        # _indicator_candles / _clip_series).
        live_bars = {}
        # Per-connection candle cache for the indicator path — keeps the
        # warm-up read to once per connection instead of every cycle.
        indi_cache = {}
        while True:
            # Reap a dead peer HERE rather than waiting for a
            # write to fail -- which, per ws_alive(), it may
            # never do.
            if not ws_alive(websocket):
                break
            # Live tick-by-tick streaming only makes sense for the 1m
            # view — live_candle:{symbol} is always built from 1m
            # ticks (MarketDataAgent._build_candle), so pushing it into
            # a 5m/15m/60m chart would misrepresent the current bar
            # (it'd look like a 1m-sized bar merging into a coarser
            # one). Those views show the historical bars from the
            # initial "history" message only, refreshed periodically
            # via reconnect/symbol-switch rather than live-updating —
            # a disclosed limitation, not silently wrong data.
            # 2026-07-26 — additionally gated on market_open(). Outside
            # trading hours the only "ticks" are keepalive re-broadcasts
            # of the last LTP (not trades); streaming them appended an
            # ever-growing flat bar tail at weekend timestamps after
            # Friday's real close — dragging the view onto the tail (the
            # chart opened looking blank, axis autoscaled to a 0.14-pt
            # window) and feeding the indicators fake flat bars. The
            # builder itself is now also gated (MarketDataAgent.
            # _build_candle), this is the second line of defence.
            live = (pilot.bus.get(f"live_candle:{symbol}")
                    if interval == "1" and agents.market_open() else None)
            if live and live != last_sent:
                await ws_send(websocket, {"type": "live", "candle": live})
                last_sent = live
                warned_no_live = False
                if live.get("time") is not None:
                    live_bars[live["time"]] = {
                        "time": live["time"], "open": live.get("open"),
                        "high": live.get("high"), "low": live.get("low"),
                        "close": live.get("close")}
            elif interval == "1" and not live and not warned_no_live and \
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
                await ws_send(websocket, {"type": "diagnostic",
                                          "message": msg_text})
            # 2026-07-25 — chart signal overlays, per explicit request
            # ("update it signals on charts"). Reuses data ALREADY
            # computed elsewhere (no new calculation here):
            #   levels: Feature #3's levels:{symbol} bus key (R1-R3/
            #     S1-S3, merged OI-wall + prev-day + VWAP), refreshed
            #     by RegimeAgent every 90s — sent only when it actually
            #     changed since last send, so the chart isn't redrawing
            #     identical price lines every second.
            #   signals: Feature #4's smart_money engine output PLUS
            #     Feature #5's institutional engine output (both inside
            #     analysis:{symbol}/institutional:{symbol}, refreshed
            #     every ~60s by TechnicalAgent) — converted into
            #     Lightweight Charts marker objects. Throttled
            #     independently to once every 10s (these events don't
            #     need per-second granularity and this avoids re-
            #     deriving markers on every loop iteration for no
            #     reason).
            #
            # 2026-07-26 — BUG FIX (reported live: "Key levels R1-R3/
            # S1-S3 still showing Loading..., all overlay and underlay
            # indicators not loaded", while the option-chain-fed Key
            # Levels ladder below the chart WAS populating). Root
            # cause: this block read `levels:{symbol}` and
            # `regime_candles:{symbol}` — both published ONLY by
            # RegimeAgent, which returns early when market_open() is
            # False (agents.py). Outside market hours neither key ever
            # exists, so all four chart features (levels, overlays,
            # panes, zigzag) stayed silent forever with no diagnostic,
            # while the ladder kept working because it reads
            # TechnicalAgent's `analysis:{symbol}`, which is NOT
            # market-gated. The candle history above already handles
            # market-closed correctly (most_recent_session + DB tiers);
            # only this indicator path didn't. Fixed below by giving
            # levels an on-demand fallback and by computing indicators
            # from the candles actually being DISPLAYED.
            levels = pilot.bus.get(f"levels:{symbol}")
            levels_source = "regime_agent"
            if not levels:
                levels, levels_source = _levels_fallback(symbol,
                                                         history_payload)
            if levels and levels != last_levels_sent:
                await ws_send(websocket, {"type": "levels", "levels": levels,
                                           "source": levels_source})
                last_levels_sent = levels
            elif not levels and not levels_warned:
                # Fail loud, not silent (project convention): say why
                # the panel is empty instead of leaving "Loading..."
                # on screen indefinitely.
                levels_warned = True
                await ws_send(websocket, {
                    "type": "unavailable", "what": "levels",
                    "reason": _levels_unavailable_reason(symbol)})
            if time.time() - last_signals_ts > 10:
                last_signals_ts = time.time()
                analysis = pilot.bus.get(f"analysis:{symbol}")
                institutional = pilot.bus.get(f"institutional:{symbol}")
                sm = (analysis or {}).get("smart_money")
                # 2026-07-25 — bug fixed: markers used to land at
                # wall-clock "now" for every symbol, so a quick switch
                # between symbols showed markers at nearly identical
                # positions regardless of which chart was actually
                # showing (a live screenshot showed "False Breakout" at
                # the same spot for every index). Anchor to THIS
                # symbol's own last real candle instead — `last_sent`
                # (the most recent live tick already tracked above) if
                # any has arrived this connection, else the last bar of
                # the historical seed sent at connect time.
                anchor_ts = None
                if last_sent:
                    anchor_ts = last_sent.get("time")
                elif history_payload:
                    anchor_ts = history_payload[-1].get("time")
                # 2026-07-27 — second-order bug from the 2026-07-25 fix
                # above: right after market open, before the first live
                # tick of the day has arrived AND before the 1m builder
                # has produced any bar for today, `history_payload[-1]`
                # is still YESTERDAY's final candle — anchoring a
                # CURRENT institutional/smart-money read to that stale
                # bar puts a "False Breakout" (or any other) marker
                # sitting on yesterday's session, which is exactly what
                # was reported live: the label visibly persisted well
                # after today's open. The 07-25 fix's reasoning (avoid
                # wall-clock "now" to stop markers colliding across a
                # quick symbol switch) doesn't apply here — there's no
                # collision risk in falling back to "now" specifically
                # when the only candle on hand predates today.
                if anchor_ts is not None:
                    from datetime import datetime as _dt
                    anchor_day = _dt.fromtimestamp(anchor_ts, tz=agents.IST).date()
                    if anchor_day != agents.now_ist().date():
                        anchor_ts = None
                # Trade event markers (Series Markers, per explicit
                # request) — real historical events, kept separate from
                # the capped current-state flags below since these
                # should always be visible, not truncated away by a
                # noisy option-chain cycle.
                trade_events = pilot.bus.get(f"chart_events:{symbol}", [])
                trade_markers = _trade_events_to_markers(trade_events)
                new_markers = []
                # 2026-07-27 (fix #2 to this same feature) — real bug
                # found from a live report: "False Breakout count
                # increases on every refresh." Root cause: the item-4
                # fix's transition-tracking state (institutional_
                # active_state/smart_money_active_state) was a PER-
                # CONNECTION local variable, reset to {} every time a
                # NEW websocket connects — i.e. every page refresh —
                # while the PERSISTED marker list it feeds
                # (institutional_events:{symbol}) is bus-shared and
                # survives across connections. Refreshing the page made
                # the still-ongoing condition look like a brand new
                # transition again from the fresh connection's point of
                # view, appending another marker to the shared list
                # every time. Fixed by moving the state itself to the
                # bus (per-symbol, shared across all connections,
                # exactly like the persisted markers already are) so a
                # refresh can no longer forget what was already seen.
                sm_state_key = f"smart_money_active_state:{symbol}"
                inst_state_key = f"institutional_active_state:{symbol}"
                today_state_str = agents.now_ist().date().isoformat()
                sm_state_wrapper = pilot.bus.get(sm_state_key, {})
                inst_state_wrapper = pilot.bus.get(inst_state_key, {})
                sm_prev_state = (sm_state_wrapper.get("state", {})
                                if sm_state_wrapper.get("day") == today_state_str else {})
                inst_prev_state = (inst_state_wrapper.get("state", {})
                                  if inst_state_wrapper.get("day") == today_state_str else {})
                if sm:
                    sm_markers, new_sm_state = _smart_money_to_markers(
                        sm, anchor_ts, sm_prev_state)
                    pilot.bus.set(sm_state_key, {"day": today_state_str, "state": new_sm_state})
                    new_markers += sm_markers
                if institutional:
                    inst_markers, new_inst_state = _institutional_to_markers(
                        institutional, anchor_ts, inst_prev_state)
                    pilot.bus.set(inst_state_key, {"day": today_state_str, "state": new_inst_state})
                    new_markers += inst_markers
                # 2026-07-27 — the transition-based fix above (only emit
                # a marker on a genuine OFF->ON edge, not every cycle a
                # condition stays true) created a NEW problem: the
                # client fully REPLACES its marker array on every
                # "signals" message rather than accumulating (confirmed
                # by reading connectLwChart's own handler before writing
                # this) — so a marker that only appears in ONE message
                # would flash for a few seconds and then vanish on the
                # very next message, instead of persisting on the chart
                # like a real historical event should. Fixed the same
                # way trade events already work correctly: persist any
                # NEWLY-fired marker to a bus key, day-pruned, and
                # resend the full day's accumulated history every cycle
                # — not just this cycle's transition.
                if new_markers:
                    persisted = pilot.bus.get(f"institutional_events:{symbol}", [])
                    today_str = agents.now_ist().date().isoformat()
                    persisted = [m for m in persisted if m.get("day") == today_str]
                    for m in new_markers:
                        persisted.append(dict(m, day=today_str))
                    pilot.bus.set(f"institutional_events:{symbol}", persisted[-30:])
                persisted_today = pilot.bus.get(f"institutional_events:{symbol}", [])
                today_str = agents.now_ist().date().isoformat()
                markers = [{k: v for k, v in m.items() if k != "day"}
                          for m in persisted_today if m.get("day") == today_str]
                markers = trade_markers + markers[-10:]
                # 2026-07-27 (item 11) — Strategy 7's own dedicated
                # marker layer (`lwSignalMarkers` client-side, declared
                # since v51 but never actually populated — confirmed
                # genuinely dead code on both ends). Sent as a SEPARATE
                # field, not merged into `markers` above, since the
                # client keeps this in its own distinct layer with its
                # own visibility toggle ("S7 Signals" checkbox).
                s7_events = pilot.bus.get(f"s7_rejected_events:{symbol}", [])
                s7_markers = _s7_rejections_to_markers(s7_events)
                if markers or s7_markers:
                    await ws_send(websocket, {"type": "signals",
                                               "markers": markers,
                                               "s7_markers": s7_markers})
            # Feature #7 chart integration — indicator overlay lines,
            # per explicit request ("candle and strategies indicators
            # on the chart"). Recomputed less often than levels/signals
            # (every 30s) since EMA/Supertrend/Bollinger don't shift
            # meaningfully bar-to-bar and this reuses regime_candles
            # (no new API call) each time.
            if time.time() - last_overlays_ts > 30:
                last_overlays_ts = time.time()
                # 2026-07-26 — was `pilot.bus.get(f"regime_candles:
                # {symbol}")`. Two defects with that:
                #   1. RegimeAgent-gated (see the levels note above), so
                #      every indicator was blank outside market hours.
                #   2. regime_candles is ALWAYS 5-minute bars. On the
                #      1m/15m/60m views the overlays and panes were
                #      therefore computing 5m EMA/MACD/RSI/ATR values
                #      and plotting them at 5m timestamps over bars of a
                #      different granularity — silently WRONG numbers,
                #      not merely misaligned. Only the 5m view was ever
                #      correct.
                # Both fixed by computing from the same candle series the
                # chart is drawing, warmed up with prior persisted bars.
                overlay_candles, bar_times, indi_note = _indicator_candles(
                    symbol, interval, history_payload, db_backed_interval,
                    live_bars, indi_cache)
                visible_from = bar_times[0] if bar_times else None
                if overlay_candles:
                    overlays = _clip_series(
                        _indicator_overlays(overlay_candles), bar_times)
                    if overlays:
                        await ws_send(websocket, {"type": "overlays",
                                                   "series": overlays})
                    # Multi-pane sub-chart data (MACD/RSI/StochRSI/ATR)
                    # — same throttle, same candle source, computed
                    # alongside the overlays in the same cycle rather
                    # than a separate fetch.
                    panes = _clip_series(_pane_series(overlay_candles),
                                        bar_times)
                    if panes:
                        await ws_send(websocket, {"type": "panes",
                                                   "series": panes})
                    # ZigZag market-structure overlay, per explicit
                    # request — same throttle/source as everything else
                    # here, no new candle fetch.
                    # ZigZag deliberately runs over the WARMED series
                    # (prior bars included) before clipping: pivot
                    # HH/HL/LH/LL structure is classified against the
                    # previous pivot of the same type, so including
                    # prior-session pivots makes the first visible
                    # pivot's structure label correct instead of
                    # "nothing to compare against yet".
                    zigzag = [p for p in _zigzag_series(overlay_candles)
                              if visible_from is None
                              or p["time"] >= visible_from]
                    if zigzag:
                        await ws_send(websocket, {"type": "zigzag",
                                                   "pivots": zigzag})
                if not indicators_warned and (not overlay_candles
                                              or len(overlay_candles) < 60):
                    indicators_warned = True
                    await ws_send(websocket, {
                        "type": "unavailable", "what": "indicators",
                        "reason": indi_note})
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        # ws_send is already a no-op on a dead peer. The old version
        # called send_json unconditionally, so a disconnect produced one
        # MORE failed write and one more "socket.send() raised
        # exception." line on the way out.
        await ws_send(websocket, {"type": "error", "message": str(e)})


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
            "future_position": (pilot.bus.get("futures_positions", {}) or {}).get(sym),
            "paper_mode": config.load().get("paper_mode", True),
            "ts": pilot.bus.get("ticker", {}).get(sym, {}).get("ts"),
            # 2026-07-27 — added for the consolidated LTP Monitor table
            # (replacing 4 separate cards with one row-per-symbol table,
            # per the wireframe): regime is already computed by
            # RegimeAgent and cached here — cheap to expose, no new
            # computation. OI-change-at-the-index-level and feed-source
            # (WS vs REST) are NOT included here — they'd need genuine
            # new work to expose cleanly (OI change here would need to
            # mean something at the index level, not per-strike, which
            # isn't already computed anywhere) rather than a quick
            # addition, so left out rather than faked.
            "regime": (pilot.bus.get(f"regime:{sym}") or {}).get("regime"),
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


@app.get("/api/news/impact-validation")
def api_news_impact_validation(symbol: str = "NIFTY", days: int = 10,
                               min_samples: int = 5):
    """v58.51 (roadmap B7) — has the news impact classifier ever been
    right, and which feeds earn their place?

    Scores every tracked event's realised index range against the
    distribution of all same-length windows on the SAME DAY, so the
    answer is a percentile rank rather than an absolute move that means
    nothing on its own. Events the classifier said would NOT move price
    are the control group — without them there is no way to distinguish
    a working classifier from one that labels everything.

    Per-feed medians are the objective form of "review the ~9 feeds for
    relevance": a feed whose flagged headlines land at a median rank
    near 50 is indistinguishable from picking windows at random.
    """
    import news_validation
    return news_validation.load_and_validate(symbol, days,
                                             min_samples=min_samples)


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


@app.get("/api/backtest/day-candles")
def api_backtest_day_candles(symbol: str, day: str):
    """Historical 1m index candles for one specific day — powers the
    Backtest-page chart overlay (v55). Deliberately scoped to ONE day
    at a time rather than a full multi-month range: a candlestick chart
    spanning many disjoint trading days (with large gaps between each
    day's session) doesn't render usefully, and the practical use case
    is "show me what this trade actually looked like," which is a
    single-day question. The frontend lets the person pick which day
    from the set that actually has trades."""
    import history
    rows = history.day_index_candles(symbol.upper(), day)
    return {"candles": [{"time": r["ts"], "open": r["open"], "high": r["high"],
                         "low": r["low"], "close": r["close"]} for r in rows]}


@app.get("/api/backtest/range-candles")
def api_backtest_range_candles(symbol: str, days: str):
    """1m index candles across SEVERAL days, concatenated.

    v58.37, replacing the day-at-a-time dropdown. The original
    single-day scoping (see api_backtest_day_candles above) reasoned
    that a chart spanning disjoint sessions renders badly because of
    the overnight gaps. That turns out not to be true for Lightweight
    Charts: its time scale allocates space only for bars that EXIST in
    the data, so overnight gaps collapse to nothing and consecutive
    sessions read as one continuous series. Scrolling left through
    prior days is then the natural gesture, which is what was asked
    for, and it also makes it immediately obvious when a day produced
    no trades — the previous UI simply omitted such days from the
    dropdown, which looked identical to the backtest not having run.

    `days` is a comma-separated list, oldest first.
    """
    import history
    out, boundaries = [], []
    for day in [d.strip() for d in days.split(",") if d.strip()]:
        rows = history.day_index_candles(symbol.upper(), day)
        if not rows:
            continue
        boundaries.append({"day": day, "ts": rows[0]["ts"], "bars": len(rows)})
        out.extend({"time": r["ts"], "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"]} for r in rows)
    out.sort(key=lambda c: c["time"])
    return {"candles": out, "day_boundaries": boundaries,
            "days_requested": len([d for d in days.split(",") if d.strip()]),
            "days_with_data": len(boundaries)}


@app.get("/api/backtest/status")
def api_backtest_status():
    import backtester, history, os, json as _j
    p = store.path("backtests.json")
    ran_at = None
    if os.path.exists(p):
        try:
            ran_at = _j.load(open(p)).get("at")
        except Exception:
            pass
    # v56 — latest optimizer-sweep record per (symbol, strategy), if
    # any has been run. Bus-only (not persisted to disk — a sweep is
    # cheap to re-run and this is transparency detail, not something
    # that needs to survive a restart the way version history does).
    sweeps = {}
    # 2026-07-27 — real gap found: this tuple was hardcoded and had
    # ALREADY silently excluded sg_ema (Strategy 7) from sweep-record
    # lookups since it was added — the same "two lists that drift"
    # class of bug already found and fixed once this session elsewhere.
    # This list is only used to check whether a sweep record EXISTS
    # (not to call evaluate() directly, unlike the eligibility-preview
    # loop above, which needs sg_ema excluded for a different reason) —
    # deriving the PA-strategy portion from PA_NAMES directly closes
    # the gap for every strategy, current and future.
    import pa_strategies as pa_lib3
    ALL_NAMES = ("bull_put_spread", "bear_call_spread", "momentum_buy") + pa_lib3.PA_NAMES
    for sym in SYMBOLS:
        for name in ALL_NAMES:
            rec = pilot.bus.get(f"bt_last_sweep:{sym}:{name}")
            if rec:
                sweeps.setdefault(sym, {})[name] = rec
    # v58.37 — the actual LIST of archived days per symbol, not just the
    # count. The Backtest chart needs it to render days the backtest ran
    # over but which produced NO trade: the old dropdown was built from
    # trade days only, so a no-setup day vanished and looked exactly
    # like the backtest never having run — which is how it was reported.
    archive_days = {}
    for _s in SYMBOLS:
        try:
            archive_days[_s] = {"days": sorted(history.index_days(_s))[-30:]}
        except Exception:
            archive_days[_s] = {"days": []}
    return {"coverage": history.coverage(),
            "archive": archive_days,
            "results": pilot.bus.get("bt_results") or _load_bt_results(),
            "results_at": ran_at,
            "versions": backtester.load_versions(),
            "sweeps": sweeps,
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
    p = store.path("backtests.json")
    return _j.load(open(p))["results"] if os.path.exists(p) else {}


@app.post("/api/backtest/run")
def api_backtest_run(sync: bool = False):
    """Queue a manual backtest run (executes in the agent thread)."""
    if not pilot.running:
        return {"error": "Start the agents first."}
    pilot.bus.set("bt_manual_job", {"sync": sync})
    return {"ok": True, "note": "queued — watch the backtest agent box"}


class OptimizeIn(BaseModel):
    name: str
    symbol: str


@app.post("/api/backtest/optimize")
def api_backtest_optimize(body: OptimizeIn):
    """v56 — queues an on-demand parameter SWEEP (genuinely searches
    several candidate values per tunable parameter, not just a single
    greedy nudge) for one (strategy, symbol). Runs in the agent thread,
    same as /api/backtest/run above — a coordinate-wise sweep is
    several full backtest replays, real seconds to tens of seconds,
    too slow to run inline on this request."""
    if not pilot.running:
        return {"error": "Start the agents first."}
    pilot.bus.set("bt_optimize_job", {"name": body.name, "symbol": body.symbol.upper()})
    return {"ok": True, "note": "optimizer queued — watch the backtest agent box; "
                                "results appear as a new version once done"}


@app.get("/api/ml-probability/status")
def api_ml_probability_status():
    """v58.9 (item 12) — reports the ACTUAL current Shadow Journal
    volume against the training threshold every time this is called,
    and trains a real model inline if volume is now sufficient. Cheap
    enough to run on every call rather than queued: reading and
    labeling the shadow journal is a single file scan, and logistic
    regression over a few hundred rows trains in well under a second.
    This is the direct, repeatable answer to "is there enough volume
    yet" — a measured fact refreshed on demand, not a standing
    assumption from when this was first scoped."""
    try:
        import ml_probability as ml
        closed_trades = pilot.bus.get("closed_trades", [])
        result = ml.train_model(closed_trades=closed_trades)
        return {"ok": True, "trained": result["trained"], "volume": result["volume"],
               "model_summary": ({"feature_names": result["model"]["feature_names"],
                                 "weights": [round(w, 4) for w in result["model"]["weights"]],
                                 "trained_on_n": result["model"]["trained_on_n"]}
                                if result["trained"] else None)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/backtest/audit-today")
def api_backtest_audit_today(symbol: str, name: str):
    """v58.9 (item 6) — on-demand retroactive audit for TODAY: replays
    the exact strategy rules against today's own archived data and
    compares against what actually happened live, returning the FULL
    comparison (not the trimmed summary LearningAgent's daily cycle
    persists to journal.json to avoid bloating that already-large
    file further). Cheap enough (one replay, not a multi-candidate
    sweep) to run inline rather than queued through the agent thread."""
    try:
        import backtester
        all_trades = pilot.bus.get("closed_trades", [])
        result = backtester.audit_today(name, symbol.upper(), all_trades)
        return {"ok": True, **result}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/history/prune-candles-status")
def api_history_prune_status():
    """v58.9 — dry-run count for the pre-v50 weekend-keepalive candle
    prune, per the roadmap's own framing ("cosmetic, low priority...
    a one-time offline prune would reclaim space and remove the read-
    filter dependency"). Read-only — safe to call anytime, does not
    touch the table. Scans the whole candles table (no index exists
    for the market-session predicate itself), so this can take a
    couple of seconds on a multi-year table — acceptable for a one-time
    maintenance check, not a per-request-latency-sensitive endpoint."""
    try:
        import history
        result = history.count_non_market_session_candles()
        return {"ok": True, **result}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/api/history/prune-candles")
def api_history_prune_candles(confirm: bool = False):
    """v58.9 — the actual prune. Requires `?confirm=true` explicitly —
    this is a destructive, one-time operation on persisted data, and
    silently running it from a bare POST would go against this
    project's own established caution around destructive operations
    on this exact table (the read-side filter was chosen originally
    specifically to AVOID a prune until it was actually wanted)."""
    if not confirm:
        return {"error": "Pass ?confirm=true to actually delete rows. "
                         "Call GET /api/history/prune-candles-status first "
                         "to see how many rows would be affected."}
    try:
        import history
        result = history.prune_non_market_session_candles(dry_run=False)
        return {"ok": True, **result}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(e)})


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


@app.get("/api/ta_elliott/calibration")
def api_ta_calibration(days: int = 5, symbol: str = None):
    """v58.32 — the calibration answer for Strategy 9, aggregated.

    Returns the hit rate of each of the seven confluence signals over
    real observed cycles, which signals NEVER fire, the phase mix, how
    often the Tide was unavailable, and the distribution of confluence
    counts actually reached. Replay against synthetic days showed only
    one of seven signals ever triggering; this is how to find out
    whether that holds on live data, and which thresholds to move.

    Populated by TAElliottAgent every cycle whenever
    ta_calibration_logging is on — auto_deploy does NOT need to be on,
    which is the whole point.
    """
    symbol = require_symbol(symbol) if symbol else None
    import history
    return history.ta_calibration_summary(days=days, symbol=symbol)


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
    # 2026-07-27 — real gap found from a direct report ("nothing
    # changed") after shipping a build with genuine, verified layout
    # changes: FileResponse's default headers (Last-Modified/ETag,
    # derived from the file's own mtime/size) let browsers skip
    # re-fetching entirely on a normal reload — the page looked
    # unchanged not because the file wasn't updated, but because the
    # BROWSER never asked the server for it again. This has real
    # consequences during active testing: every future build could hit
    # the same "did you do a hard refresh?" question. Explicit
    # Cache-Control headers force a fresh fetch every load instead.
    return FileResponse(os.path.join(BASE, "static", "dashboard.html"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                "Pragma": "no-cache", "Expires": "0"})


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
