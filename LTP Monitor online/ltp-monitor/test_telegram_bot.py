#!/usr/bin/env python3
"""test_telegram_bot.py — a chat bot must not become a second order path.

2026-08-08. The bot pushes fills, P&L, session changes and news, and
answers /pnl /positions /status /news. Everything that matters about it
is a NEGATIVE property:

  * it cannot place, modify or exit an order. CLAUDE.md's invariant is
    that every order passes RiskAgent.evaluate(); a chat command that
    could trade would be a second execution path reachable by anyone
    holding the bot token — and a bot token is a bearer credential that
    travels through Telegram's servers.
  * it answers only the configured chat. Without that check, whoever
    finds the bot reads the book.
  * it does not invent a second notion of "notify-worthy" — it forwards
    bus.alerts, the same stream the dashboard bell renders. Two
    definitions of the same thing is how the news regexes and the OI
    quadrant classifier drifted.
  * it never reports lifetime P&L as today's. `closed_trades` is loaded
    at startup from the FULL persisted history and appended to all
    session; summing it whole is the exact bug LearningAgent's journal
    had.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_telegram_bot")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import config
import telegram_bot

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "telegram_bot.py")).read()
_code = [l for l in SRC.split("\n") if not l.strip().startswith("#")]


def _agent(**bus_vals):
    bus = agents.Bus()
    for k, v in bus_vals.items():
        bus.set(k, v)
    return telegram_bot.TelegramAgent(bus, {}), bus


print("1) it CANNOT trade — no execution path is reachable from chat")
# Parsed, not grepped. The first version matched the module's OWN
# docstring — the paragraph explaining that it does NOT call these — and
# failed. That is the same prose-matching mistake this codebase has hit
# before; the fix is to check what the code DOES, not what it says.
import ast

_tree = ast.parse(SRC)
_called, _attrs = set(), set()
for node in ast.walk(_tree):
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            _called.add(f.id)
        elif isinstance(f, ast.Attribute):
            _called.add(f.attr)
    elif isinstance(node, ast.Attribute):
        _attrs.add(node.attr)

for forbidden in ("manual_trade", "enter_spread", "place_order",
                  "confirm_pending", "square_off"):
    check(f"never CALLS {forbidden}()", forbidden not in _called,
          "a chat command that could trade would bypass the "
          "RiskAgent.evaluate() invariant and be reachable by anyone "
          "holding the bot token")
check("never touches an order/broker handle",
      not ({"orders", "orders_factory", "broker", "dhan_client"} & _attrs),
      f"attributes used: {sorted(_attrs & {'orders','orders_factory','broker','dhan_client'})}")
check("only sendMessage and getUpdates are ever requested",
      set(_api_methods := sorted(set(
          n.value for n in ast.walk(_tree)
          if isinstance(n, ast.Constant) and isinstance(n.value, str)
          and n.value in ("sendMessage", "getUpdates", "sendPhoto",
                          "sendDocument")))) <= {"sendMessage", "getUpdates"},
      f"{_api_methods}")

print("\n1b) messages are HTML and every interpolated value is ESCAPED")
# This is a correctness check, not a style one. Under legacy Markdown an
# ODD number of underscores is an unterminated entity and Telegram
# rejects the WHOLE message with HTTP 400 — which _send() catches and
# logs, so the notification silently never arrives. Measured live
# 2026-08-08: vwap_pullback, ema_mtf, momentum_confluence and sg_ema all
# returned 400 (message LOST); bull_put_spread sent but rendered
# bull<i>put</i>spread. Four of pa_strategies.PA_NAMES — the fills most
# worth being told about — were the ones that failed.
check("parse_mode is HTML, not Markdown",
      '"parse_mode": "HTML"' in SRC and '"parse_mode": "Markdown"' not in SRC,
      "Markdown drops any message whose strategy name has an odd number "
      "of underscores")
check("an escaping helper exists", "def _esc(" in SRC)
# Behaviour, not a source window. The first version grepped 400 chars
# after "def _esc(" and failed because the docstring is longer than that
# — testing the prose again instead of the code.
import telegram_bot as _tgm
check("_esc covers &, < and >",
      _tgm._esc("a & b < c > d") == "a &amp; b &lt; c &gt; d",
      _tgm._esc("a & b < c > d"))
check("and & is escaped FIRST, so &lt; is not double-escaped",
      _tgm._esc("<") == "&lt;", _tgm._esc("<"))
check("the alert body escapes the message field",
      "_esc(a.get('message'" in SRC or '_esc(a.get("message"' in SRC,
      "a['message'] is where strategy names land")
check("and escapes the symbol and category too",
      "_esc(a.get('category'" in SRC and "_esc(a.get('symbol'" in SRC)
# the real regression: the four names that were lost must survive escaping
import telegram_bot as _tg
for _n in ("vwap_pullback", "ema_mtf", "momentum_confluence", "sg_ema"):
    check(f"{_n} survives _esc() unchanged", _tg._esc(_n) == _n,
          "HTML has no underscore semantics — this is why the switch fixes it")
check("&lt;script&gt; style input is neutralised",
      _tg._esc("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;",
      "a headline containing markup must not become markup")

print("\n1c) the message cards are structured, not bare prose")
for frag, what in (("📊", "P&L card has an indicator"),
                   ("📂", "positions card has an indicator"),
                   ("⚙️", "status card has an indicator"),
                   ("📰", "news card has an indicator"),
                   ("<b>", "bold headings are used"),
                   ("def _rule(", "a separator rule exists"),
                   ("def _money(", "money carries a red/green cue")):
    check(what, frag in SRC)

print("\n2) it answers ONLY the configured chat")
check("_poll_commands compares the sender against telegram_chat_id",
      any("!= chat_id" in l for l in _code),
      "without this, whoever finds the bot reads your positions")
check("and an unauthorised sender is COUNTED, not silently dropped",
      any("_rejected" in l for l in _code) and "unauthorised chat" in SRC,
      "a stranger probing the bot is something the operator should see")

print("\n3) today's P&L is TODAY's, not lifetime")
ag, bus = _agent(closed_trades=[
    {"closed_date": "2019-01-01", "pnl": 999999},          # ancient
    {"closed_date": agents.now_ist().strftime("%Y-%m-%d"), "pnl": 250},
    {"closed_date": agents.now_ist().strftime("%Y-%m-%d"), "pnl": -100},
])
net, n, wins = ag._today_pnl()
check("only today's trades are summed", net == 150 and n == 2 and wins == 1,
      f"net={net} n={n} wins={wins} — closed_trades is loaded from the "
      f"FULL persisted history at startup and is NOT reset daily")

print("\n4) it forwards the SHARED alert stream, not its own idea of one")
check("_push_alerts reads bus.alerts",
      any("self.bus.alerts" in l for l in _code),
      "the same stream the dashboard bell renders")
ag, bus = _agent()
# _send MUST be stubbed. The first version of this check called the real
# one, which failed on the fake token and returned 0 — so `n == 0` held
# whether or not the backlog was replayed, and a mutation that replayed
# it went UNDETECTED. It also made a real request to api.telegram.org
# from the test suite.
_pushed = []
ag._send = lambda t, c, m: (_pushed.append(m), True)[1]
bus.alert("high", "execution", "NIFTY", "entered CE 24600")
# First cycle adopts the tip as a watermark instead of replaying history.
n = ag._push_alerts(config.DEFAULTS, "tok", "123")
check("the first pass does NOT replay the backlog",
      n == 0 and not _pushed,
      f"n={n} pushed={_pushed} — up to 100 buffered alerts would "
      f"otherwise flood the chat on every restart")
# and the NEXT alert, arriving after the watermark, IS forwarded
bus.alert("high", "execution", "NIFTY", "exited CE 24600")
n2 = ag._push_alerts(config.DEFAULTS, "tok", "123")
check("but a genuinely new alert IS forwarded",
      n2 == 1 and any("exited" in m for m in _pushed),
      f"n2={n2} pushed={_pushed} — a watermark that never advances "
      f"would mute the bot entirely")

print("\n5) severity floor is honoured")
sent = []
ag, bus = _agent()
ag._send = lambda t, c, m: (sent.append(m), True)[1]
ag._seen_alert = "0"
bus.alert("low", "risk", "NIFTY", "trivial")
bus.alert("high", "execution", "NIFTY", "important")
ag._push_alerts({"telegram_alert_min_severity": "medium"}, "tok", "123")
check("a 'low' alert is not forwarded at the medium floor",
      not any("trivial" in m for m in sent), str(sent))
check("a 'high' alert is forwarded",
      any("important" in m for m in sent), str(sent))

print("\n6) it ships OFF and degrades loudly when unconfigured")
check("telegram_enabled defaults to False",
      config.DEFAULTS.get("telegram_enabled") is False)
ag, bus = _agent()
config.save({"telegram_enabled": False})
ag.cycle()
check("disabled -> status 'disabled', nothing attempted",
      ag.status == "disabled", ag.status)
config.save({"telegram_enabled": True, "telegram_bot_token": "",
             "telegram_chat_id": ""})
ag.cycle()
check("enabled but unconfigured -> status 'unconfigured'",
      ag.status == "unconfigured", ag.status)
check("and it SAYS so once, rather than failing silently",
      any("nothing will be sent" in str(l) for l in bus.feed),
      str(list(bus.feed)[-2:]))
config.save({"telegram_enabled": False})

print("\n7) the token is a secret and the keys are settable")
check("telegram_bot_token is in SECRET_KEYS",
      "telegram_bot_token" in config.SECRET_KEYS,
      "otherwise public_view() ships it to the browser")
check("and public_view() masks it",
      "telegram_bot_token" not in config.public_view(config.load()))
APP = open(os.path.join(HERE, "app.py")).read()
for k in ("telegram_enabled", "telegram_bot_token", "telegram_chat_id",
          "telegram_pnl_interval_min", "telegram_alert_min_severity"):
    check(f"{k} in DEFAULTS", k in config.DEFAULTS)
    check(f"{k} in SettingsIn", f"{k}:" in APP,
          "config.save() silently drops any key not in DEFAULTS, and a "
          "key absent from SettingsIn cannot be set from Settings")

print("\n8) the agent is optional and registered")
check("TelegramAgent is in AGENT_CLASSES",
      any(c.__name__ == "TelegramAgent" for c in agents.AGENT_CLASSES))
AG = open(os.path.join(HERE, "agents.py")).read()
check("and its import degrades loudly, like MarketSense/NewsMacro",
      "telegram_bot unavailable" in AG)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all telegram checks passed")
