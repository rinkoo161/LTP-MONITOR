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
PATH = os.path.join(BASE, "config.json")
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
    "lots_per_trade": 1,
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

SECRET_KEYS = ("dhan_client_id", "dhan_access_token", "anthropic_api_key")


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
