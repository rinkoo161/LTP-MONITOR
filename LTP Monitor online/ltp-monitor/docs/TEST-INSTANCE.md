# Test instance for external review

Created 2026-08-13 so an external expert can exercise LTP Monitor without
touching the live system.

## Access

| | |
|---|---|
| URL | `http://127.0.0.1:8001` |
| Username | `test` |
| Password | `test1234` |
| Authenticator | **not required** on this instance |

Start / stop:

```bash
cd "/Users/user/Documents/Stock Tools/LTP Monitor online/ltp-monitor"
LTP_MONITOR_HOME=~/.ltp-monitor-test PORT=8001 \
  nohup ./venv/bin/python3 app.py > ~/.ltp-monitor-test/app.out 2>&1 &

pkill -f "LTP_MONITOR_HOME=.*8001"        # or: pkill -f "app.py" and restart prod
```

## Why the password is not `test`

`auth.create_user()` enforces an 8-character minimum. That floor is not
worth weakening for convenience on a system holding broker credentials,
so the account uses `test1234` — obviously a test credential, still
inside the rule.

## Why there is no fixed authenticator code

A fixed TOTP code is not possible, by construction. TOTP (RFC 6238)
derives a 6-digit code from a shared secret **and the current time**,
rolling every 30 seconds; there is no value that stays valid. A code like
`12345678` is also 8 digits, which is not a TOTP shape at all.

The intent — "the expert should not have to pair an authenticator" — is
met a different way: this instance sets `auth_require_mfa: false`, so
username + password is the whole login. **Production still requires
MFA** and is unaffected:

```
:8001  {"enabled":true,"needs_setup":false,"require_mfa":false}   ← sandbox
:8000  {"enabled":true,"needs_setup":false,"require_mfa":true}    ← production
```

## What is isolated (verified, not assumed)

Isolation comes from `store.py`'s `LTP_MONITOR_HOME` — the same mechanism
the test suite uses. Everything stateful moves with it:

| | Production | Sandbox |
|---|---|---|
| Home | `~/.ltp-monitor` | `~/.ltp-monitor-test` |
| Port | 8000 | 8001 |
| Accounts (`users.json`) | `rinkoo161` (admin) | `test` (user) |
| Trade record / P&L | the real one | **empty — own P&L** |
| Config, positions, journal | own | own |
| Agents, bus, sessions | own process | own process |

Session isolation was checked by logging in on 8001 and presenting that
cookie to 8000 — it is rejected (`not authenticated`). Sessions are
in-process, so they cannot cross.

## What the sandbox deliberately does NOT have

- **No broker credentials.** Two consequences, both intended: it cannot
  place a real order under any setting, and it cannot consume the
  production Dhan rate limit (1 chain request / 3 s is per *account*, so
  a second instance polling live would degrade the real feed — the same
  shared-budget failure already seen with Ollama and Kite).
- **`ai_engine: off`** — no contention for the local Ollama model.
- **Telegram off.**
- **`paper_mode: true`, `auto_execute: false`.**

Consequence: **no live market data.** To make the instance useful anyway,
`history.db` (568 MB of archived candles and chain snapshots) and
`strategy_versions.json` were copied in, so backtests, the promotion-gate
report, charts, and strategy review all work against real history.

If live data is genuinely required for the review, the honest trade-off
is: add Dhan credentials to `~/.ltp-monitor-test/config.json` and accept
that both instances share one rate limit — and that the expert can then
flip `paper_mode`. Prefer running the sandbox outside market hours
instead.

## The multi-user question, answered honestly

**One instance cannot give two users separate P&L.** The application is
single-tenant by design:

- one `Bus`, one set of ~15 agents, one `positions`/`spreads` state;
- one `trades.jsonl`, one `config.json`, one `history.db`;
- authentication controls *access to the app*, not data partitioning.

`auth.py` says so in its own header: *"BOTH ROLES HAVE THE SAME
OPERATIONAL ACCESS, per explicit choice — the split exists for separate
credentials and attribution, not to restrict trading."* Of 92 routes,
only 6 are admin-gated (account management). Any logged-in user —
`user` role included — can change settings, place a manual trade, or
exit an open position, and every one of those acts on the single shared
state.

So sharing the production login with a reviewer means they see your real
P&L and can affect your real positions. **Separate instances are the
supported way to get separate sessions and separate P&L**, and that is
what this document describes.

Making one instance genuinely multi-tenant would mean per-user bus
namespaces, per-user trade records and per-user broker credentials —
a redesign, not a setting.
