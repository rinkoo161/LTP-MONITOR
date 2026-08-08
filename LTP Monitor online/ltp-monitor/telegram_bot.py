"""telegram_bot.py — push notifications and a READ-ONLY chat interface.

Sends: trade fills and other alerts, a periodic P&L summary, market
open/close transitions, and news the system already judged material.
Answers: /pnl /positions /status /news /help.

THREE THINGS THIS DELIBERATELY DOES NOT DO
------------------------------------------
1. **It cannot place, modify or exit an order.** No command touches
   `Orchestrator.manual_trade`, `enter_spread`, or any execution path.
   CLAUDE.md's invariant is that every order passes
   `RiskAgent.evaluate()`; a chat message that could trade would be a
   second execution path reachable by anyone holding the bot token, and
   a bot token is a bearer credential that travels through Telegram's
   servers. Read-only is the only defensible shape here.
2. **It does not invent a second notion of "notify-worthy".** It
   forwards `bus.alerts` — the same stream the dashboard bell renders,
   already populated by execution/risk/news/volatility. Deciding
   separately what deserves a message is exactly how the news sentiment
   regexes and the OI quadrant classifier drifted into two definitions.
3. **It does not answer strangers.** Every update is checked against
   `telegram_chat_id`; anything else is dropped and counted. Without
   that, whoever finds the bot reads your book.

DATA LEAVES THE MACHINE. Positions, P&L and symbols are sent to
api.telegram.org in cleartext-to-Telegram (TLS in transit, plaintext to
them). That is inherent to the request, not an oversight — but it is the
reason this ships DISABLED and needs a token pasted in Settings, rather
than defaulting on.

Ships off (`telegram_enabled: False`), same as every other integration
in this codebase.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config

IST = timezone(timedelta(hours=5, minutes=30))
API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "*LTP Monitor*\n"
    "/pnl — realised P&L today + open risk\n"
    "/positions — open positions and spreads\n"
    "/status — agents, market session, link health\n"
    "/news — latest material headlines\n"
    "/help — this message\n\n"
    "_Read-only. This bot cannot place or exit trades._"
)


def _call(token, method, params=None, timeout=15):
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fmt_money(v):
    try:
        return f"₹{float(v):+,.0f}"
    except (TypeError, ValueError):
        return "₹—"


class TelegramAgent(threading.Thread):
    """Notifier + read-only chat. One thread, its own cadence."""

    name = "telegram"
    interval = 15          # cheap due-check; real cadences are below

    def __init__(self, bus, ctx):
        super().__init__(daemon=True)
        self.bus, self.ctx = bus, ctx
        self.stop_evt = threading.Event()
        self.last_run = None
        self.status = "idle"
        self.summary = ""
        self._offset = 0            # Telegram getUpdates cursor
        self._seen_alert = None     # watermark into bus.alerts
        self._last_pnl_push = 0.0
        self._last_session = None   # None until first observation
        self._warned = False
        self._rejected = 0

    def info(self):
        return {"name": self.name, "status": self.status,
                "summary": self.summary, "last_run": self.last_run}

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.cycle()
                self.last_run = datetime.now(IST).strftime("%H:%M:%S")
            except Exception as e:
                # Loud, never swallowed — the house rule. A notifier that
                # dies quietly is worse than none: it looks armed.
                self.status = "error"
                self.bus.log(self.name, f"⚠ {type(e).__name__}: {e}")
            self.stop_evt.wait(self.interval)

    # ---------------------------------------------------------- sending
    def _send(self, token, chat_id, text):
        try:
            _call(token, "sendMessage",
                  {"chat_id": chat_id, "text": text,
                   "parse_mode": "Markdown",
                   "disable_web_page_preview": "true"})
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.bus.log(self.name, f"⚠ send failed: {type(e).__name__}: {e}")
            return False

    # ------------------------------------------------------- P&L / view
    def _today_pnl(self):
        """Realised P&L for TODAY only.

        `closed_trades` is loaded at startup from the FULL persisted
        history and appended to all session — it is NOT reset daily.
        Summing it whole would report lifetime P&L as today's, which is
        the exact bug LearningAgent's journal had (see its comment).
        """
        today = datetime.now(IST).strftime("%Y-%m-%d")
        rows = self.bus.get("closed_trades", []) or []
        mine = [t for t in rows if str(t.get("closed_date") or "")[:10] == today]
        net = sum((t.get("pnl") or 0) for t in mine)
        wins = sum(1 for t in mine if (t.get("pnl") or 0) > 0)
        return net, len(mine), wins

    def _positions_text(self):
        pos = self.bus.get("positions", {}) or {}
        spr = self.bus.get("spreads", {}) or {}
        if not pos and not spr:
            return "No open positions."
        out = []
        for k, p in pos.items():
            out.append(f"• {p.get('symbol', k)} {p.get('leg', '')} "
                       f"{p.get('strike', '')} × {p.get('lots', '?')} lot(s) "
                       f"@ {p.get('entry', '?')}")
        for k, s in spr.items():
            out.append(f"• {s.get('symbol', k)} SPREAD {s.get('name', '')} "
                       f"× {s.get('lots', '?')} lot(s)")
        return "\n".join(out)

    def _status_text(self):
        import agents as _ag
        net, n, wins = self._today_pnl()
        open_n = len(self.bus.get("positions", {}) or {}) + \
            len(self.bus.get("spreads", {}) or {})
        ms = self.bus.get("ms_link") or {}
        return (f"*Session*: {'OPEN' if _ag.market_open() else 'CLOSED'}\n"
                f"*Today*: {_fmt_money(net)} over {n} trade(s), {wins} win(s)\n"
                f"*Open*: {open_n}\n"
                f"*MarketSense*: {'ok' if ms.get('ok') else 'down/stale'}")

    def _news_text(self):
        n = self.bus.get("news") or {}
        items = (n.get("headlines") or n.get("items") or [])[:5]
        if not items:
            return "No material headlines on the bus."
        out = []
        for h in items:
            t = h.get("title") if isinstance(h, dict) else str(h)
            out.append(f"• {t}")
        return "\n".join(out)

    # ----------------------------------------------------------- cycle
    def cycle(self):
        cfg = config.load()
        if not cfg.get("telegram_enabled", False):
            self.status = "disabled"
            return
        token = (cfg.get("telegram_bot_token") or "").strip()
        chat_id = str(cfg.get("telegram_chat_id") or "").strip()
        if not token or not chat_id:
            self.status = "unconfigured"
            if not self._warned:
                self._warned = True
                self.bus.log(self.name,
                             "⚠ telegram_enabled is on but "
                             "telegram_bot_token/telegram_chat_id are not set "
                             "— nothing will be sent")
            return
        self._warned = False
        self.status = "ok"
        sent = 0
        sent += self._push_alerts(cfg, token, chat_id)
        sent += self._push_session(token, chat_id)
        sent += self._push_pnl(cfg, token, chat_id)
        self._poll_commands(token, chat_id)
        self.summary = (f"offset {self._offset}, {sent} pushed this cycle"
                        + (f", {self._rejected} rejected" if self._rejected else ""))

    def _push_alerts(self, cfg, token, chat_id):
        """Forward NEW entries from the shared alert stream."""
        order = {"low": 0, "medium": 1, "high": 2}
        floor = order.get(cfg.get("telegram_alert_min_severity", "medium"), 1)
        alerts = list(self.bus.alerts)
        if self._seen_alert is None:
            # First cycle: adopt the current tip as the watermark instead
            # of replaying up to 100 backlogged alerts into the chat.
            self._seen_alert = alerts[-1]["id"] if alerts else "0"
            return 0
        fresh = [a for a in alerts if a.get("id", "0") > self._seen_alert]
        n = 0
        for a in fresh:
            if order.get(a.get("severity", "low"), 0) < floor:
                continue
            icon = {"high": "🔴", "medium": "🟠", "low": "⚪"}.get(a.get("severity"), "•")
            if self._send(token, chat_id,
                          f"{icon} *{a.get('category', '?')}* "
                          f"{a.get('symbol', '')}\n{a.get('message', '')}"):
                n += 1
        if fresh:
            self._seen_alert = fresh[-1].get("id", self._seen_alert)
        return n

    def _push_session(self, token, chat_id):
        import agents as _ag
        now_open = _ag.market_open()
        if self._last_session is None:
            self._last_session = now_open      # no message on boot
            return 0
        if now_open == self._last_session:
            return 0
        self._last_session = now_open
        if now_open:
            return int(self._send(token, chat_id, "🟢 *Market OPEN*"))
        net, n, wins = self._today_pnl()
        return int(self._send(
            token, chat_id,
            f"🔴 *Market CLOSED*\nToday: {_fmt_money(net)} over {n} "
            f"trade(s), {wins} win(s)"))

    def _push_pnl(self, cfg, token, chat_id):
        import agents as _ag
        mins = int(cfg.get("telegram_pnl_interval_min", 30))
        if mins <= 0 or not _ag.market_open():
            return 0
        if time.time() - self._last_pnl_push < mins * 60:
            return 0
        self._last_pnl_push = time.time()
        net, n, wins = self._today_pnl()
        open_n = len(self.bus.get("positions", {}) or {}) + \
            len(self.bus.get("spreads", {}) or {})
        return int(self._send(
            token, chat_id,
            f"📊 *P&L update*\n{_fmt_money(net)} over {n} trade(s), "
            f"{wins} win(s)\nOpen: {open_n}"))

    def _poll_commands(self, token, chat_id):
        try:
            r = _call(token, "getUpdates",
                      {"offset": self._offset + 1, "timeout": 0, "limit": 20})
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.bus.log(self.name, f"⚠ getUpdates: {type(e).__name__}: {e}")
            return
        for upd in r.get("result", []):
            self._offset = max(self._offset, upd.get("update_id", 0))
            msg = upd.get("message") or upd.get("edited_message") or {}
            frm = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if frm != chat_id:
                # Counted, not silent: a stranger probing the bot is
                # something the operator should be able to see.
                self._rejected += 1
                self.bus.log(self.name,
                             f"⚠ ignored message from unauthorised chat {frm}")
                continue
            self._handle(token, chat_id, text.split()[0].lower().lstrip("/"))

    def _handle(self, token, chat_id, cmd):
        if cmd in ("pnl", "p"):
            net, n, wins = self._today_pnl()
            self._send(token, chat_id,
                       f"📊 Today: {_fmt_money(net)} over {n} trade(s), "
                       f"{wins} win(s)")
        elif cmd in ("positions", "pos"):
            self._send(token, chat_id, self._positions_text())
        elif cmd == "status":
            self._send(token, chat_id, self._status_text())
        elif cmd == "news":
            self._send(token, chat_id, self._news_text())
        else:
            self._send(token, chat_id, HELP)

    def stop(self):
        self.stop_evt.set()
