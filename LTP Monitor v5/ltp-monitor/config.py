"""
Local settings store: saves credentials and trading preferences to
config.json next to the app, so nothing needs to be typed in the terminal.
Environment variables still work as fallback.

NOTE: config.json holds your API keys in plain text on YOUR machine.
Keep the folder private; don't sync it to cloud drives or git.
"""

import json
import os
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.expanduser("~/.ltp-monitor"), "config.json")
_OLD_PATH = os.path.join(BASE, "config.json")
os.makedirs(os.path.dirname(PATH), exist_ok=True)
# one-time migration: settings previously lived inside the app folder and
# were lost on every version update — copy them to the persistent store
if os.path.exists(_OLD_PATH) and not os.path.exists(PATH):
    try:
        import shutil
        shutil.copy(_OLD_PATH, PATH)
    except Exception:
        pass
_lock = threading.Lock()

DEFAULTS = {
    "dhan_client_id": "",
    "dhan_access_token": "",
    "anthropic_api_key": "",
    "theme": "dark",
    "paper_mode": True,          # simulate orders until explicitly disabled
    "auto_execute": False,       # autopilot may only place orders when True
    "min_confidence": 70,        # AI confidence needed to act
    "max_trades_per_day": 3,     # hard cap for autopilot
    "daily_loss_limit": 5000,    # ₹; risk agent blocks orders beyond this
    "cooldown_after_loss_min": 15,  # pause new signals for N minutes after any loss
    "stop_after_consecutive_losses": 2,  # stop autopilot after N losses in a row
    "regime_gate_enabled": True,   # block trades in choppy/rangebound regimes
    "require_tf_confluence": True, # require 1m/5m/15m to agree with signal direction
    "lots_per_trade": 1,
    "max_concurrent_positions": 1,   # allow >1 to trade multiple indices at once
    "fee_per_lot": 40,
    "trail_sl_enabled": True,    # trail SL upward once trade is in profit
    "trail_sl_trigger_pct": 5,   # start trailing after +5% over entry
    "trail_sl_gap_pct": 10,      # keep SL 10% below the peak price
    "auto_strategies": [],       # strategy names auto-deployed when eligible
    "max_concurrent_spreads": 2, # cap on simultaneously open spreads
    "spread_reentry_cooldown_min": 15,
    "pa_min_trades_for_confidence": 15,  # min backtest trades before a version can go live
    "pa_tuning_improvement_threshold": 0.15,  # new version must improve P&L by this fraction
    "pa_tuning_max_attempts": 4,      # consecutive non-improving attempts before pausing
    "pa_retune_cooldown_days": 7,     # days to wait after exhausting attempts
    "backtest_capital": 200000,      # assumed available capital for sizing/backtest context
    "margin_per_lot_spread": 85000,  # approx margin blocked per lot when SELLING a spread leg
                                      # (buying options only costs premium, already captured
                                      # via entry price × qty — this is for the sold leg)
    "dynamic_sizing_enabled": False,  # off by default — opt in explicitly
    "risk_pct_per_trade": 1.0,        # % of capital risked per trade when sizing is dynamic
    "max_lots_per_trade": 10,         # hard cap regardless of the risk-budget math
    "portfolio_kill_switch_enabled": True,
    "portfolio_max_drawdown": 15000,  # combined UNREALIZED loss across all open
                                       # positions+spreads that force-closes everything
                                       # (separate from daily_loss_limit, which only
                                       # gates new entries against REALIZED P&L —
                                       # this catches a correlated shock mid-event,
                                       # the gap our regression testing surfaced)
    "portfolio_halt_cooldown_min": 60,  # after a kill-switch trip, block new
                                        # entries for this long before resuming
    "time_stop_minutes": 0,  # exit any position/spread still open after this
                             # many minutes, regardless of P&L — 0 disables it.
                             # Addresses the observation that some trades were
                             # held indefinitely waiting for a target that
                             # never came; a time stop forces a decision.
    "spread_defense_enabled": True,
    "spread_defense_zone_pct": 30,  # once spot is within this % of the
                                    # spread's width from the short strike
                                    # (but hasn't breached it yet), tighten
                                    # the loss limit rather than waiting for
                                    # a full breach — addresses the
                                    # observation that spreads need defense
                                    # rules BEFORE the short strike is hit,
                                    # not only a hard exit once it is
    "spread_defense_tighten_pct": 50,  # tighten loss_limit to this % of
                                       # its current value when defense fires  # wait after closing before re-entering same setup
    "broker": "dhan",            # dhan | zerodha | kotak — active data+order broker
    "zerodha_api_key": "",
    "zerodha_access_token": "",  # regenerate daily via Kite login flow
    "kotak_consumer_key": "",
    "kotak_access_token": "",
    "kotak_sid": "",
    "kotak_mobile": "",
    "kotak_ucc": "",
    "kotak_session_token": "",
    "kotak_base_url": "",
    "kotak_auth_token": "",           # ₹ brokerage+charges per lot per transaction
                                 # (entry and exit each count as one transaction)
    # verify lot sizes with your broker; exchanges revise them periodically
    "lot_sizes": {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "SENSEX": 20},
    # ---- AI engine + cost controls ----
    "ai_engine": "local",        # local (Ollama) | online (Anthropic) | auto | off
    "ollama_model": "qwen2.5:3b", # DEFAULT: lightweight ~2GB. Safer options:
                                  # qwen2.5:1.5b (~1GB, works on 8GB Macs),
                                  # llama3.2:3b (~2GB). AVOID llama3.1 (8B)
                                  # on <16GB Macs — it freezes the machine.
    "ollama_num_thread": 4,      # max CPU cores Ollama can use (out of your total)
    "ollama_num_ctx": 2048,      # context window (smaller = less RAM)
    "ollama_keep_alive": "2m",   # unload model after 2 min idle to free RAM
    "ollama_timeout": 60,        # seconds; fail fast if machine thrashes
    "ai_enabled": True,          # kept for back-compat; off == ai_engine "off"
    "ai_active_only": True,      # only call AI for the symbol you're viewing
    "ai_min_interval": 180,      # min seconds between AI calls per symbol (cache TTL)
    "ai_daily_call_cap": 400,    # hard stop on LLM calls per day across everything
    "ai_signal_on_change_only": True,  # skip AI if chain/bias barely moved
    "news_block_minutes": 20,    # how long a news risk event blocks trades
}

SECRET_KEYS = ("dhan_client_id", "dhan_access_token", "anthropic_api_key",
               "zerodha_api_key", "zerodha_access_token",
               "kotak_consumer_key", "kotak_access_token",
               "kotak_sid", "kotak_auth_token",
               "kotak_session_token", "kotak_mobile")


def load() -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(PATH):
            try:
                cfg.update(json.load(open(PATH)))
            except Exception:
                pass
    # env fallback for secrets
    cfg["dhan_client_id"] = cfg["dhan_client_id"] or os.environ.get("DHAN_CLIENT_ID", "")
    cfg["dhan_access_token"] = cfg["dhan_access_token"] or os.environ.get("DHAN_ACCESS_TOKEN", "")
    cfg["anthropic_api_key"] = cfg["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save(updates: dict) -> dict:
    with _lock:
        cfg = dict(DEFAULTS)
        if os.path.exists(PATH):
            try:
                cfg.update(json.load(open(PATH)))
            except Exception:
                pass
        for k, v in updates.items():
            if k in DEFAULTS and v is not None:
                cfg[k] = v
        json.dump(cfg, open(PATH, "w"), indent=2)
    return cfg


def public_view(cfg: dict) -> dict:
    """Settings safe to send to the browser (secrets masked)."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        v = cfg.get(k, "")
        out[k + "_set"] = bool(v)
        out[k + "_masked"] = (v[:6] + "…" + v[-4:]) if len(v) > 12 else ("set" if v else "")
    return out
